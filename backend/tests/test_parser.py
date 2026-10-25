import io
import zipfile

import pytest

from backend.parser import ParseError, finite, parse
from backend.tests.fixtures import archive, package


def test_alias_region_result_mapping_with_changed_prefix():
    parsed = parse(package(prefix="somethingElse", aliases=True))
    inputs = [f for f in parsed.fields if f["direction"] == "input"]
    assert [f["alias"] for f in inputs] == ["parameterAlias", "secondAlias"]
    assert inputs[0]["display_name"] == "output1"
    assert inputs[0]["saved_value"]["value"] == "10"
    output = parsed.fields[-1]
    assert output["region_id"] == "900" and output["result_ref"] == "17"
    assert output["saved_value"]["value"] == "20"
    assert parsed.parts["mathcad/media/unknown.bin"] == b"\x00\x01opaque"


@pytest.mark.parametrize("kwargs,status", [({"pending": True}, "pending"), ({"result": False}, "missing")])
def test_missing_pending_never_become_zero(kwargs, status):
    value = parse(package(**kwargs)).fields[-1]["saved_value"]
    assert value["value"] is None and value["status"] == status


def test_formula_is_not_a_numeric_input():
    field = parse(package(formula=True)).fields[0]
    assert not field["supported"] and field["saved_value"]["value"] is None
    assert "plus" in field["expression_xml"]


@pytest.mark.parametrize("cached", [b"", b"<m:result><m:real>999</m:real></m:result>"])
def test_literal_input_comes_from_definition_despite_empty_or_stale_cache(cached):
    with zipfile.ZipFile(io.BytesIO(package())) as z:
        parts = {p: z.read(p) for p in z.namelist()}
    parts["mathcad/worksheet.xml"] = parts["mathcad/worksheet.xml"].replace(b"<w:math>", b'<w:math resultRef="input-result">', 1)
    parts["mathcad/result.xml"] = parts["mathcad/result.xml"].replace(
        b"</resultsList>", b'<resultData result-id="input-result" calculation-status="Synchronized">' + cached + b"</resultData></resultsList>")
    field = parse(archive(parts)).fields[0]
    assert field["supported"] and field["saved_value"]["value"] == "10"


def test_formula_input_with_empty_synchronized_result_stays_readonly_with_reason():
    with zipfile.ZipFile(io.BytesIO(package(formula=True))) as z:
        parts = {p: z.read(p) for p in z.namelist()}
    parts["mathcad/worksheet.xml"] = parts["mathcad/worksheet.xml"].replace(b"<w:math>", b'<w:math resultRef="input-result">', 1)
    parts["mathcad/result.xml"] = parts["mathcad/result.xml"].replace(
        b"</resultsList>", b'<resultData result-id="input-result" calculation-status="Synchronized"/></resultsList>')
    field = parse(archive(parts)).fields[0]
    assert not field["supported"] and field["saved_value"]["value"] is None
    assert "前置输入变量" in field["reason"] and "plus" in field["saved_value"]["raw_xml"]


def test_compound_output_uses_alias_instead_of_first_variable_name():
    with zipfile.ZipFile(io.BytesIO(package())) as z:
        parts = {p: z.read(p) for p in z.namelist()}
    parts["mathcad/worksheet.xml"] = parts["mathcad/worksheet.xml"].replace(
        b"<m:eval><m:id>answer</m:id></m:eval>",
        b"<m:eval><m:apply><m:plus/><m:id>B</m:id><m:id>C</m:id></m:apply></m:eval>")
    field = parse(archive(parts)).fields[-1]
    assert field["display_name"] == "resultAlias" and field["saved_value"]["value"] == "20"


def test_matrix_order_requires_explicit_evidence():
    assert not parse(package(matrix=True)).fields[-1]["supported"]
    matrix = parse(package(matrix=True), matrix_order="column-major").fields[-1]["saved_value"]
    assert matrix["value"] == [["1", "2", "3"], ["4", "5", "6"]]
    assert matrix["rows"] == 2 and matrix["cols"] == 3


def test_foreign_namespace_does_not_supply_numeric_value():
    data = package()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        parts = {p: z.read(p) for p in z.namelist()}
    parts["mathcad/result.xml"] = parts["mathcad/result.xml"].replace(b"<m:real>20</m:real>", b'<x:real xmlns:x="urn:foreign">20</x:real>')
    assert not parse(archive(parts)).fields[-1]["supported"]


def test_no_io_marks_does_not_guess_definitions():
    with zipfile.ZipFile(io.BytesIO(package())) as z:
        parts = {p: z.read(p) for p in z.namelist() if p != "mathcad/integration.xml"}
    assert parse(archive(parts)).fields == []


@pytest.mark.parametrize("path", ["../evil", "/absolute", "C:/evil", "bad\\file"])
def test_unsafe_zip_paths_rejected(path):
    with pytest.raises(ParseError):
        parse(archive({path: b"x"}))


def test_xml_entity_rejected():
    with zipfile.ZipFile(io.BytesIO(package())) as z:
        parts = {p: z.read(p) for p in z.namelist()}
    parts["mathcad/worksheet.xml"] = b'<!DOCTYPE worksheet [<!ENTITY x SYSTEM "file:///C:/private">]><worksheet>&x;</worksheet>'
    with pytest.raises(ParseError):
        parse(archive(parts))


def test_unknown_result_units_are_not_silently_dropped():
    with zipfile.ZipFile(io.BytesIO(package())) as z:
        parts = {p: z.read(p) for p in z.namelist()}
    parts["mathcad/result.xml"] = parts["mathcad/result.xml"].replace(b"</m:result>", b'<u:units xmlns:u="http://schemas.mathsoft.com/units10">kg</u:units></m:result>')
    field = parse(archive(parts)).fields[-1]
    assert not field["supported"] and "kg" in field["saved_value"]["raw_xml"]


def test_invalid_package_relationship_rejected():
    with zipfile.ZipFile(io.BytesIO(package())) as z:
        parts = {p: z.read(p) for p in z.namelist()}
    parts["_rels/.rels"] = parts["_rels/.rels"].replace(b"/mathcad/worksheet.xml", b"/missing.xml")
    with pytest.raises(ParseError, match="包入口"):
        parse(archive(parts))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e9999", "1e-9999", "2+b", "", "__import__('os')"])
def test_invalid_doubles(value):
    assert not finite(value)
