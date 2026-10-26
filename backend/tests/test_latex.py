import zipfile
from pathlib import Path

from lxml import etree as ET
from sqlalchemy import select
from fastapi.testclient import TestClient

from backend import service, worker
from backend.db import Artifact, Job, SourcePart
from backend.formulas import expression_from_xml, format_expression
from backend.latex import parse_latex, to_latex
from backend.main import app
from backend.parser import ML, WS, parse, sha
from backend.tests.fixtures import formula_package, package
from backend.tests.test_service import TestPrime


def test_latex_and_mathml_roundtrip():
    tree = parse_latex(r"\frac{a + 2}{b \cdot 3}", ["a", "b"])
    latex = to_latex(tree)
    assert latex == r"\frac{a + 2}{b \cdot 3}"
    from backend.latex import mathml_expression
    imported = expression_from_xml(mathml_expression(tree), allowed_variables=["a", "b"])
    assert format_expression(imported) == "(a + 2) / (b * 3)"
    assert format_expression(parse_latex("2x", ["x"])) == "2 * x"


def test_latex_rejects_unknown_commands_and_variables():
    import pytest
    from backend.formulas import FormulaError

    with pytest.raises(FormulaError, match="不支持 LaTeX 命令"):
        parse_latex(r"\sqrt{x}")
    with pytest.raises(FormulaError, match="未允许的变量"):
        parse_latex("x + 1", ["a"])


def test_region_conversion_preserves_definition_name(session):
    document_id = service.upload(session, "formula.mcdx", package())["id"]
    data = service.latex_workspace(session, document_id)
    region = next(region for region in data["regions"] if region["region_id"] == "42")
    assert region["editable"] and region["latex"] == "10" and region["display_name"] == "output1"
    saved = service.save_latex_region(session, document_id, "42", data["latex_version"], "12 + 3")
    assert saved["latex_version"] == 1
    loaded = service.latex_workspace(session, document_id)
    updated = next(region for region in loaded["regions"] if region["region_id"] == "42")
    assert updated["latex"] == "12 + 3" and updated["saved_draft"]


def test_latex_formula_region_only_allows_prior_variables(session):
    document_id = service.upload(session, "formula.mcdx", formula_package())["id"]
    workspace = service.latex_workspace(session, document_id)
    c = next(region for region in workspace["regions"] if region["region_id"] == "2")
    assert c["allowed_variables"] == ["A", "B"] and c["latex"]
    service.save_latex_region(session, document_id, "2", workspace["latex_version"], r"\frac{A+B}{3}")
    with __import__("pytest").raises(service.BusinessError, match="未允许的变量"):
        service.save_latex_region(session, document_id, "2", 1, "out + 1")


def test_latex_workspace_can_calculate_without_formula_edits(session):
    document_id = service.upload(session, "baseline.mcdx", package())["id"]
    data = service.latex_workspace(session, document_id)
    result = service.submit_latex_calculation(session, document_id, data["latex_version"])
    job = session.get(Job, result["job_id"])
    assert result["revision"] == 1 and job.kind == "latex_calculate"


def test_latex_calculation_patches_copy_publishes_results_and_exports(session):
    original = package()
    document_id = service.upload(session, "formula.mcdx", original)["id"]
    data = service.latex_workspace(session, document_id)
    service.save_latex_region(session, document_id, "42", data["latex_version"], "12 + 3")
    data = service.latex_workspace(session, document_id)
    result = service.submit_latex_calculation(session, document_id, data["latex_version"])
    assert result["revision"] == 1
    assert worker.claim() == result["job_id"]
    worker.execute(result["job_id"], prime_factory=TestPrime)
    session.expire_all()

    loaded = service.latex_workspace(session, document_id)
    region = next(item for item in loaded["regions"] if item["region_id"] == "42")
    assert loaded["latest_revision"] == 1 and not loaded["latex_dirty"]
    assert region["latex"] == "12 + 3"
    assert region["value"]["value"] == "15"
    assert loaded["fields"][-1]["value"]["value"] == "20"

    artifact = session.scalar(select(Artifact).where(Artifact.document_id == document_id,
                                                  Artifact.revision == 1, Artifact.kind == "mcdx"))
    assert artifact and sha(Path(service.get_document(session, document_id).source_path).read_bytes()) == sha(original)
    with zipfile.ZipFile(artifact.path) as archive:
        worksheet = ET.fromstring(archive.read("mathcad/worksheet.xml"))
    region_math = worksheet.find(f".//{{{WS}}}region[@region-id='42']/{{{WS}}}math")
    definition = region_math.find(f"{{{ML}}}define")
    assert definition[0].text == "output1"
    assert definition[1].tag == f"{{{ML}}}apply"
    assert "plus" in ET.tostring(definition[1], encoding="unicode")

    source = session.scalars(select(SourcePart).where(SourcePart.document_id == document_id,
                                                       SourcePart.snapshot == "upload",
                                                       SourcePart.path == "mathcad/worksheet.xml")).one()
    assert source.content == parse(original).parts["mathcad/worksheet.xml"]


def test_latex_api_uses_csrf_and_persists_region_draft():
    with TestClient(app) as client:
        token = client.get("/api/csrf").json()["token"]
        uploaded = client.post("/api/documents", files={"file": ("latex.mcdx", package())},
                               headers={"x-csrf-token": token})
        assert uploaded.status_code == 201
        document_id = uploaded.json()["id"]
        workspace = client.get(f"/api/documents/{document_id}/latex")
        assert workspace.status_code == 200
        saved = client.put(f"/api/documents/{document_id}/latex/regions/42",
                           json={"expected_version": 0, "latex": "12+3"},
                           headers={"x-csrf-token": token})
        assert saved.status_code == 200 and saved.json()["latex_version"] == 1
        refreshed = client.get(f"/api/documents/{document_id}/latex").json()
        assert next(region for region in refreshed["regions"] if region["region_id"] == "42")["latex"] == "12+3"
        submitted = client.post(f"/api/documents/{document_id}/latex/calculate",
                                json={"expected_version": 1}, headers={"x-csrf-token": token})
        assert submitted.status_code == 202 and submitted.json()["revision"] == 1
