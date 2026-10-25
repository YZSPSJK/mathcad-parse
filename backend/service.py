import json
from pathlib import Path

from sqlalchemy import select

from backend.config import settings
from backend.db import Artifact, CalculationRun, Document, DraftInput, IOField, Job, Region, SourcePart, now, uid
from backend.formulas import (FormulaError, definition_xml, expression_from_xml, expression_key,
                              format_expression, parse_expression, references)
from backend.parser import finite, parse, sha

GENERATOR = "mathcad-workbench/0.2.0-formulas"


class BusinessError(Exception):
    def __init__(self, code: str, message: str, status=400, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details
        super().__init__(message)


def error(code, message, **details):
    return {"code": code, "message": message, "details": details}


def get_document(session, document_id):
    doc = session.get(Document, document_id)
    if doc is None:
        raise BusinessError("not_found", "文档不存在", 404)
    return doc


def snapshot_parts(session, document_id, snapshot, parts):
    session.add_all([SourcePart(document_id=document_id, snapshot=snapshot, path=path,
                                content=data, sha256=sha(data)) for path, data in parts.items()])


def upload(session, filename: str, data: bytes):
    if not filename.lower().endswith(".mcdx"):
        raise BusinessError("invalid_extension", "请选择 .mcdx 工作表")
    parsed = parse(data, matrix_order=settings().matrix_xml_order)
    document_id = uid()
    original = settings().directory() / "documents" / document_id / "original" / "source.mcdx"
    original.parent.mkdir(parents=True)
    with original.open("xb") as handle:
        handle.write(data)
    duplicate = session.scalar(select(Document.id).where(Document.sha256 == sha(data)))
    doc = Document(id=document_id, name=filename.replace("\\", "/").split("/")[-1][:255],
                   source_path=str(original), sha256=sha(data), size=len(data),
                   app_info=parsed.app_info, diagnostics=parsed.diagnostics,
                   parse_status="partial" if parsed.diagnostics else "parsed")
    session.add(doc)
    session.flush()
    snapshot_parts(session, document_id, "upload", parsed.parts)
    session.add_all([Region(document_id=document_id, **region) for region in parsed.regions])
    fields, drafts = [], []
    for field in parsed.fields:
        field.pop("geometry")
        field.pop("kind")
        row = IOField(id=uid(), document_id=document_id, **field)
        fields.append(row)
        if row.direction == "input" and row.supported:
            drafts.append(DraftInput(field_id=row.id, document_id=document_id, value=clean(row.saved_value)))
    session.add_all(fields)
    session.flush()
    session.add_all(drafts)
    session.commit()
    return {"id": document_id, "duplicate_of": duplicate}


def clean(value):
    return {k: v for k, v in value.items() if k in {"type", "value", "units", "rows", "cols"}}


def canonical(value):
    """仅用于比较 API double 的数值含义；编辑字符串仍原样持久保存。"""
    if isinstance(value, dict):
        if value.get("type") == "formula":
            return "formula", value.get("units", ""), expression_key(parse_expression(value["value"]))
        return {k: canonical(v) for k, v in value.items() if k not in {"status", "raw_xml", "reason"}}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, str) and finite(value):
        return float(value)
    return value


def formula_baseline(fields, values):
    # 旧 revision 未显式记录公式输入，源文件哈希已绑定其原始公式；只补齐比较基线，不改旧快照。
    result = {**{f.id: clean(f.saved_value) for f in fields if f.value_type == "formula" and f.supported}, **values}
    for field in fields:
        value = result.get(field.id)
        if field.value_type == "formula" and value and value["type"] == "real":
            result[field.id] = {"type": "formula", "value": value["value"], "units": value["units"]}
    return result


def same_values(left, right):
    return canonical(left) == canonical(right)


def current(session, doc):
    fields = session.scalars(select(IOField).where(IOField.document_id == doc.id)).all()
    drafts = session.scalars(select(DraftInput).where(DraftInput.document_id == doc.id)).all()
    run = session.scalar(select(CalculationRun).where(CalculationRun.document_id == doc.id,
                                                     CalculationRun.revision == doc.latest_revision))
    return fields, {d.field_id: d.value for d in drafts}, run


def validate_inputs(fields, values):
    expected = {f.id: f for f in fields if f.direction == "input" and f.supported}
    if set(expected) != set(values):
        raise BusinessError("incomplete_inputs", "必须提交全部可编辑输入，且不能包含输出或未知字段", 422)
    by_region = {}
    for field_id, payload in values.items():
        field = expected[field_id]
        value = payload.get("value")
        if payload.get("type") != field.value_type or payload.get("units", "") != field.units:
            raise BusinessError("invalid_type", f"{field.alias}：类型和单位不可修改", 422)
        if field.value_type == "real":
            if not isinstance(value, str) or len(value) > 128 or not finite(value):
                raise BusinessError("invalid_number", f"{field.alias}：请输入有限实数，支持科学计数法", 422)
        elif field.value_type == "matrix":
            source = field.saved_value
            rows, cols = source["rows"], source["cols"]
            if (payload.get("rows") != rows or payload.get("cols") != cols
                    or not isinstance(value, list) or len(value) != rows
                    or any(not isinstance(row, list) or len(row) != cols for row in value)):
                raise BusinessError("invalid_matrix", f"{field.alias}：矩阵维度固定为 {rows} × {cols}", 422)
            if any(not isinstance(v, str) or len(v) > 128 or not finite(v) for row in value for v in row):
                raise BusinessError("invalid_number", f"{field.alias}：矩阵单元格必须是有限实数", 422)
        elif field.value_type == "formula":
            try:
                parse_expression(value, field.saved_value.get("allowed_variables", []))
            except FormulaError as exc:
                raise BusinessError("invalid_formula", f"{field.alias}：{exc}", 422) from exc
        if field.region_id in by_region and not same_values(by_region[field.region_id], payload):
            raise BusinessError("alias_conflict", "同一区域的多个 alias 必须提交相同的数值", 422)
        by_region[field.region_id] = payload
    return {key: clean(payload) for key, payload in values.items()}


def begin_write(session):
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def check_version(doc, version):
    if doc.draft_version != version:
        raise BusinessError("version_conflict", "文档已在其他页面更新，请刷新后重试", 409)
    if doc.active_job:
        raise BusinessError("document_busy", "此文档有未完成任务，请等待任务结束", 409)


def update_drafts(session, doc, fields, values):
    values = validate_inputs(fields, values)
    rows = session.scalars(select(DraftInput).where(DraftInput.document_id == doc.id)).all()
    previous = {row.field_id: row.value for row in rows}
    # 字符串变化也保存，避免丢失用户的数字编辑格式。
    if previous != values:
        doc.draft_version += 1
        for row in rows:
            row.value = values[row.field_id]
            row.version = doc.draft_version
            row.updated_at = now()
    return values


def save_inputs(session, document_id, version, values):
    begin_write(session)
    doc = get_document(session, document_id)
    check_version(doc, version)
    fields = session.scalars(select(IOField).where(IOField.document_id == doc.id)).all()
    update_drafts(session, doc, fields, values)
    session.commit()
    return {"draft_version": doc.draft_version}


def new_calculation(session, doc, fields, inputs, kind, retry_of=None):
    values = update_drafts(session, doc, fields, inputs)
    doc.revision_counter += 1
    run = CalculationRun(id=uid(), document_id=doc.id, revision=doc.revision_counter,
                         draft_version=doc.draft_version, inputs=values,
                         configuration={"source_sha256": doc.sha256, "generator": GENERATOR,
                                        "strategy": settings().result_strategy,
                                        "timeout": settings().calculation_timeout})
    session.add(run)
    session.flush()
    job = Job(id=uid(), document_id=doc.id, run_id=run.id, kind=kind, retry_of=retry_of)
    session.add(job)
    doc.active_job = job.id
    return job


def submit_calculation(session, document_id, version, inputs, retry_of=None):
    begin_write(session)
    doc = get_document(session, document_id)
    check_version(doc, version)
    fields = session.scalars(select(IOField).where(IOField.document_id == doc.id)).all()
    job = new_calculation(session, doc, fields, inputs, "calculate", retry_of)
    session.commit()
    return {"job_id": job.id, "revision": doc.revision_counter}


def artifact_json(artifact):
    return {"id": artifact.id, "revision": artifact.revision, "kind": artifact.kind,
            "sha256": artifact.sha256, "size": artifact.size, "created_at": artifact.created_at,
            "url": f"/api/artifacts/{artifact.id}/content"}


def require_file(artifact):
    if not Path(artifact.path).is_file() or sha(Path(artifact.path).read_bytes()) != artifact.sha256:
        raise BusinessError("artifact_corrupt", "产物缺失或校验失败，请重新计算生成新版本", 409)


def request_artifact(session, document_id, version, revision, kind):
    begin_write(session)
    doc = get_document(session, document_id)
    check_version(doc, version)
    fields, drafts, run = current(session, doc)
    if run and not same_values(drafts, formula_baseline(fields, run.inputs)):
        raise BusinessError("draft_dirty", "输入已修改，请先计算再生成预览或导出", 409)
    if run is None:
        if revision is not None:
            raise BusinessError("revision_conflict", "指定版本不存在", 409)
        job = new_calculation(session, doc, fields, drafts,
                              "calculate_preview" if kind == "pdf" else "calculate")
        session.commit()
        return {"job_id": job.id, "revision": doc.revision_counter}
    if revision != run.revision:
        raise BusinessError("revision_conflict", "请明确指定当前计算版本", 409)
    artifacts = session.scalars(select(Artifact).where(Artifact.run_id == run.id)).all()
    artifact = next((a for a in artifacts if a.kind == kind), None)
    if artifact:
        require_file(artifact)
        session.commit()
        return {"artifact": artifact_json(artifact), "cached": True}
    if kind == "mcdx" or not any(a.kind == "mcdx" for a in artifacts):
        # 保存失败后的重试需要重新计算，故创建新 revision。
        job = new_calculation(session, doc, fields, drafts,
                              "calculate_preview" if kind == "pdf" else "calculate")
    else:
        job = Job(id=uid(), document_id=doc.id, run_id=run.id, kind="preview")
        session.add(job)
        doc.active_job = job.id
    session.commit()
    return {"job_id": job.id, "revision": doc.revision_counter}


def view(session, document_id, form=False):
    doc = get_document(session, document_id)
    fields, drafts, run = current(session, doc)
    artifacts = session.scalars(select(Artifact).where(Artifact.document_id == doc.id)
                               .order_by(Artifact.revision.desc())).all()
    jobs = session.scalars(select(Job).where(Job.document_id == doc.id)
                          .order_by(Job.created_at.desc()).limit(20)).all()
    baseline = formula_baseline(fields, run.inputs) if run else {f.id: clean(f.saved_value) for f in fields
                                      if f.direction == "input" and f.supported}
    data = {"id": doc.id, "name": doc.name, "sha256": doc.sha256, "size": doc.size,
            "uploaded_at": doc.uploaded_at, "app_info": doc.app_info, "parse_status": doc.parse_status,
            "diagnostics": doc.diagnostics, "draft_version": doc.draft_version,
            "latest_revision": doc.latest_revision, "active_job": doc.active_job,
            "dirty": not same_values(drafts, baseline),
            "artifacts": [artifact_json(a) for a in artifacts], "jobs": [job_json(j) for j in jobs],
            "run": {"revision": run.revision, "status": run.status, "artifact_status": run.artifact_status,
                    "error": run.error, "created_at": run.created_at} if run else None}
    if form:
        data["inputs"] = drafts
        data["fields"] = [{"id": f.id, "region_id": f.region_id, "result_ref": f.result_ref,
                           "direction": f.direction, "alias": f.alias, "display_name": f.display_name,
                           "value_type": f.value_type, "units": f.units, "supported": f.supported,
                           "reason": f.reason, "expression_xml": f.expression_xml,
                           "allowed_variables": f.saved_value.get("allowed_variables", []),
                           "formula_xml": definition_xml(f.display_name, parse_expression(
                               drafts.get(f.id, f.saved_value)["value"])) if f.value_type == "formula" and f.supported else None,
                           "effective_value": run.engine.get("effective_inputs", {}).get(f.id) if run
                           else f.saved_value.get("effective_value"),
                           "value": run.outputs.get(f.id, f.saved_value) if run and f.direction == "output"
                           else drafts.get(f.id, f.saved_value),
                           "origin": f"计算版本 r{run.revision}" if run and f.direction == "output"
                           else "草稿输入" if f.direction == "input" and (run or not same_values(
                               drafts.get(f.id, f.saved_value), f.saved_value)) else "文件中保存的值"}
                          for f in fields]
    return data


def convert_formula(session, document_id, field_id, *, expression=None, xml_content=None):
    get_document(session, document_id)
    field = session.get(IOField, field_id)
    if field is None or field.document_id != document_id:
        raise BusinessError("not_found", "输入字段不存在", 404)
    if field.direction != "input" or field.value_type != "formula" or not field.supported:
        raise BusinessError("formula_readonly", "此字段不支持公式编辑", 422)
    if (expression is None) == (xml_content is None):
        raise BusinessError("formula_payload", "请提交公式或 XML 中的一种", 422)
    try:
        allowed = field.saved_value.get("allowed_variables", [])
        tree = (parse_expression(expression, allowed) if expression is not None else
                expression_from_xml(xml_content, field.display_name, allowed))
        return {"expression": format_expression(tree), "xml": definition_xml(field.display_name, tree),
                "references": references(tree)}
    except FormulaError as exc:
        raise BusinessError("invalid_formula", str(exc), 422) from exc


def job_json(job):
    return {"id": job.id, "job_id": job.id, "document_id": job.document_id, "kind": job.kind,
            "status": job.status, "stage": job.stage, "error": job.error, "result": job.result,
            "created_at": job.created_at, "started_at": job.started_at, "completed_at": job.completed_at,
            "retry_of": job.retry_of}


def cache_key(run):
    payload = {"revision": run.revision, "inputs": run.inputs, "config": run.configuration,
               "generator": GENERATOR}
    return sha(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
