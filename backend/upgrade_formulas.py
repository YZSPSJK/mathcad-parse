"""为已上传文档启用受限公式；保留字段 ID、用户数值草稿和已有计算/产物快照。"""
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from backend.config import settings
from backend.db import Document, DraftInput, IOField, Session, migrate
from backend.parser import parse, sha
from backend.service import clean


def upgrade(session):
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    documents = session.scalars(select(Document)).all()
    fields = session.scalars(select(IOField)).all()
    drafts = session.scalars(select(DraftInput)).all()
    by_document = defaultdict(dict)
    for field in fields:
        by_document[field.document_id][(field.direction, field.alias, field.region_id)] = field
    by_field = {draft.field_id: draft for draft in drafts}
    changed = []
    for document in documents:
        if document.active_job:
            continue
        candidates = by_document[document.id]
        if not any(field.direction == "input" and field.value_type != "formula"
                   for field in candidates.values()):
            continue
        source = Path(document.source_path).read_bytes()
        if sha(source) != document.sha256:
            raise RuntimeError(f"{document.name} 源文件校验失败，停止公式升级")
        parsed = parse(source, matrix_order=settings().matrix_xml_order)
        changes = [(candidates[(item["direction"], item["alias"], item["region_id"])], item)
                   for item in parsed.fields if item["value_type"] == "formula" and item["supported"]
                   and (item["direction"], item["alias"], item["region_id"]) in candidates
                   and candidates[(item["direction"], item["alias"], item["region_id"])].value_type != "formula"
                   and (candidates[(item["direction"], item["alias"], item["region_id"])].id not in by_field
                        or by_field[candidates[(item["direction"], item["alias"], item["region_id"])].id].value["type"] in {"real", "formula"})]
        if not changes:
            continue
        document.draft_version += 1
        for field, item in changes:
            field.value_type, field.supported, field.reason = "formula", True, None
            field.saved_value, field.units = item["saved_value"], item["units"]
            if field.id not in by_field:
                session.add(DraftInput(field_id=field.id, document_id=document.id,
                                       value=clean(item["saved_value"]), version=document.draft_version))
            elif by_field[field.id].value["type"] == "real":
                draft = by_field[field.id]
                draft.value = {**draft.value, "type": "formula"}
                draft.version = document.draft_version
        changed.append(document.id)
    session.commit()
    return changed


if __name__ == "__main__":
    migrate()
    with Session() as session:
        print("已启用公式的文档：", upgrade(session))
