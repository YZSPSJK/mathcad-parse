import argparse
import json
import logging
import multiprocessing
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

import psutil
from sqlalchemy import select

from backend.automation import Prime, validate_pdf
from backend.config import settings
from backend.db import Artifact, CalculationRun, Document, IOField, Job, Session, migrate, now, uid
from backend.parser import RESULT, parse, sha, xml
from backend.service import BusinessError, artifact_json, cache_key, error, same_values, snapshot_parts
from backend.worksheet_inputs import check_saved_formula, patch_formulas

log = logging.getLogger("mathcad.worker")


@contextmanager
def host_lock():
    if os.name == "nt":
        import win32api
        import win32event
        import winerror
        handle = win32event.CreateMutex(None, False, "Global\\MathcadWorkbenchWorker")
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            handle.Close()
            raise RuntimeError("此主机已有 Mathcad worker，拒绝并行启动")
        try:
            yield
        finally:
            handle.Close()
    else:
        import fcntl
        with (settings().directory() / "worker.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def set_stage(job_id, stage):
    log.info("job=%s stage=%s", job_id, stage)
    with Session.begin() as session:
        job = session.get(Job, job_id)
        if job.status != "running":
            raise BusinessError("job_interrupted", "任务已中断，停止发布产物", 409)
        job.stage = stage


def finish_failure(job_id, detail, status="failed"):
    with Session.begin() as session:
        job = session.get(Job, job_id)
        if job is None or job.status not in {"queued", "running"}:
            return
        doc = session.get(Document, job.document_id)
        run = session.get(CalculationRun, job.run_id)
        job.status, job.stage, job.error, job.completed_at = status, status, detail, now()
        if run.status in {"queued", "running"}:
            run.status, run.error, run.completed_at = status, detail, now()
        elif run.status == "succeeded" and run.artifact_status == "pending":
            run.artifact_status, run.error = "failed", detail
        if doc.active_job == job.id:
            doc.active_job = None


def recover():
    with Session.begin() as session:
        jobs = session.scalars(select(Job).where(Job.status == "running")).all()
        doc_ids, run_ids = [j.document_id for j in jobs], [j.run_id for j in jobs]
        documents = {d.id: d for d in session.scalars(select(Document).where(Document.id.in_(doc_ids))).all()}
        runs = {r.id: r for r in session.scalars(select(CalculationRun).where(CalculationRun.id.in_(run_ids))).all()}
        for job in jobs:
            detail = error("worker_restarted", "worker 重启中断了任务，请重试；重新计算会创建新版本")
            job.status, job.stage, job.error, job.completed_at = "interrupted", "interrupted", detail, now()
            run, doc = runs[job.run_id], documents[job.document_id]
            if run.status in {"queued", "running"}:
                run.status, run.error, run.completed_at = "interrupted", detail, now()
            elif run.status == "succeeded" and run.artifact_status == "pending":
                run.artifact_status, run.error = "failed", detail
            if doc.active_job == job.id:
                doc.active_job = None
    # SQLite 事务结束后才处理进程资源。
    for job in jobs:
        cleanup_owned(settings().directory() / "work" / job.id)


def claim():
    with Session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        job = session.scalar(select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1))
        if job is None:
            session.rollback()
            return None
        job.status, job.stage, job.started_at = "running", "connecting", now()
        run = session.get(CalculationRun, job.run_id)
        if run.status == "queued":
            run.status = "running"
        session.commit()
        return job.id


def cleanup_owned(work_dir):
    marker = work_dir / "owned-process.json"
    if not marker.exists():
        return
    try:
        owned = json.loads(marker.read_text(encoding="utf-8"))
        for pid, created in owned.items():
            try:
                process = psutil.Process(int(pid))
                if process.create_time() == created and process.name().lower() == "mathcadprime.exe":
                    # 仅结束任务启动且记录了创建时间的专用进程，防止误杀复用 PID。
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        marker.unlink(missing_ok=True)
    except Exception:
        log.exception("清理专用 Mathcad 进程失败：%s", work_dir)


def validate_saved(path, fields, expected_inputs=None, expected_outputs=None):
    parsed = parse(path.read_bytes(), matrix_order=settings().matrix_xml_order)
    expected = {(f["direction"], f["alias"], f["region_id"]) for f in fields}
    actual = {(f["direction"], f["alias"], f["region_id"]) for f in parsed.fields}
    if expected != actual:
        raise BusinessError("saved_alias_mismatch", "保存后的输入输出映射变化，拒绝发布文件", 409)
    by_alias = {(f["direction"], f["alias"]): f for f in parsed.fields}
    for field in fields:
        expected_value = (expected_inputs or {}).get(field["id"]) if field["direction"] == "input" else (expected_outputs or {}).get(field["id"])
        saved_value = by_alias[(field["direction"], field["alias"])]["saved_value"]
        if expected_value and expected_value["type"] == "formula":
            check_saved_formula(field, expected_value, by_alias[(field["direction"], field["alias"])]["expression_xml"])
        elif expected_value and saved_value["type"] in {"real", "matrix"} and not same_values(expected_value, saved_value):
            raise BusinessError("saved_value_mismatch", f"{field['alias']} 的保存值与本次计算不一致", 409)
    if "mathcad/result.xml" in parsed.parts:
        results = xml(parsed.parts["mathcad/result.xml"])
        for node in results.findall(f"{{{RESULT}}}resultData"):
            if node.get("calculation-status", "").lower() == "pending":
                raise BusinessError("pending_result", "保存后仍有 Pending 结果，拒绝发布未完成产物", 409)
    return parsed


def publish(job_id, temporary, kind, parts=None):
    """文件原子发布后才登记；崩溃留下的孤立文件无下载索引，不会暴露半成品。"""
    data = temporary.read_bytes()
    with Session() as session:
        job = session.get(Job, job_id)
        doc = session.get(Document, job.document_id)
        run = session.get(CalculationRun, job.run_id)
        if job.status != "running" or doc.active_job != job.id or run.status != "succeeded":
            raise BusinessError("publish_conflict", "任务或版本状态已变更，停止发布", 409)
        target = settings().directory() / "documents" / doc.id / "revisions" / str(run.revision) / f"{uid()}.{kind}"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
        artifact = Artifact(id=uid(), document_id=doc.id, run_id=run.id, revision=run.revision,
                            kind=kind, path=str(target), sha256=sha(data), size=len(data), cache_key=cache_key(run))
        session.add(artifact)
        if parts:
            snapshot_parts(session, doc.id, f"revision:{run.revision}", parts)
        if kind == "mcdx":
            run.artifact_status = "ready"
        session.commit()
        return artifact_json(artifact)


def save_results(job_id, outputs, engine):
    with Session.begin() as session:
        job = session.get(Job, job_id)
        doc = session.get(Document, job.document_id)
        run = session.get(CalculationRun, job.run_id)
        if job.status != "running" or doc.active_job != job.id:
            raise BusinessError("job_obsolete", "任务已失效，结果不会覆盖当前状态", 409)
        run.outputs, run.engine, run.completed_at = outputs, engine, now()
        failures = {key: value for key, value in outputs.items() if value.get("status") == "error"}
        if failures:
            run.status = "partial_failure"
            run.error = error("output_errors", "部分配置输出计算失败；最近成功版本保持不变", outputs=failures)
            raise_after_commit = run.error
        else:
            run.status = "succeeded"
            if doc.latest_revision is None or run.revision > doc.latest_revision:
                doc.latest_revision = run.revision
            raise_after_commit = None
    if raise_after_commit:
        raise BusinessError("output_errors", raise_after_commit["message"], 422, raise_after_commit["details"])


def mark_artifact_failure(job_id, exc):
    with Session.begin() as session:
        job = session.get(Job, job_id)
        run = session.get(CalculationRun, job.run_id)
        run.artifact_status = "failed"
        run.error = error("save_failed", f"计算结果已保存，但 MCDX 生成失败：{exc}")


def execute(job_id, *, prime_factory=Prime):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    work = settings().directory() / "work" / job_id
    work.mkdir(parents=True, exist_ok=True)
    try:
        with Session() as session:
            job = session.get(Job, job_id)
            doc = session.get(Document, job.document_id)
            run = session.get(CalculationRun, job.run_id)
            fields = session.scalars(select(IOField).where(IOField.document_id == doc.id)).all()
            fields = [{**{key: getattr(f, key) for key in (
                "id", "direction", "alias", "region_id", "units", "supported", "display_name")},
                "allowed_variables": f.saved_value.get("allowed_variables", [])}
                      for f in fields]
            source, original_hash = Path(doc.source_path), doc.sha256
            inputs, outputs, kind = run.inputs, run.outputs, job.kind
            saved_artifact = session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.kind == "mcdx"))
        if sha(source.read_bytes()) != original_hash:
            raise BusinessError("source_changed", "源文件校验失败，请从备份恢复", 409)
        source_copy = work / "working.mcdx"
        if kind == "preview":
            if saved_artifact is None:
                raise BusinessError("mcdx_missing", "此版本没有有效 MCDX，请重新计算生成新版本", 409)
            saved_data = Path(saved_artifact.path).read_bytes()
            if sha(saved_data) != saved_artifact.sha256:
                raise BusinessError("artifact_corrupt", "版本 MCDX 校验失败", 409)
            source_copy.write_bytes(saved_data)
        else:
            shutil.copyfile(source, source_copy)
            patch_formulas(source_copy, fields, inputs)
        with prime_factory(work) as prime:
            prime.open(source_copy)
            prime.verify_aliases(fields)
            if kind != "preview":
                set_stage(job_id, "setting_inputs")
                prime.set_inputs(fields, inputs)
                set_stage(job_id, "calculating")
                prime.calculate()
                set_stage(job_id, "reading_outputs")
                effective_inputs = prime.verify_inputs(fields, inputs)
                outputs = prime.read_outputs(fields)
                save_results(job_id, outputs, {"version": prime.version, "adapter": "Prime COM",
                                              "strategy": "api_recalculate", "effective_inputs": effective_inputs or {}})
                set_stage(job_id, "saving")
                temporary = work / "calculated.mcdx"
                try:
                    prime.save(temporary)
                    set_stage(job_id, "validating")
                    parsed = validate_saved(temporary, fields, inputs, outputs)
                    # 重开同一计算产物，核对生效输入/输出后才登记可下载文件。
                    prime.close_sheet()
                    prime.open(temporary)
                    prime.verify_aliases(fields)
                    prime.verify_inputs(fields, inputs)
                    reopened = prime.read_outputs(fields)
                    if not same_values(outputs, reopened):
                        raise BusinessError("reopen_changed", "保存并重开后结果发生变化，拒绝发布此版本", 409)
                    prime.close_sheet()
                    if kind == "calculate_preview":
                        shutil.copyfile(temporary, work / "printing.mcdx")
                    mcdx = publish(job_id, temporary, "mcdx", parsed.parts)
                except Exception as exc:
                    mark_artifact_failure(job_id, exc)
                    raise
                if kind == "calculate_preview":
                    prime.open(work / "printing.mcdx")
                    if not same_values(outputs, prime.read_outputs(fields)):
                        raise BusinessError("preview_changed", "打印副本结果发生变化，请重新计算创建新版本", 409)
            else:
                prime.verify_inputs(fields, inputs)
                if not same_values(outputs, prime.read_outputs(fields)):
                    raise BusinessError("preview_changed", "打开文件后结果发生变化，请重新计算创建新版本", 409)
                mcdx = artifact_json(saved_artifact)
            result = {"mcdx": mcdx}
            if kind in {"preview", "calculate_preview"}:
                set_stage(job_id, "printing")
                pdf = work / "preview.pdf"
                prime.print_pdf(pdf)
                validate_pdf(pdf)
                if not same_values(outputs, prime.read_outputs(fields)):
                    raise BusinessError("preview_changed", "打印前后结果发生变化，请重新计算创建新版本", 409)
                result["pdf"] = publish(job_id, pdf, "pdf")
        if sha(source.read_bytes()) != original_hash:
            raise BusinessError("source_changed", "源文件完整性检查失败", 409)
        with Session.begin() as session:
            job = session.get(Job, job_id)
            doc = session.get(Document, job.document_id)
            if job.status != "running" or doc.active_job != job.id:
                raise BusinessError("job_obsolete", "任务已失效", 409)
            job.status, job.stage, job.completed_at, job.result = "succeeded", "completed", now(), result
            doc.active_job = None
    except BusinessError as exc:
        log.warning("job=%s code=%s %s", job_id, exc.code, exc.message)
        finish_failure(job_id, error(exc.code, exc.message, context=exc.details))
    except Exception as exc:
        log.exception("job=%s failed", job_id)
        finish_failure(job_id, error("automation_failed", f"Mathcad 自动化失败，请检查许可、桌面会话和日志：{exc}"))


def supervise(job_id):
    child = multiprocessing.get_context("spawn").Process(target=execute, args=(job_id,))
    child.start()
    heartbeat = settings().directory() / "worker-heartbeat"
    deadline = time.monotonic() + settings().job_timeout
    try:
        while child.is_alive() and time.monotonic() < deadline:
            heartbeat.touch()
            child.join(timeout=1)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
            finish_failure(job_id, error("process_timeout", "任务进程总超时，已中断；请检查 Mathcad 弹窗后重试"))
        elif child.exitcode != 0:
            finish_failure(job_id, error("worker_crash", f"任务进程异常退出（{child.exitcode}），请重试"))
    except BaseException:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        finish_failure(job_id, error("worker_stopped", "worker 已停止，任务已中断"), "interrupted")
        raise
    finally:
        cleanup_owned(settings().directory() / "work" / job_id)
        child.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只领取并处理一个任务")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    migrate()
    heartbeat = settings().directory() / "worker-heartbeat"
    with host_lock():
        recover()
        try:
            while True:
                heartbeat.touch()
                job_id = claim()
                if job_id:
                    supervise(job_id)
                if args.once:
                    break
                time.sleep(1)
        finally:
            heartbeat.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
