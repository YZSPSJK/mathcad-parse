"""只在任务工作副本中替换已分配输入的右侧公式，保留其他 ZIP 部件。"""
import os
import zipfile
from pathlib import Path

from lxml import etree as ET

from backend.formulas import (FormulaError, expression_from_xml, expression_key,
                              parse_expression, to_mathml)
from backend.latex import parse_latex, region_target, validate_region_expression
from backend.parser import ML, WS, xml
from backend.service import BusinessError


def patch_region_expressions(path, edits):
    """Apply saved LaTeX MathML only to a disposable worker copy of the worksheet."""
    if not edits:
        return
    path = Path(path)
    temporary = path.with_name(path.stem + "-latex-patch.mcdx")
    try:
        with zipfile.ZipFile(path) as archive:
            root = xml(archive.read("mathcad/worksheet.xml"))
            found = set()
            for region in root.findall(f".//{{{WS}}}region"):
                region_id = region.get("region-id")
                if region_id not in edits:
                    continue
                if region_id in found:
                    raise BusinessError("latex_region_conflict", "区域标识重复，无法安全写入公式", 409)
                found.add(region_id)
                math_node = region.find(f"{{{WS}}}math")
                if math_node is None:
                    raise BusinessError("latex_region_mismatch", f"区域 {region_id} 已不包含数学表达式", 409)
                try:
                    target, _ = region_target(ET.tostring(math_node, encoding="unicode"))
                    allowed = edits[region_id].get("allowed_variables", [])
                    replacement_xml = validate_region_expression(edits[region_id]["expression_xml"], allowed)
                    replacement = xml(replacement_xml.encode("utf-8"))
                    expected = parse_latex(edits[region_id]["latex"], allowed)
                    actual = expression_from_xml(replacement_xml)
                    if expression_key(expected) != expression_key(actual):
                        raise FormulaError("LaTeX 与已保存的 Mathcad 表达式不一致")
                except FormulaError as exc:
                    raise BusinessError("invalid_latex", f"区域 {region_id}：{exc}", 409) from exc
                if target == "define":
                    define = math_node.find(f"{{{ML}}}define")
                    original = define[1]
                    if (original.tag == f"{{{ML}}}apply" and len(original) == 3
                            and original[0].tag == f"{{{ML}}}scale"
                            and original[2].tag == f"{{{ML}}}id"
                            and original[2].get("labels") == "UNIT"):
                        wrapped = ET.Element(f"{{{ML}}}apply")
                        ET.SubElement(wrapped, f"{{{ML}}}scale")
                        wrapped.append(replacement)
                        wrapped.append(ET.fromstring(ET.tostring(original[2])))
                        replacement = wrapped
                    define.replace(define[1], replacement)
                else:
                    evaluate = math_node.find(f"{{{ML}}}eval")
                    original = next(child for child in evaluate if child.tag != f"{{{ML}}}unitOverride")
                    evaluate.replace(original, replacement)
            if found != set(edits):
                missing = ", ".join(sorted(set(edits) - found))
                raise BusinessError("latex_region_missing", f"工作副本缺少区域：{missing}", 409)
            worksheet = ET.tostring(root, encoding="utf-8")
            with zipfile.ZipFile(temporary, "x") as output:
                for entry in archive.infolist():
                    content = worksheet if entry.filename == "mathcad/worksheet.xml" else archive.read(entry)
                    output.writestr(entry, content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def patch_formulas(path, fields, inputs):
    by_id = {field["id"]: field for field in fields}
    changes = {}
    for field_id, value in inputs.items():
        if value["type"] != "formula":
            continue
        field = by_id[field_id]
        tree = parse_expression(value["value"], field.get("allowed_variables", []))
        if field["region_id"] in changes and expression_key(changes[field["region_id"]][1]) != expression_key(tree):
            raise BusinessError("alias_conflict", "同一区域的公式 alias 值不一致", 422)
        changes[field["region_id"]] = (field["display_name"], tree)
    if not changes:
        return
    path = Path(path)
    temporary = path.with_name(path.stem + "-formula-patch.mcdx")
    try:
        with zipfile.ZipFile(path) as archive:
            root = xml(archive.read("mathcad/worksheet.xml"))
            found = set()
            for region in root.findall(f".//{{{WS}}}region"):
                region_id = region.get("region-id")
                if region_id not in changes:
                    continue
                if region_id in found:
                    raise BusinessError("formula_region_conflict", "公式区域标识重复", 409)
                found.add(region_id)
                definition = region.find(f"{{{WS}}}math/{{{ML}}}define")
                name, tree = changes[region_id]
                if (definition is None or len(definition) != 2 or definition[0].tag != f"{{{ML}}}id"
                        or (definition[0].text or "").strip() != name):
                    raise BusinessError("formula_region_mismatch", f"{name} 的源定义与字段映射不一致", 409)
                definition.replace(definition[1], to_mathml(tree))
            if found != set(changes):
                raise BusinessError("formula_region_missing", "工作副本缺少公式输入区域", 409)
            from lxml import etree as ET
            worksheet = ET.tostring(root, encoding="utf-8")
            with zipfile.ZipFile(temporary, "x") as output:
                for entry in archive.infolist():
                    content = worksheet if entry.filename == "mathcad/worksheet.xml" else archive.read(entry)
                    output.writestr(entry, content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_saved_formula(field, value, expression_xml):
    try:
        expected = parse_expression(value["value"])
        actual = expression_from_xml(expression_xml, field["display_name"])
        if expression_key(expected) != expression_key(actual):
            raise FormulaError("保存后的公式结构与提交值不一致")
    except FormulaError as exc:
        raise BusinessError("saved_formula_mismatch", f"{field['alias']}：{exc}", 409) from exc
