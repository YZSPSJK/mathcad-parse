import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lxml import etree as ET
from pypdf import PdfWriter
from sqlalchemy import select

from backend import service, worker
from backend.automation import validate_pdf
from backend.db import CalculationRun, Document, Job, SourcePart
from backend.main import app
from backend.parser import ML, parse, sha
from backend.tests.fixtures import archive, package


class TestPrime:
    """业务测试替身。不会连接 COM，不用于生产，也不验证 Prime 文件兼容性。"""
    __test__ = False
    fail_calculation = False
    fail_save = False
    fail_print = False
    calls = 0
    version = "test-double"

    def __init__(self, work):
        self.work = work

    def __enter__(self):
        type(self).calls += 1
        return self

    def __exit__(self, *_):
        pass

    def open(self, path):
        self.path = path

    def close_sheet(self):
        pass

    def verify_aliases(self, fields):
        self.fields = fields

    def set_inputs(self, fields, inputs):
        self.inputs = inputs

    def calculate(self):
        if self.fail_calculation:
            raise service.BusinessError("test_calculation_failure", "测试计算失败")

    def verify_inputs(self, fields, inputs):
        self.inputs = inputs

    def read_outputs(self, fields):
        return {f["id"]: {"type": "real", "value": "20", "units": "", "status": "calculated"}
                for f in fields if f["direction"] == "output"}

    def save(self, path):
        if self.fail_save:
            raise RuntimeError("测试保存失败")
        with zipfile.ZipFile(self.path) as z:
            parts = {p: z.read(p) for p in z.namelist()}
        root = ET.fromstring(parts["mathcad/worksheet.xml"])
        root.find(f".//{{{ML}}}define/{{{ML}}}real").text = next(iter(self.inputs.values()))["value"]
        parts["mathcad/worksheet.xml"] = ET.tostring(root)
        path.write_bytes(archive(parts))

    def print_pdf(self, path):
        if self.fail_print:
            raise RuntimeError("测试打印失败")
        writer = PdfWriter()
        writer.add_blank_page(300, 400)  # 仅测试 PDF 容器与缓存，不冒充真实 Mathcad 预览。
        writer.write(path)


@pytest.fixture(autouse=True)
def reset_prime():
    TestPrime.fail_calculation = TestPrime.fail_save = TestPrime.fail_print = False
    TestPrime.calls = 0


def uploaded(session):
    document_id = service.upload(session, "工作表.mcdx", package())["id"]
    return service.view(session, document_id, True)


def run_job(session, doc, *, preview=False):
    if preview:
        result = service.request_artifact(session, doc["id"], doc["draft_version"], doc["latest_revision"], "pdf")
    else:
        result = service.submit_calculation(session, doc["id"], doc["draft_version"], doc["inputs"])
    job_id = worker.claim()
    assert job_id == result["job_id"]
    worker.execute(job_id, prime_factory=TestPrime)
    session.expire_all()
    return service.view(session, doc["id"], True)


def test_upload_immutable_bytes_snapshots_and_duplicate(session):
    data = package()
    first = service.upload(session, "a.mcdx", data)
    second = service.upload(session, "a.mcdx", data)
    assert first["id"] != second["id"] and second["duplicate_of"] == first["id"]
    doc = session.get(Document, first["id"])
    assert Path(doc.source_path).read_bytes() == data and doc.sha256 == sha(data)
    parts = session.scalars(select(SourcePart).where(SourcePart.document_id == doc.id)).all()
    assert {p.path: p.content for p in parts} == parse(data).parts


def test_version_conflict_and_numeric_validation(session):
    doc = uploaded(session)
    inputs = doc["inputs"]
    next(iter(inputs.values()))["value"] = "12.34500"
    saved = service.save_inputs(session, doc["id"], 0, inputs)
    assert saved["draft_version"] == 1
    refreshed = service.view(session, doc["id"], True)
    assert refreshed["dirty"] and refreshed["fields"][0]["origin"] == "草稿输入"
    with pytest.raises(service.BusinessError, match="其他页面"):
        service.save_inputs(session, doc["id"], 0, inputs)
    session.rollback()
    next(iter(inputs.values()))["value"] = "NaN"
    with pytest.raises(service.BusinessError, match="有限实数"):
        service.save_inputs(session, doc["id"], 1, inputs)


def test_calculate_atomic_snapshot_and_locks(session):
    doc = uploaded(session)
    next(iter(doc["inputs"].values()))["value"] = "11"
    result = service.submit_calculation(session, doc["id"], 0, doc["inputs"])
    job = session.get(Job, result["job_id"])
    run = session.get(CalculationRun, job.run_id)
    assert next(iter(run.inputs.values()))["value"] == "11" and run.draft_version == 1
    with pytest.raises(service.BusinessError, match="未完成任务"):
        service.save_inputs(session, doc["id"], 1, doc["inputs"])


def test_success_dirty_cache_and_new_revision(session):
    doc = uploaded(session)
    original = session.get(Document, doc["id"]).source_path
    digest = sha(Path(original).read_bytes())
    doc = run_job(session, doc)
    assert doc["latest_revision"] == 1 and doc["run"]["artifact_status"] == "ready"
    assert doc["fields"][-1]["origin"] == "计算版本 r1"
    artifact = service.request_artifact(session, doc["id"], doc["draft_version"], 1, "mcdx")
    assert artifact["cached"] and TestPrime.calls == 1
    doc = run_job(session, doc, preview=True)
    assert TestPrime.calls == 2
    response = service.request_artifact(session, doc["id"], doc["draft_version"], 1, "pdf")
    assert response["cached"] and TestPrime.calls == 2
    doc = run_job(session, doc)
    assert doc["latest_revision"] == 2 and TestPrime.calls == 3
    assert len(doc["artifacts"]) == 3
    assert sha(Path(original).read_bytes()) == digest
    next(iter(doc["inputs"].values()))["value"] = "33"
    service.save_inputs(session, doc["id"], doc["draft_version"], doc["inputs"])
    doc = service.view(session, doc["id"], True)
    assert doc["dirty"]
    session.rollback()
    with pytest.raises(service.BusinessError, match="先计算"):
        service.request_artifact(session, doc["id"], doc["draft_version"], 2, "mcdx")


def test_calculation_failure_preserves_successful_revision(session):
    doc = run_job(session, uploaded(session))
    TestPrime.fail_calculation = True
    doc = run_job(session, doc)
    assert doc["latest_revision"] == 1
    assert doc["jobs"][0]["status"] == "failed"
    assert doc["fields"][-1]["value"]["value"] == "20"


def test_save_failure_keeps_results_and_retry_creates_new_revision(session):
    TestPrime.fail_save = True
    doc = run_job(session, uploaded(session))
    assert doc["latest_revision"] == 1 and doc["run"]["status"] == "succeeded"
    assert doc["run"]["artifact_status"] == "failed" and not doc["artifacts"]
    result = service.request_artifact(session, doc["id"], doc["draft_version"], 1, "mcdx")
    assert result["revision"] == 2


def test_pdf_failure_keeps_calculated_outputs_and_mcdx(session):
    TestPrime.fail_print = True
    doc = run_job(session, uploaded(session), preview=True)
    assert doc["latest_revision"] == 1 and doc["run"]["artifact_status"] == "ready"
    assert [a["kind"] for a in doc["artifacts"]] == ["mcdx"]
    assert doc["jobs"][0]["status"] == "failed"
    result = service.request_artifact(session, doc["id"], doc["draft_version"], 1, "pdf")
    assert result["revision"] == 1


def test_recovery_interrupts_running_not_queued(session):
    doc = uploaded(session)
    result = service.submit_calculation(session, doc["id"], 0, doc["inputs"])
    worker.claim()
    worker.recover()
    session.expire_all()
    job = session.get(Job, result["job_id"])
    assert job.status == "interrupted" and session.get(Document, doc["id"]).active_job is None


def test_api_csrf_openapi_download_and_range(session):
    with TestClient(app) as client:
        assert client.post("/api/documents").status_code == 403
        token = client.get("/api/csrf").json()["token"]
        uploaded_bytes = package()
        response = client.post("/api/documents", files={"file": ("x.mcdx", uploaded_bytes)}, headers={"x-csrf-token": token})
        assert response.status_code == 201
        doc_id = response.json()["id"]
        assert client.get(f"/api/documents/{doc_id}/original").content == uploaded_bytes
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/api/health").json()["database"] == "ok"
        ranged = client.get(f"/api/documents/{doc_id}/original", headers={"Range": "bytes=0-3"})
        assert ranged.status_code == 206 and ranged.content == b"PK\x03\x04"


def test_pdf_validation_rejects_fake_or_truncated_file(tmp_path):
    path = tmp_path / "not-pdf.pdf"
    path.write_bytes(b"%PDF-1.7 fake")
    with pytest.raises(Exception):
        validate_pdf(path)
