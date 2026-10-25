"""按用户提供的 Dispatch/Synchronize/sleep/SaveAs 脚本独立实测，不使用业务 worker 或验收开关。"""
import argparse
import hashlib
import json
import multiprocessing
import shutil
import time
import traceback
import zipfile
from pathlib import Path
from uuid import uuid4


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calculate(source, target, report_path):
    import pythoncom
    import win32com.client

    report = {"success": False, "source_copy": str(source), "target": str(target), "steps": []}

    def record(stage):
        report["stage"] = stage
        report["steps"].append({"stage": stage, "time": time.time()})
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(stage, flush=True)

    pythoncom.CoInitialize()
    app = worksheet = None
    try:
        record("Dispatch")
        app = win32com.client.Dispatch("MathcadPrime.Application")
        record("Open")
        worksheet = app.Open(str(source))
        if worksheet is None:
            raise RuntimeError("Mathcad 无法打开文件")
        record("Synchronize")
        worksheet.Synchronize()
        record("等待 3 秒（复现原脚本）")
        time.sleep(3)
        record("SaveAs")
        worksheet.SaveAs(str(target))
        if not target.is_file():
            raise RuntimeError("计算后文件未生成")
        record("检查生成的 ZIP/XML")
        with zipfile.ZipFile(target) as archive:
            invalid = archive.testzip()
            if invalid:
                raise RuntimeError(f"生成文件 ZIP 校验失败：{invalid}")
            result_xml = archive.read("mathcad/result.xml")
            archive.read("mathcad/worksheet.xml")
        (target.parent / "result.xml").write_bytes(result_xml)
        report.update(success=True, target_sha256=digest(target), target_size=target.stat().st_size,
                      result_xml=result_xml.decode("utf-8-sig"))
        record("完成")
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        if hasattr(exc, "hresult"):
            report["hresult"] = f"0x{exc.hresult & 0xffffffff:08X}"
        traceback.print_exc()
    finally:
        # 仅关闭本脚本打开的工作副本；不 Quit/CloseAll，不接管其他工作表。
        if worksheet is not None:
            try:
                worksheet.Close(2)
            except Exception as exc:
                report["close_error"] = str(exc)
        worksheet = app = None
        pythoncom.CoUninitialize()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    original = args.source.resolve(strict=True)
    original_hash = digest(original)
    work = args.output_dir or Path(__file__).resolve().parents[1] / "data" / "manual-tests" / str(uuid4())
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    source, target, report_path = work / "source-copy.mcdx", work / "recalculated.mcdx", work / "report.json"
    shutil.copyfile(original, source)
    process = multiprocessing.get_context("spawn").Process(target=calculate, args=(source, target, report_path))
    process.start()
    process.join(timeout=args.timeout)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        report.update(success=False, error=f"原脚本运行超过 {args.timeout} 秒，任务进程已中断")
    else:
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {
            "success": False, "error": f"任务进程退出，返回码 {process.exitcode}"}
    report.update(original=str(original), original_sha256=original_hash,
                  original_unchanged=digest(original) == original_hash)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print("报告路径：", report_path, flush=True)
    process.close()
    return 0 if report.get("success") and report["original_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
