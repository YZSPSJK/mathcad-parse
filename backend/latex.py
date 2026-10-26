"""Safe, deliberately small LaTeX ↔ Mathcad MathML conversion for the alternate editor."""
import re

from lxml import etree as ET

from backend.formulas import (Expression, FormulaError, ML, TOKEN, WS, format_expression,
                              from_mathml, parse_expression, to_mathml)

MAX_LATEX = 1024
GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "phi": "φ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Sigma": "Σ", "Phi": "Φ", "Omega": "Ω",
}
GREEK_LATEX = {value: rf"\{key}" for key, value in GREEK.items()}


def _group(text: str, start: int, opening: str, closing: str) -> tuple[str, int]:
    if start >= len(text) or text[start] != opening:
        raise FormulaError("LaTeX 分数需要使用花括号分组")
    depth = 1
    for end in range(start + 1, len(text)):
        if text[end] == opening:
            depth += 1
        elif text[end] == closing:
            depth -= 1
            if depth == 0:
                return text[start + 1:end], end + 1
    raise FormulaError("LaTeX 中存在未闭合的分组")


def _normalize(text: str, depth=0) -> str:
    if depth > 32:
        raise FormulaError("LaTeX 嵌套过深")
    output, index = [], 0
    while index < len(text):
        if text.startswith("\\frac", index):
            index += 5
            while index < len(text) and text[index].isspace():
                index += 1
            numerator, index = _group(text, index, "{", "}")
            while index < len(text) and text[index].isspace():
                index += 1
            denominator, index = _group(text, index, "{", "}")
            output.append(f"(({_normalize(numerator, depth + 1)})/({_normalize(denominator, depth + 1)}))")
            continue
        if text[index] == "\\":
            match = re.match(r"\\([A-Za-z]+|.)", text[index:])
            if not match:
                raise FormulaError("无法识别 LaTeX 转义")
            command = match.group(1)
            index += len(match.group(0))
            if command in {"left", "right"}:
                continue
            if command in {",", ";", ":", "!", " "}:
                continue
            if command in {"cdot", "times"}:
                output.append("*")
                continue
            if command == "div":
                output.append("/")
                continue
            if command in GREEK:
                output.append(GREEK[command])
                continue
            raise FormulaError(f"暂不支持 LaTeX 命令 \\{command}")
        char = text[index]
        if char == "{" or char == "}":
            raise FormulaError("仅支持用括号或分数分组的表达式")
        output.append({"×": "*", "÷": "/", "−": "-", "（": "(", "）": ")"}.get(char, char))
        index += 1
    return "".join(output)


def parse_latex(latex: str, allowed_variables=None) -> Expression:
    if not isinstance(latex, str) or not latex.strip():
        raise FormulaError("请输入 LaTeX 表达式")
    if len(latex) > MAX_LATEX:
        raise FormulaError(f"LaTeX 表达式不能超过 {MAX_LATEX} 个字符")
    normalized = _normalize(latex.strip())
    # MathLive commonly emits implicit multiplication (``2x`` or ``2(x+1)``).
    # Tokenize before inserting separators so scientific notation remains one number.
    tokens, position = [], 0
    while position < len(normalized):
        match = TOKEN.match(normalized, position)
        if match is None:
            break
        numeric, exponent, variable, operator = match.groups()
        if numeric is not None:
            tokens.append(("number", numeric + (exponent or "")))
        elif variable is not None:
            tokens.append(("variable", variable))
        else:
            tokens.append(("operator", operator))
        position = match.end()
    if position == len(normalized) and tokens:
        parts = []
        previous = None
        for kind, value in tokens:
            starts_operand = kind in {"number", "variable"} or value == "("
            ends_operand = previous and (previous[0] in {"number", "variable"} or previous[1] == ")")
            if ends_operand and starts_operand:
                parts.append("*")
            parts.append(value)
            previous = (kind, value)
        normalized = " ".join(parts)
    tree = parse_expression(normalized, allowed_variables)
    return tree


def to_latex(tree: Expression) -> str:
    def precedence(node):
        if node.kind == "binary":
            return 1 if node.value in {"+", "-"} else 2
        return 3 if node.kind == "unary" else 4

    if tree.kind == "number":
        return tree.value
    if tree.kind == "variable":
        return GREEK_LATEX.get(tree.value, tree.value)
    if tree.kind == "unary":
        child = tree.children[0]
        text = to_latex(child)
        return "-" + (rf"\left({text}\right)" if precedence(child) <= 3 else text)
    left, right = tree.children
    if tree.value == "/":
        return rf"\frac{{{to_latex(left)}}}{{{to_latex(right)}}}"
    left_text, right_text = to_latex(left), to_latex(right)
    if precedence(left) < precedence(tree):
        left_text = rf"\left({left_text}\right)"
    if precedence(right) <= precedence(tree):
        right_text = rf"\left({right_text}\right)"
    operator = r"\cdot" if tree.value == "*" else tree.value
    return f"{left_text} {operator} {right_text}"


def mathml_expression(tree: Expression) -> str:
    return ET.tostring(to_mathml(tree), encoding="unicode")


def _region_math(expression_xml: str):
    parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                          remove_comments=True, remove_pis=True, huge_tree=False)
    try:
        root = ET.fromstring(expression_xml.encode("utf-8"), parser)
    except ET.XMLSyntaxError as exc:
        raise FormulaError("工作表区域 XML 无效") from exc
    if root.getroottree().docinfo.doctype:
        raise FormulaError("不允许 XML DTD 或实体声明")
    if root.tag != f"{{{WS}}}math":
        raise FormulaError("区域不是 Mathcad 数学表达式")
    define = root.find(f"{{{ML}}}define")
    if define is not None and len(define) == 2 and define[0].tag == f"{{{ML}}}id":
        name = (define[0].text or "").strip()
        if not name or len(define[0]):
            raise FormulaError("变量定义格式不受支持")
        return root, "define", name, define[1]
    evaluate = root.find(f"{{{ML}}}eval")
    if evaluate is not None and len(evaluate) >= 1:
        expression = next((child for child in evaluate if child.tag != f"{{{ML}}}unitOverride"), None)
        if expression is not None:
            return root, "eval", "", expression
    raise FormulaError("仅支持单个变量定义或求值区域")


def region_math_from_latex(expression_xml: str, latex: str) -> str:
    root, target, _name, _expression = _region_math(expression_xml)
    tree = parse_latex(latex)
    replacement = to_mathml(tree)
    if target == "define":
        define = root.find(f"{{{ML}}}define")
        original = define[1]
        if (original.tag == f"{{{ML}}}apply" and len(original) == 3
                and original[0].tag == f"{{{ML}}}scale" and original[2].tag == f"{{{ML}}}id"
                and original[2].get("labels") == "UNIT"):
            wrapped = ET.Element(f"{{{ML}}}apply")
            ET.SubElement(wrapped, f"{{{ML}}}scale")
            wrapped.append(replacement)
            wrapped.append(ET.fromstring(ET.tostring(original[2])))
            replacement = wrapped
        define.replace(define[1], replacement)
    else:
        evaluate = root.find(f"{{{ML}}}eval")
        original = next(child for child in evaluate if child.tag != f"{{{ML}}}unitOverride")
        evaluate.replace(original, replacement)
    return ET.tostring(root, encoding="unicode")


def region_to_latex(expression_xml: str, allowed_variables=None) -> dict:
    _root, target, name, expression = _region_math(expression_xml)
    unit = ""
    if (expression.tag == f"{{{ML}}}apply" and len(expression) == 3
            and expression[0].tag == f"{{{ML}}}scale"
            and expression[2].tag == f"{{{ML}}}id" and expression[2].get("labels") == "UNIT"):
        unit = "".join(expression[2].itertext()).strip()
        expression = expression[1]
    tree = from_mathml(expression, allowed_variables)
    return {"latex": to_latex(tree), "display_name": name, "target": target, "units": unit,
            "expression_xml": expression_xml, "infix": format_expression(tree)}


def region_target(expression_xml: str) -> tuple[str, str]:
    _root, target, name, _expression = _region_math(expression_xml)
    return target, name


def validate_region_expression(expression_xml: str, allowed_variables=None) -> str:
    parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                          remove_comments=True, remove_pis=True, huge_tree=False)
    try:
        node = ET.fromstring(expression_xml.encode("utf-8"), parser)
    except ET.XMLSyntaxError as exc:
        raise FormulaError("生成的 Mathcad 表达式 XML 无效") from exc
    if node.getroottree().docinfo.doctype:
        raise FormulaError("不允许 XML DTD 或实体声明")
    tree = from_mathml(node, allowed_variables)
    return mathml_expression(tree)
