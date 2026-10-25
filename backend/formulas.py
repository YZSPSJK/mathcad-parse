"""受限算术公式与 Mathcad MathML 的双向转换；只解析结构，不执行计算。"""
import math
import re
from dataclasses import dataclass
from decimal import Decimal

from lxml import etree as ET

ML = "http://schemas.mathsoft.com/math50"
WS = "http://schemas.mathsoft.com/worksheet50"
MAX_LENGTH, MAX_NODES, MAX_DEPTH = 1024, 256, 32
NAME = re.compile(r"(?:[^\W\d]|_)\w*", re.UNICODE)
TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d*)?|\.\d+)(?:([eE][+-]?\d+))?|((?:[^\W\d]|_)\w*)|([+*/()\-]))", re.UNICODE)
OPERATORS = {"plus": "+", "minus": "-", "mult": "*", "scale": "*", "div": "/"}
XML_OPERATORS = {"+": "plus", "-": "minus", "*": "mult", "/": "div"}


class FormulaError(ValueError):
    pass


@dataclass(frozen=True)
class Expression:
    kind: str
    value: str
    children: tuple["Expression", ...] = ()


def number(text):
    if len(text) > 128:
        raise FormulaError("数字过长")
    value = float(text)
    if not math.isfinite(value) or (value == 0 and Decimal(text) != 0):
        raise FormulaError("公式中的数字必须在有限实数范围内")
    return Expression("number", text)


def references(tree):
    return sorted({tree.value} if tree.kind == "variable" else {
        name for child in tree.children for name in references(child)})


def validate_references(tree, allowed_variables):
    if allowed_variables is not None:
        invalid = set(references(tree)) - set(allowed_variables)
        if invalid:
            raise FormulaError(f"只能引用前置输入变量，未允许的变量：{'、'.join(sorted(invalid))}")
    return tree


def check_depth(tree, depth=0):
    if depth > MAX_DEPTH:
        raise FormulaError("公式嵌套过深")
    for child in tree.children:
        check_depth(child, depth + 1)


def parse_expression(text, allowed_variables=None):
    if not isinstance(text, str) or not text.strip():
        raise FormulaError("请输入公式")
    if len(text) > MAX_LENGTH:
        raise FormulaError(f"公式不能超过 {MAX_LENGTH} 个字符")
    text = text.translate(str.maketrans({"×": "*", "÷": "/", "（": "(", "）": ")", "−": "-"})).strip()
    tokens, position = [], 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if match is None:
            raise FormulaError(f"第 {position + 1} 个字符附近无法识别，仅支持数字、变量、+ - * / 和括号")
        numeric, exponent, variable, operator = match.groups()
        tokens.append(("number", numeric + (exponent or "")) if numeric is not None else
                      ("variable", variable) if variable is not None else ("operator", operator))
        if len(tokens) > MAX_NODES:
            raise FormulaError("公式过于复杂")
        position = match.end()
    index = 0

    def peek():
        return tokens[index][1] if index < len(tokens) else None

    def atom(depth):
        nonlocal index
        if depth > MAX_DEPTH:
            raise FormulaError("公式嵌套过深")
        token = peek()
        if token in {"+", "-"}:
            index += 1
            child = atom(depth + 1)
            return child if token == "+" else Expression("unary", "-", (child,))
        if token == "(":
            index += 1
            result = additive(depth + 1)
            if peek() != ")":
                raise FormulaError("缺少右括号")
            index += 1
            return result
        if index >= len(tokens) or tokens[index][0] not in {"number", "variable"}:
            raise FormulaError("运算符后需要数字、前置变量或括号表达式")
        kind, value = tokens[index]
        index += 1
        return number(value) if kind == "number" else Expression("variable", value)

    def multiplicative(depth):
        nonlocal index
        result = atom(depth)
        while peek() in {"*", "/"}:
            operator = peek()
            index += 1
            result = Expression("binary", operator, (result, atom(depth)))
        return result

    def additive(depth):
        nonlocal index
        result = multiplicative(depth)
        while peek() in {"+", "-"}:
            operator = peek()
            index += 1
            result = Expression("binary", operator, (result, multiplicative(depth)))
        return result

    result = additive(0)
    if index != len(tokens):
        raise FormulaError("缺少运算符或存在多余括号；乘法请使用 *")
    # 连续二元运算也会产生深树，统一限制后续 XML 序列化的递归深度。
    check_depth(result)
    if len(format_expression(result)) > MAX_LENGTH:
        raise FormulaError(f"格式化后的公式不能超过 {MAX_LENGTH} 个字符")
    return validate_references(result, allowed_variables)


def from_mathml(node, allowed_variables=None):
    count = 0

    def read(item, depth=0):
        nonlocal count
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise FormulaError("公式 XML 过于复杂")
        tag = ET.QName(item)
        if tag.namespace != ML:
            raise FormulaError("公式节点的命名空间不受支持")
        if tag.localname == "real" and not len(item):
            text = (item.text or "").strip()
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
                raise FormulaError("公式 XML 中不是有限实数")
            return number(text)
        if tag.localname == "id" and not len(item) and item.get("labels", "VARIABLE") == "VARIABLE":
            name = (item.text or "").strip()
            if not NAME.fullmatch(name):
                raise FormulaError("暂不支持带特殊排版或空格的变量名")
            return Expression("variable", name)
        if tag.localname == "parens" and len(item) == 1:
            if item.get("unpaired", "false").lower() == "true":
                raise FormulaError("不支持未闭合括号")
            return read(item[0], depth + 1)
        if tag.localname == "apply" and len(item) >= 2 and not len(item[0]):
            operation = ET.QName(item[0])
            if operation.namespace != ML:
                raise FormulaError("运算符命名空间不受支持")
            if operation.localname == "neg" and len(item) == 2:
                return Expression("unary", "-", (read(item[1], depth + 1),))
            operator = OPERATORS.get(operation.localname)
            if operator and (len(item) == 3 or operator in {"+", "*"} and len(item) > 3):
                children = [read(child, depth + 1) for child in item[1:]]
                result = children[0]
                for child in children[1:]:
                    result = Expression("binary", operator, (result, child))
                return result
        raise FormulaError("仅支持加减乘除、括号和前置输入变量，当前 XML 含其他结构")

    result = read(node)
    check_depth(result)
    if len(format_expression(result)) > MAX_LENGTH:
        raise FormulaError(f"公式不能超过 {MAX_LENGTH} 个字符")
    return validate_references(result, allowed_variables)


def format_expression(tree):
    def priority(item):
        return 1 if item.kind == "binary" and item.value in {"+", "-"} else (
            2 if item.kind == "binary" else 3 if item.kind == "unary" else 4)

    if tree.kind in {"number", "variable"}:
        return tree.value
    if tree.kind == "unary":
        child = tree.children[0]
        text = format_expression(child)
        return "-" + (f"({text})" if priority(child) <= 3 else text)
    left, right = tree.children
    left_text, right_text = format_expression(left), format_expression(right)
    if priority(left) < priority(tree):
        left_text = f"({left_text})"
    if priority(right) <= priority(tree):
        right_text = f"({right_text})"
    return f"{left_text} {tree.value} {right_text}"


def to_mathml(tree):
    if tree.kind == "number":
        node = ET.Element(f"{{{ML}}}real")
        node.text = tree.value
        return node
    if tree.kind == "variable":
        node = ET.Element(f"{{{ML}}}id", labels="VARIABLE", **{"label-is-contextual": "true"})
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        node.text = tree.value
        return node
    node = ET.Element(f"{{{ML}}}apply")
    ET.SubElement(node, f"{{{ML}}}{'neg' if tree.kind == 'unary' else XML_OPERATORS[tree.value]}")
    node.extend(to_mathml(child) for child in tree.children)
    return node


def expression_from_xml(content, variable_name=None, allowed_variables=None):
    if not isinstance(content, str) or len(content) > 16000:
        raise FormulaError("公式 XML 不能为空或超过16000字符")
    try:
        parser = ET.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                              remove_comments=True, remove_pis=True, huge_tree=False)
        root = ET.fromstring(content.encode("utf-8"), parser)
    except ET.XMLSyntaxError as exc:
        raise FormulaError(f"公式 XML 格式错误：{exc}") from exc
    if root.getroottree().docinfo.doctype:
        raise FormulaError("不允许 XML DTD 或实体声明")
    if root.tag == f"{{{WS}}}math":
        root = root.find(f"{{{ML}}}define")
        if root is None:
            raise FormulaError("需要当前输入的 define XML 或右侧表达式 XML")
    if root.tag == f"{{{ML}}}define":
        if len(root) != 2 or root[0].tag != f"{{{ML}}}id" or len(root[0]):
            raise FormulaError("仅支持单个变量定义")
        if variable_name and (root[0].text or "").strip() != variable_name:
            raise FormulaError("不能通过 XML 修改当前输入变量的名称")
        root = root[1]
    return from_mathml(root, allowed_variables)


def definition_xml(variable_name, tree):
    node = ET.Element(f"{{{ML}}}define", nsmap={"ml": ML})
    identifier = ET.SubElement(node, f"{{{ML}}}id", labels="VARIABLE")
    identifier.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    identifier.text = variable_name
    node.append(to_mathml(tree))
    return ET.tostring(node, encoding="unicode", pretty_print=True)


def expression_key(tree):
    value = Decimal(tree.value) if tree.kind == "number" else tree.value
    children = tuple(expression_key(child) for child in tree.children)
    if tree.kind == "unary" and children[0][0] == "number":
        return "number", children[0][1].copy_negate(), ()
    return tree.kind, value, children
