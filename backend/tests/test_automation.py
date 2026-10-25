import json
from types import SimpleNamespace

import pytest

from backend import automation
from backend.service import BusinessError


@pytest.mark.parametrize("existing", [{}, {123: 456.0}])
def test_dispatch_reuses_existing_process_without_owning_it(monkeypatch, tmp_path, existing):
    pythoncom = pytest.importorskip("pythoncom")
    client = pytest.importorskip("win32com.client")
    win32ts = pytest.importorskip("win32ts")
    calls = []
    app = SimpleNamespace(GetVersion=lambda: "12.0.1.0", Visible=False)
    snapshots = iter([existing, {123: 456.0}])
    monkeypatch.setattr(automation.platform, "system", lambda: "Windows")
    monkeypatch.setattr(automation, "prime_processes", lambda: next(snapshots))
    monkeypatch.setattr(pythoncom, "CoInitialize", lambda: calls.append("initialize"))
    monkeypatch.setattr(pythoncom, "CoUninitialize", lambda: calls.append("uninitialize"))
    monkeypatch.setattr(win32ts, "ProcessIdToSessionId", lambda _: 1)
    monkeypatch.setattr(client, "Dispatch", lambda progid: calls.append(progid) or app)
    monkeypatch.setattr(client, "DispatchEx", lambda *_: pytest.fail("不应使用 DispatchEx"))
    with automation.Prime(tmp_path, allow_unverified=True) as prime:
        assert prime.pid == 123
        marker = tmp_path / "owned-process.json"
        assert marker.exists() == (not existing)
        if marker.exists():
            assert json.loads(marker.read_text()) == {"123": 456.0}
    assert calls == ["initialize", "MathcadPrime.Application", "uninitialize"]


def test_pdf_uses_native_saveas_and_validates_file(monkeypatch, tmp_path):
    calls = []
    prime = automation.Prime(tmp_path)
    target = tmp_path / "preview.pdf"
    prime.sheet = SimpleNamespace(SaveAs=lambda path: calls.append(("save", path)))
    monkeypatch.setattr(automation, "validate_pdf", lambda path: calls.append(("validate", path)) or 2)
    assert prime.print_pdf(target) == 2
    assert calls == [("save", str(target.resolve())), ("validate", target.resolve())]


def test_pdf_rejects_invalid_native_export(monkeypatch, tmp_path):
    prime = automation.Prime(tmp_path)
    prime.sheet = SimpleNamespace(SaveAs=lambda _: None)

    def invalid(_):
        raise ValueError("不是有效 PDF")

    monkeypatch.setattr(automation, "validate_pdf", invalid)
    with pytest.raises(BusinessError, match="原生 PDF 导出失败"):
        prime.print_pdf(tmp_path / "preview.pdf")


def test_cleanup_error_does_not_mask_original_task_error(monkeypatch, tmp_path):
    pytest.importorskip("pythoncom")
    prime = automation.Prime(tmp_path)

    def close():
        raise RuntimeError("清理失败")

    monkeypatch.setattr(prime, "close_sheet", close)
    with pytest.raises(BusinessError, match="原始打印错误"):
        try:
            raise BusinessError("print_failed", "原始打印错误")
        except BusinessError as exc:
            prime.__exit__(type(exc), exc, exc.__traceback__)
            raise
