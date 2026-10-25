"""真实集成验收。不会修改 sample；所有实验只针对独立副本，不包含模拟引擎。"""
import argparse
import json
import logging
import sys
import zipfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.automation import Prime, validate_pdf  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.parser import ML, WS, parse, sha, xml  # noqa: E402
from backend.service import same_values  # noqa: E402
from backend.worker import cleanup_owned, host_lock, validate_saved  # noqa: E402


def detect_matrix_order(parsed, inputs, fields):
    worksheet = xml(parsed.parts["mathcad/worksheet.xml"])
    orders = set()
    for field in fields:
        value = inputs.get(field["id"])
        if not value or value["type"] != "matrix":
            continue
        region = worksheet.xpath(".//w:region[@region-id=$rid]", namespaces={"w": WS}, rid=field["region_id"])[0]
        matrix = region.find(f".//{{{ML}}}matrix")
        if matrix is None:
            raise RuntimeError("保存后的矩阵 XML 结构不支持，不能确认单元格顺序")
        actual = [float(cell.text) for cell in matrix]
        rows = value["value"]
        row_major = [float(cell) for row in rows for cell in row]
        col_major = [float(rows[r][c]) for c in range(len(rows[0])) for r in range(len(rows))]
        if row_major == col_major:
            raise RuntimeError("请使用至少 2×3 且单元格值各不相同的矩阵验证顺序")
        if actual == col_major:
            orders.add("column-major")
        elif actual == row_major:
            orders.add("row-major")
        else:
            raise RuntimeError("矩阵保存值与 API 输入不一致")
    if len(orders) != 1:
        raise RuntimeError("矩阵顺序不一致或没有可验证的矩阵")
    return orders.pop()


def smoke(args):
    work = Path(args.output_dir).resolve() if args.output_dir else settings().directory() / "smoke" / str(uuid4())
    work.mkdir(parents=True, exist_ok=False)
    report = {"verified": False, "sample": str(args.sample.resolve()), "stages": {}, "result_strategy": "api_recalculate"}
    original = args.sample.read_bytes()
    source_hash = sha(original)
    parsed = parse(original, matrix_order="column-major")  # 这里只是待实验的候选，不作为生产默认。
    payload = json.loads(args.inputs.read_text(encoding="utf-8-sig"))
    fields = [{**f, "id": f"{f['direction']}:{f['alias']}"} for f in parsed.fields]
    if not any(f["direction"] == "output" for f in fields):
        raise ValueError("smoke 样本需要配置至少一个输出，以验证计算结果")
    inputs = {f["id"]: payload[f["alias"]] for f in fields if f["direction"] == "input" and f["alias"] in payload}
    if set(payload) != {f["alias"] for f in fields if f["direction"] == "input"}:
        raise ValueError("smoke 输入 JSON 必须包含样本全部 input alias")
    if not {"real", "matrix"} <= {v["type"] for v in inputs.values()}:
        raise ValueError("smoke 需要至少一个实数输入和一个数值矩阵输入")
    for value in inputs.values():
        cells = [value["value"]] if value["type"] == "real" else [c for row in value["value"] for c in row]
        from backend.parser import finite
        if any(not isinstance(cell, str) or not finite(cell) for cell in cells):
            raise ValueError("输入必须是有限数值字符串")
    (work / "source-copy.mcdx").write_bytes(original)
    try:
        with host_lock(), Prime(work, allow_unverified=True) as prime:
            prime.open(work / "source-copy.mcdx")
            prime.verify_aliases(fields)
            report["version"] = prime.version
            report["aliases"] = {direction: prime.aliases(direction) for direction in ("input", "output")}
            prime.set_inputs(fields, inputs)
            prime.calculate()
            prime.verify_inputs(fields, inputs)
            outputs = prime.read_outputs(fields)
            if any(value.get("status") == "error" for value in outputs.values()):
                raise RuntimeError(f"配置输出计算失败：{outputs}")
            report["outputs"] = outputs
            report["stages"]["calculation"] = "passed"
            saved = work / "calculated.mcdx"
            prime.save(saved)
            prime.close_sheet()
            saved_parsed = validate_saved(saved, fields)
            order = detect_matrix_order(saved_parsed, inputs, fields)
            report["matrix_xml_order"] = order
            reparsed = parse(saved.read_bytes(), matrix_order=order)
            reparsed_values = {f"{f['direction']}:{f['alias']}": f["saved_value"] for f in reparsed.fields}
            for key, expected in inputs.items():
                if not same_values(expected, reparsed_values[key]):
                    raise RuntimeError(f"XML/API 矩阵或标量输入不一致：{key}")
            prime.open(saved)
            prime.verify_inputs(fields, inputs)
            if not same_values(outputs, prime.read_outputs(fields)):
                raise RuntimeError("保存重开后输出变化")
            report["stages"]["reopen"] = "passed"
            pdf = work / "preview.pdf"
            prime.print_pdf(pdf)
            report["pdf_pages"] = validate_pdf(pdf)
            if not same_values(outputs, prime.read_outputs(fields)):
                raise RuntimeError("打印后输出变化")
            report["stages"]["pdf"] = "passed"
            prime.close_sheet()
            # 删除 result.xml 的兼容性实验仅在临时 ZIP 中进行，保存关系/内容类型供人工核查。
            deleted = work / "experiment-no-result.mcdx"
            with zipfile.ZipFile(saved) as archive, zipfile.ZipFile(deleted, "w") as target:
                for item in archive.infolist():
                    if item.filename != "mathcad/result.xml":
                        target.writestr(item, archive.read(item))
            try:
                prime.open(deleted)
                prime.set_inputs(fields, inputs)
                prime.calculate()
                experiment_outputs = prime.read_outputs(fields)
                rebuilt = work / "experiment-rebuilt.mcdx"
                prime.save(rebuilt)
                prime.close_sheet()
                validate_saved(rebuilt, fields)
                prime.open(rebuilt)
                if not same_values(outputs, experiment_outputs) or not same_values(outputs, prime.read_outputs(fields)):
                    raise RuntimeError("删除结果后重算/重开输出不一致")
                report["delete_result_experiment"] = "passed; 生产仍使用已完整验证的 API 重算策略"
            except Exception as exc:
                report["delete_result_experiment"] = f"failed: {exc}"
            report["verified"] = True
    except Exception as exc:
        report["error"] = str(exc)
        logging.exception("真实 smoke 未通过")
    finally:
        cleanup_owned(work)
        report["source_unchanged"] = sha(args.sample.read_bytes()) == source_hash
        if not report["source_unchanged"]:
            report["verified"] = False
        (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("报告目录：", work)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True, help="按 alias 索引的完整输入 JSON")
    parser.add_argument("--output-dir")
    arguments = parser.parse_args()
    raise SystemExit(smoke(arguments))
