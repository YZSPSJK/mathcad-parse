import io
import zipfile

import pytest
from lxml import etree as ET
from sqlalchemy import select

from backend import service, worker
from backend.db import CalculationRun, Document, DraftInput, IOField, uid
from backend.formulas import (FormulaError, ML, definition_xml, expression_from_xml, expression_key,
                              format_expression, from_mathml, parse_expression, to_mathml)
from backend.parser import parse, sha
from backend.tests.fixtures import archive, formula_package
from backend.upgrade_formulas import upgrade
from backend.worksheet_inputs import patch_formulas


@pytest.mark.parametrize("expression", [
    "A+2*B+12", "(A+B)*3-6/(B+1)", "A-(B-A)", "A/(B/A)", "-(A+B)/2", "+A", "A+-B",
    "(1e-3 + .5) * A", "变量 + 2 * α", "A × (B − 1)", "（A+B）÷2", "12.00", "-2",
])
def test_expression_mathml_roundtrip_preserves_precedence(expression):
    tree = parse_expression(expression, ["A", "B", "变量", "α"])
    xml = definition_xml("C", tree)
    imported = expression_from_xml(xml, "C", ["A", "B", "变量", "α"])
    assert expression_key(tree) == expression_key(imported)
    assert expression_key(parse_expression(format_expression(imported))) == expression_key(tree)


@pytest.mark.parametrize("expression", [
    "A+B", "C+1", "out+1", "aliasA+1", "sin(A)", "__import__('os')", "A**2", "A//2", "A;B",
    "A[0]", "A.__class__", "2A", "(A+1", "A+", "1e999", "1e-999", "A=1", "", "NaN",
])
def test_expression_rejects_invalid_or_nonpreceding_variables(expression):
    with pytest.raises(FormulaError):
        parse_expression(expression, ["A"])


def test_expression_limits_and_unsupported_xml():
    with pytest.raises(FormulaError):
        parse_expression("(" * 40 + "A" + ")" * 40, ["A"])
    with pytest.raises(FormulaError):
        parse_expression("A" * 1025, ["A"])
    with pytest.raises(FormulaError):
        expression_from_xml('<!DOCTYPE x [<!ENTITY value SYSTEM "file:///C:/private">]><x>&value;</x>')
    with pytest.raises(FormulaError):
        expression_from_xml('<real xmlns="urn:foreign">12</real>')
    with pytest.raises(FormulaError):
        expression_from_xml(definition_xml("Other", parse_expression("A")), "C", ["A"])
    with pytest.raises(FormulaError):
        from_mathml(ET.fromstring(f'<apply xmlns="{ML}"><pow/><real>2</real><real>3</real></apply>'))
    with pytest.raises(FormulaError):
        from_mathml(ET.fromstring(f'<apply xmlns="{ML}"><plus/>{"<real>1</real>" * 40}</apply>'))


def test_source_formula_is_editable_and_alias_is_not_variable_name():
    parsed = parse(formula_package())
    formula = parsed.fields[2]
    assert formula["value_type"] == "formula" and formula["supported"]
    assert formula["saved_value"]["value"] == "A + 2 * B + 12"
    assert formula["saved_value"]["allowed_variables"] == ["A", "B"]
    assert formula["alias"] == "aliasC" and formula["display_name"] == "C"
    assert parsed.fields[-1]["direction"] == "output" and parsed.fields[-1]["saved_value"]["value"] == "30"


def test_forward_and_self_reference_are_readonly_on_upload():
    with zipfile.ZipFile(io.BytesIO(formula_package())) as source:
        parts = {p: source.read(p) for p in source.namelist()}
    parts["mathcad/worksheet.xml"] = parts["mathcad/worksheet.xml"].replace(
        b'<m:id labels="VARIABLE">A</m:id><m:apply>', b'<m:id labels="VARIABLE">C</m:id><m:apply>')
    formula = parse(archive(parts)).fields[2]
    assert not formula["supported"] and "前置输入变量" in formula["reason"]


def uploaded_formula(session):
    document_id = service.upload(session, "formula.mcdx", formula_package())["id"]
    doc = service.view(session, document_id, True)
    field = next(f for f in doc["fields"] if f["value_type"] == "formula")
    return doc, field


def test_formula_validation_conversion_and_frozen_job_snapshot(session):
    doc, field = uploaded_formula(session)
    converted = service.convert_formula(session, doc["id"], field["id"], expression="(A+B)*3-6/(B+1)")
    assert converted["references"] == ["A", "B"] and "<ml:div/>" in converted["xml"]
    assert service.convert_formula(session, doc["id"], field["id"], xml_content=converted["xml"])["expression"] == converted["expression"]
    session.rollback()
    doc["inputs"][field["id"]]["value"] = "(A+B)*3-6/(B+1)"
    job = service.submit_calculation(session, doc["id"], doc["draft_version"], doc["inputs"])
    run = session.scalar(select(CalculationRun).where(CalculationRun.document_id == doc["id"]))
    assert run.inputs[field["id"]]["value"] == "(A+B)*3-6/(B+1)"
    assert run.configuration["generator"].endswith("formulas")
    assert session.get(Document, doc["id"]).active_job == job["job_id"]


def test_formula_draft_rejects_future_variables_without_changing_saved_draft(session):
    doc, field = uploaded_formula(session)
    doc["inputs"][field["id"]]["value"] = "A + C"
    session.rollback()
    with pytest.raises(service.BusinessError, match="前置输入变量"):
        service.save_inputs(session, doc["id"], doc["draft_version"], doc["inputs"])
    session.rollback()
    assert session.get(DraftInput, field["id"]).value["value"] == "A + 2 * B + 12"


def test_patch_only_changes_working_worksheet_and_saved_formula_is_checked(session, tmp_path):
    doc, field = uploaded_formula(session)
    source = session.get(Document, doc["id"])
    original_hash = sha(open(source.source_path, "rb").read())
    path = tmp_path / "working.mcdx"
    path.write_bytes(formula_package())
    fields = [{**f, "allowed_variables": f["allowed_variables"]} for f in doc["fields"]]
    doc["inputs"][field["id"]]["value"] = "(A+B)*3-6/(B+1)"
    before = parse(path.read_bytes()).parts
    patch_formulas(path, fields, doc["inputs"])
    after = parse(path.read_bytes()).parts
    assert all(after[name] == data for name, data in before.items() if name != "mathcad/worksheet.xml")
    assert sha(open(source.source_path, "rb").read()) == original_hash
    worker.validate_saved(path, fields, doc["inputs"])
    doc["inputs"][field["id"]]["value"] = "A+B"
    with pytest.raises(service.BusinessError, match="公式结构"):
        worker.validate_saved(path, fields, doc["inputs"])


def test_upgrade_preserves_scalar_drafts_and_old_run_without_false_dirty_state(session):
    doc, field = uploaded_formula(session)
    scalar = next(f for f in doc["fields"] if f["alias"] == "aliasA")
    scalar_draft = session.get(DraftInput, scalar["id"])
    scalar_draft.value = {"type": "real", "value": "14", "units": ""}
    old_formula = session.get(IOField, field["id"])
    old_formula.value_type, old_formula.supported = "unsupported", False
    session.delete(session.get(DraftInput, field["id"]))
    old_inputs = {key: value for key, value in doc["inputs"].items() if key != field["id"]}
    old_inputs[scalar["id"]] = scalar_draft.value
    run = CalculationRun(id=uid(), document_id=doc["id"], revision=1, draft_version=0,
                         inputs=old_inputs, outputs={}, status="succeeded", configuration={})
    session.add(run)
    session.get(Document, doc["id"]).latest_revision = 1
    session.commit()
    assert upgrade(session) == [doc["id"]]
    refreshed = service.view(session, doc["id"], True)
    assert refreshed["inputs"][scalar["id"]]["value"] == "14"
    assert not refreshed["dirty"] and refreshed["draft_version"] == 1
    assert field["id"] not in run.inputs
    session.rollback()
    assert upgrade(session) == []


def test_formula_semantic_comparison_keeps_right_parentheses():
    assert service.same_values({"type": "formula", "value": "A + (B)", "units": ""},
                               {"type": "formula", "value": "A+B", "units": ""})
    assert not service.same_values({"type": "formula", "value": "A / (B / A)", "units": ""},
                                   {"type": "formula", "value": "A / B / A", "units": ""})
    assert expression_key(parse_expression("-2")) == expression_key(from_mathml(to_mathml(parse_expression("-2"))))
