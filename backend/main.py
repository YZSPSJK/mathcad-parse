import hmac
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.exceptions import HTTPException

from backend import service
from backend.config import settings
from backend.db import Artifact, Document, Job, Session, migrate
from backend.parser import ParseError

log = logging.getLogger("mathcad.api")


@asynccontextmanager
async def lifespan(_):
    migrate()
    yield


app = FastAPI(title="Mathcad 工作表管理与计算", version="0.1.0", lifespan=lifespan)


def session_dep():
    with Session() as session:
        yield session


def failure(code, message, status, details=None):
    return JSONResponse({"error": {"code": code, "message": message, "details": details}}, status_code=status)


@app.middleware("http")
async def csrf(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        cookie = request.cookies.get("mathcad_csrf", "")
        header = request.headers.get("x-csrf-token", "")
        if not cookie or not hmac.compare_digest(cookie, header):
            return failure("csrf", "请求校验失败，请刷新页面重试", 403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(service.BusinessError)
async def business_error(_, exc):
    return failure(exc.code, exc.message, exc.status, exc.details)


@app.exception_handler(ParseError)
async def parse_error(_, exc):
    return failure("invalid_mcdx", str(exc), 422)


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc):
    return failure("validation", "请求参数不符合接口要求", 422,
                   [{"location": e["loc"], "message": e["msg"]} for e in exc.errors()])


@app.exception_handler(HTTPException)
async def http_error(_, exc):
    return failure("http_error", str(exc.detail), exc.status_code)


@app.exception_handler(Exception)
async def unexpected_error(_, exc):
    log.exception("请求处理异常", exc_info=exc)
    return failure("internal", "服务器处理失败，请查看服务日志", 500)


@app.get("/api/csrf")
def csrf_token(request: Request):
    existing = request.cookies.get("mathcad_csrf", "")
    token = existing if re.fullmatch(r"[A-Za-z0-9_-]{43}", existing) else secrets.token_urlsafe(32)
    response = JSONResponse({"token": token})
    response.set_cookie("mathcad_csrf", token, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/documents", status_code=201)
async def upload(file: UploadFile = File(...), session=Depends(session_dep)):
    data = await file.read(settings().max_upload_mb * 1024**2 + 1)
    await file.close()
    return service.upload(session, file.filename or "", data)


@app.get("/api/documents")
def list_documents(session=Depends(session_dep)):
    documents = session.scalars(select(Document).order_by(Document.uploaded_at.desc())).all()
    # 一次批量读取最新任务，禁止为每个文档循环查库。
    jobs = session.scalars(select(Job).order_by(Job.created_at.desc())).all()
    latest_jobs = {}
    for job in jobs:
        latest_jobs.setdefault(job.document_id, service.job_json(job))
    return [{"id": d.id, "name": d.name, "uploaded_at": d.uploaded_at, "size": d.size,
             "parse_status": d.parse_status, "latest_revision": d.latest_revision,
             "latest_job": latest_jobs.get(d.id)} for d in documents]


@app.get("/api/documents/{document_id}")
def document(document_id: str, session=Depends(session_dep)):
    return service.view(session, document_id)


@app.get("/api/documents/{document_id}/form")
def form(document_id: str, session=Depends(session_dep)):
    return service.view(session, document_id, form=True)


@app.get("/api/documents/{document_id}/latex")
def latex_workspace(document_id: str, session=Depends(session_dep)):
    return service.latex_workspace(session, document_id)


class InputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    inputs: dict[str, dict]


class CalculationRequest(InputRequest):
    retry_of: str | None = None


class FormulaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str | None = Field(default=None, max_length=1024)
    xml: str | None = Field(default=None, max_length=16000)


class LatexRegionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    latex: str = Field(min_length=1, max_length=1024)


class LatexCalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)


@app.post("/api/documents/{document_id}/formulas/{field_id}/convert")
def formula_convert(document_id: str, field_id: str, payload: FormulaRequest, session=Depends(session_dep)):
    return service.convert_formula(session, document_id, field_id, expression=payload.expression,
                                   xml_content=payload.xml)


@app.put("/api/documents/{document_id}/latex/regions/{region_id}")
def save_latex_region(document_id: str, region_id: str, payload: LatexRegionRequest,
                      session=Depends(session_dep)):
    return service.save_latex_region(session, document_id, region_id,
                                     payload.expected_version, payload.latex)


@app.post("/api/documents/{document_id}/latex/calculate", status_code=202)
def calculate_latex(document_id: str, payload: LatexCalculationRequest,
                    session=Depends(session_dep)):
    return service.submit_latex_calculation(session, document_id, payload.expected_version)


class ArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    revision: int | None = Field(default=None, ge=1)


@app.put("/api/documents/{document_id}/inputs")
def inputs(document_id: str, payload: InputRequest, session=Depends(session_dep)):
    return service.save_inputs(session, document_id, payload.expected_version, payload.inputs)


@app.post("/api/documents/{document_id}/calculate", status_code=202)
def calculate(document_id: str, payload: CalculationRequest, session=Depends(session_dep)):
    return service.submit_calculation(session, document_id, payload.expected_version, payload.inputs,
                                      payload.retry_of)


def artifact_request(document_id, payload, kind, session):
    result = service.request_artifact(session, document_id, payload.expected_version, payload.revision, kind)
    return JSONResponse(result, status_code=202 if "job_id" in result else 200)


@app.post("/api/documents/{document_id}/preview")
def preview(document_id: str, payload: ArtifactRequest, session=Depends(session_dep)):
    return artifact_request(document_id, payload, "pdf", session)


@app.post("/api/documents/{document_id}/export")
def export(document_id: str, payload: ArtifactRequest, session=Depends(session_dep)):
    return artifact_request(document_id, payload, "mcdx", session)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, session=Depends(session_dep)):
    job = session.get(Job, job_id)
    if job is None:
        raise service.BusinessError("not_found", "任务不存在", 404)
    return service.job_json(job)


@app.get("/api/artifacts/{artifact_id}/content")
def content(artifact_id: str, download: bool = False, session=Depends(session_dep)):
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise service.BusinessError("not_found", "产物不存在", 404)
    service.require_file(artifact)
    doc = service.get_document(session, artifact.document_id)
    filename = f"{Path(doc.name).stem}-r{artifact.revision}.{artifact.kind}"
    return FileResponse(artifact.path, filename=filename,
                        media_type="application/pdf" if artifact.kind == "pdf" else "application/vnd.ptc.mathcad",
                        content_disposition_type="attachment" if download or artifact.kind == "mcdx" else "inline",
                        headers={"X-Mathcad-Revision": str(artifact.revision),
                                 "ETag": f'"{artifact.sha256}"', "Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/documents/{document_id}/original")
def original(document_id: str, session=Depends(session_dep)):
    doc = service.get_document(session, document_id)
    if not Path(doc.source_path).is_file():
        raise service.BusinessError("source_missing", "源文件缺失，请从备份恢复", 409)
    return FileResponse(doc.source_path, filename=doc.name, media_type="application/vnd.ptc.mathcad")


@app.get("/api/health")
def health(session=Depends(session_dep)):
    session.execute(select(1))
    heartbeat = settings().directory() / "worker-heartbeat"
    alive = heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 15
    return {"api": "ok", "database": "ok", "worker": "online" if alive else "offline",
            "integration_verified": settings().integration_verified,
            "matrix_xml_order": settings().matrix_xml_order}
