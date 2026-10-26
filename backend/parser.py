"""MCDX 只读解析。未知 XML/ZIP 部件原样保留，不生成或猜测计算结果。"""
import hashlib
import io
import math
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import PurePosixPath

from lxml import etree as ET

from backend.config import settings
from backend.formulas import FormulaError, NAME, format_expression, from_mathml

WS = "http://schemas.mathsoft.com/worksheet50"
ML = "http://schemas.mathsoft.com/math50"
RESULT = "http://schemas.mathsoft.com/result10"
INTEGRATION = "http://schemas.ptc.com/integration10"
UNITS = "http://schemas.mathsoft.com/units10"
APP = "http://schemas.mathsoft.com/extended-properties"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": WS, "m": ML, "r": RESULT, "i": INTEGRATION, "u": UNITS}
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class ParseError(ValueError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def xml(data: bytes):
    parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                          huge_tree=False, recover=False, remove_comments=True, remove_pis=True)
    root = ET.fromstring(data, parser)
    if root.getroottree().docinfo.doctype:
        raise ParseError("不允许 XML DTD/实体声明")
    return root


def finite(value: str) -> bool:
    if not NUMBER.fullmatch(value.strip()):
        return False
    number = float(value)
    return math.isfinite(number) and (number != 0 or Decimal(value) == 0)


def unsupported(reason: str, raw: str = "", status="unsupported") -> dict:
    return {"type": "unsupported", "value": None, "units": "", "status": status,
            "reason": reason, "raw_xml": raw}


def numeric(node, matrix_order: str | None = None) -> dict:
    if node is None:
        return unsupported("尚无有效结果", status="missing")
    raw = ET.tostring(node, encoding="unicode")
    tag = ET.QName(node)
    if tag.namespace != ML:
        return unsupported("未知数值命名空间", raw)
    if tag.localname == "real" and not len(node) and not node.attrib and finite(node.text or ""):
        return {"type": "real", "value": node.text.strip(), "units": "", "status": "saved"}
    if tag.localname == "matrix":
        try:
            rows, cols = int(node.get("rows", "0")), int(node.get("cols", "0"))
            cells = list(node)
            if rows <= 0 or cols <= 0 or rows * cols > 10000 or len(cells) != rows * cols:
                raise ValueError()
            # Math50 的顺序必须先由本机 Prime 样本验证；未知顺序绝不推测。
            if matrix_order not in {"column-major", "row-major"}:
                return unsupported("矩阵 XML 单元格顺序尚未验证", raw)
            values = [numeric(c, matrix_order) for c in cells]
            unit_set = {c["units"] for c in values}
            if any(c["type"] != "real" for c in values) or len(unit_set) != 1:
                return unsupported("不支持复数、混合量纲或非数值矩阵", raw)
            return {"type": "matrix", "value": [[values[c * rows + r if matrix_order == "column-major" else r * cols + c]["value"]
                    for c in range(cols)] for r in range(rows)], "rows": rows, "cols": cols,
                    "units": unit_set.pop(), "status": "saved"}
        except (ValueError, TypeError):
            return unsupported("矩阵维度或单元格数量无效", raw)
    if tag.localname == "apply" and len(node) == 3 and node[0].tag == f"{{{ML}}}scale":
        value = numeric(node[1], matrix_order)
        unit = node[2]
        if unit.tag == f"{{{ML}}}id" and unit.get("labels") == "UNIT" and value["type"] != "unsupported" and not value["units"]:
            return {**value, "units": "".join(unit.itertext()).strip()}
    return unsupported("非有限实数、复杂表达式、文本或单位结构暂不支持", raw)


@dataclass
class Parsed:
    parts: dict[str, bytes]
    regions: list[dict] = field(default_factory=list)
    fields: list[dict] = field(default_factory=list)
    result_values: dict[str, dict] = field(default_factory=dict)
    app_info: dict = field(default_factory=dict)
    diagnostics: list[dict] = field(default_factory=list)


def parse(data: bytes, *, matrix_order: str | None = None) -> Parsed:
    cfg = settings()
    if len(data) > cfg.max_upload_mb * 1024**2:
        raise ParseError("上传文件超过大小限制")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > cfg.max_zip_entries:
                raise ParseError("ZIP 部件数量超限")
            total, names = 0, set()
            for item in entries:
                path = PurePosixPath(item.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                        or ":" in item.filename or item.filename in names or item.flag_bits & 1
                        or (item.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ParseError("ZIP 包含非法、重复、加密或链接路径")
                names.add(item.filename)
                total += item.file_size
                if total > cfg.max_uncompressed_mb * 1024**2:
                    raise ParseError("ZIP 解压总量超限")
            required = {"mathcad/worksheet.xml", "[Content_Types].xml", "_rels/.rels"}
            if not required <= names:
                raise ParseError("不是完整的 MCDX：缺少工作表、类型声明或入口关系")
            parts = {entry.filename: archive.read(entry) for entry in entries if not entry.is_dir()}
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        raise ParseError(f"无法读取 MCDX ZIP：{exc}") from exc
    parsed = Parsed(parts=parts)
    roots = {}
    for path, content in parts.items():
        if path.endswith((".xml", ".rels")):
            try:
                roots[path] = xml(content)
            except (ET.XMLSyntaxError, ParseError) as exc:
                if path in required:
                    raise ParseError(f"{path}：{exc}") from exc
                parsed.diagnostics.append({"part": path, "message": str(exc)})
    worksheet = roots["mathcad/worksheet.xml"]
    if worksheet.tag != f"{{{WS}}}worksheet":
        raise ParseError("不支持的 worksheet 命名空间/版本")
    types, relationships = roots["[Content_Types].xml"], roots["_rels/.rels"]
    if types.tag != f"{{{CONTENT_TYPES}}}Types" or relationships.tag != f"{{{RELATIONSHIPS}}}Relationships":
        raise ParseError("包的类型声明或关系命名空间无效")
    if not any(node.get("Target", "").lstrip("/") == "mathcad/worksheet.xml"
               and node.get("TargetMode") != "External"
               for node in relationships.findall(f"{{{RELATIONSHIPS}}}Relationship")):
        raise ParseError("包入口关系未指向工作表")
    if not any(node.get("ContentType") and (node.get("Extension") == "xml"
               or node.get("PartName") == "/mathcad/worksheet.xml") for node in types):
        raise ParseError("包未声明工作表的内容类型")
    app = roots.get("docProps/app.xml")
    if app is not None and app.tag == f"{{{APP}}}properties":
        parsed.app_info = {ET.QName(child).localname: "".join(child.itertext())
                           for child in app if ET.QName(child).namespace == APP}
    results = {}
    result_root = roots.get("mathcad/result.xml")
    if result_root is not None:
        for item in result_root.findall(f"{{{RESULT}}}resultData"):
            rid = item.get("result-id")
            status = item.get("calculation-status", "Unknown")
            raw = ET.tostring(item, encoding="unicode")
            if status.lower() not in {"synchronized", "calculated"}:
                value = unsupported("尚无有效结果" if status.lower() == "pending" else f"计算状态：{status}",
                                    raw, status=status.lower())
            else:
                result = item.find(f"{{{ML}}}result")
                value = numeric(result[0] if result is not None and len(result) == 1 else None, matrix_order)
                if result is not None and len(result) > 1:
                    value = unsupported("复合结果/单位结构暂不支持，保留原始 XML", raw)
                value["raw_xml"] = raw
            results[rid] = value
        parsed.result_values = results
    regions = {}
    previous_variable_names = []
    definitions = {}
    for node in worksheet.findall(f".//{{{WS}}}region"):
        rid = node.get("region-id", "")
        math_node = node.find(f"{{{WS}}}math")
        ref = math_node.get("resultRef") if math_node is not None else None
        expression = ET.tostring(math_node, encoding="unicode") if math_node is not None else ""
        definition = math_node.find(f"{{{ML}}}define") if math_node is not None else None
        variable = ""
        if definition is not None:
            definitions[rid] = definition
            identifier = definition.find(f"{{{ML}}}id")
            variable = (identifier.text or "").strip() if identifier is not None else ""
            allowed_variables = [name for name in previous_variable_names if name != variable]
            if variable and NAME.fullmatch(variable) and variable not in previous_variable_names:
                previous_variable_names.append(variable)
        else:
            allowed_variables = list(previous_variable_names)
        evaluation = math_node.find(f"{{{ML}}}eval") if math_node is not None else None
        identifier = definition.find(f"{{{ML}}}id") if definition is not None else (
            evaluation[0] if evaluation is not None and len(evaluation)
            and evaluation[0].tag == f"{{{ML}}}id" else None)
        display = "".join(identifier.itertext()) if identifier is not None else ""
        literal = numeric(definition[1], matrix_order) if definition is not None and len(definition) == 2 else None
        if literal and literal["type"] == "unsupported" and any(
                identifier.get("labels") != "UNIT" for identifier in definition[1].iter(f"{{{ML}}}id")):
            literal = {**literal, "reason": "输入为公式，文件未保存有效数值，需由 Mathcad 读取"}
        region = {"region_id": rid, "result_ref": ref, "allowed_variables": allowed_variables,
                  "kind": ET.QName(node[0]).localname if len(node) else "unknown",
                  "geometry": dict(node.attrib), "expression_xml": expression}
        parsed.regions.append(region)
        regions[rid] = (region, display, literal)
    integration = roots.get("mathcad/integration.xml")
    if integration is not None and integration.tag == f"{{{INTEGRATION}}}regions":
        seen = set()
        for node in integration.findall(f"{{{INTEGRATION}}}region"):
            rid = node.get("region-id", "")
            if rid not in regions:
                parsed.diagnostics.append({"part": "mathcad/integration.xml", "region_id": rid,
                                           "message": "分配引用的 region 不存在"})
                continue
            region, display, literal = regions[rid]
            for direction in ("input", "output"):
                aliases = node.findall(f"{{{INTEGRATION}}}{direction}Aliases/{{{INTEGRATION}}}alias")
                for alias_node in aliases:
                    alias = (alias_node.text or "").strip()
                    saved = results.get(region["result_ref"])
                    if direction == "input" and literal and literal["type"] in {"real", "matrix"}:
                        # 定义中明确保存的参数值不依赖 resultData 是否存在或含有缓存。
                        value = literal
                    elif saved and saved["type"] in {"real", "matrix"}:
                        value = saved
                    elif direction == "input" and literal:
                        value = literal
                    else:
                        value = saved or unsupported("尚无有效结果", status="missing")
                    conflict = (direction, alias) in seen or not alias
                    seen.add((direction, alias))
                    supported = value["type"] in {"real", "matrix"} and not conflict
                    reason = "alias 冲突或为空" if conflict else value.get("reason")
                    parsed.fields.append({**region, "direction": direction, "alias": alias,
                                          "display_name": display or alias, "value_type": value["type"],
                                          "units": value["units"], "saved_value": value,
                                          "supported": supported, "reason": reason})
                    if conflict:
                        parsed.diagnostics.append({"region_id": rid, "alias": alias, "message": reason})
    # 按工作表区域顺序收集前置输入变量名，使用定义名而非 API alias。
    input_regions = {}
    for item in parsed.fields:
        if item["direction"] == "input":
            input_regions.setdefault(item["region_id"], []).append(item)
    previous_names = []
    for region in parsed.regions:
        fields = input_regions.get(region["region_id"], [])
        definition = definitions.get(region["region_id"])
        if not fields or definition is None or len(definition) != 2 or definition[0].tag != f"{{{ML}}}id":
            continue
        name = (definition[0].text or "").strip()
        allowed = [variable for variable in previous_names if variable != name]
        literal = numeric(definition[1], matrix_order)
        if literal["type"] == "unsupported" and definition[1].tag in {
                f"{{{ML}}}apply", f"{{{ML}}}id", f"{{{ML}}}parens"}:
            try:
                expression = from_mathml(definition[1], allowed)
                for item in fields:
                    saved = item["saved_value"]
                    item.update(value_type="formula", supported=True, reason=None)
                    item["saved_value"] = {
                        "type": "formula", "value": format_expression(expression), "units": item["units"],
                        "status": "saved", "allowed_variables": allowed,
                        "raw_xml": ET.tostring(definition[1], encoding="unicode"),
                        "effective_value": saved if saved["type"] in {"real", "matrix"} else None}
            except FormulaError as exc:
                for item in fields:
                    item["reason"] = f"公式暂不可编辑：{exc}"
        if NAME.fullmatch(name) and name not in previous_names:
            previous_names.append(name)
    counts = Counter((f["direction"], f["alias"]) for f in parsed.fields)
    conflicts = {key for key, count in counts.items() if count > 1}
    for item in parsed.fields:
        if (item["direction"], item["alias"]) in conflicts:
            item["supported"], item["reason"] = False, "alias 冲突"
    return parsed
