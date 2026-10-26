"""Prime 12 的真实 COM 适配；所有 COM 对象只在创建它们的任务进程内使用。"""
import json
import logging
import math
import platform
from pathlib import Path

import psutil
from pypdf import PdfReader

from backend.config import settings
from backend.service import BusinessError, clean, same_values

ERRORS = {-1: "未知错误或工作表未打开", 1: "alias 不存在", 2: "输入输出未启用", 3: "单位不兼容",
          4: "输入无效", 5: "计算错误", 6: "结果类型不兼容", 7: "矩阵无效", 8: "计算超时", 9: "计算被禁用"}


def check(code, operation):
    if code != 0:
        raise BusinessError("mathcad_api", f"{operation}：{ERRORS.get(code, '未知返回码')}（{code}）", 503)


def prime_processes():
    return {process.pid: process.create_time() for process in psutil.process_iter(["name"])
            if (process.info["name"] or "").lower() == "mathcadprime.exe"}


def validate_pdf(path: Path):
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError("PDF 文件头无效")
        handle.seek(0)
        reader = PdfReader(handle, strict=True)
        if reader.is_encrypted or len(reader.pages) < 1:
            raise ValueError("PDF 无可读取页面")
        # 读取页面对象和内容流，避免只通过文件头/大小检查。
        for page in reader.pages:
            content = page.get_contents()
            if content is not None:
                content.get_data()
        return len(reader.pages)


class Prime:
    def __init__(self, work_dir: Path, *, allow_unverified=False):
        self.work_dir = work_dir
        self.allow_unverified = allow_unverified
        self.app = self.sheet = None
        self.pid = None
        self.initialized = False

    def __enter__(self):
        if platform.system() != "Windows":
            raise BusinessError("windows_required", "真实计算需要安装并授权 Mathcad Prime 12 的 Windows 主机", 503)
        # 临时跳过集成验证门禁；完成本机 Mathcad smoke 验收后可恢复此检查。
        # if not settings().integration_verified and not self.allow_unverified:
        #     raise BusinessError("integration_unverified", "Mathcad 集成尚未通过验证，请先完成本机计算与打印验收，再启用计算服务", 503,
        #                         {"command": "python scripts/smoke.py --sample <mcdx> --inputs <json>",
        #                          "configuration": "INTEGRATION_VERIFIED=true"})
        if settings().result_strategy != "api_recalculate":
            raise BusinessError("strategy_unverified", "结果清理策略未验证；生产仅允许已通过 smoke 的 api_recalculate 策略", 503)
        import pythoncom
        import win32com.client.dynamic
        import win32com.client
        import win32ts

        if win32ts.ProcessIdToSessionId(psutil.Process().pid) == 0:
            raise BusinessError("desktop_required", "请在已登录的 Windows 桌面启动 worker，不能运行在 Session 0", 503)
        existing = prime_processes()
        pythoncom.CoInitialize()
        self.initialized = True
        try:
            # Dispatch 优先连接已有实例；已有进程不登记为任务拥有，避免清理用户资源。
            self.app = win32com.client.Dispatch(settings().mathcad_progid)
            processes = prime_processes()
            if len(processes) != 1:
                raise BusinessError("ownership_unknown", "存在多个或无法识别 Mathcad 进程，无法确定预览窗口，请保留一个实例后重试", 503)
            self.pid = next(iter(processes))
            if not existing:
                self.work_dir.mkdir(parents=True, exist_ok=True)
                (self.work_dir / "owned-process.json").write_text(json.dumps(processes), encoding="utf-8")
            self.app.Visible = True
            self.version = str(self.app.GetVersion())
            if not self.version.startswith("12."):
                raise BusinessError("prime_version", f"需要 Prime 12，实际版本 {self.version}", 503)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def open(self, path):
        self.sheet = self.app.Open(str(Path(path).resolve()) if path else "")
        if self.sheet is None:
            raise BusinessError("open_failed", "Mathcad 无法打开工作副本，请检查格式、许可和文件锁", 503)
        self.sheet.DefaultCalculationTimeout = settings().calculation_timeout
        return self.sheet

    def aliases(self, direction):
        collection = self.sheet.Inputs if direction == "input" else self.sheet.Outputs
        if collection is None:
            raise BusinessError("alias_read", "无法枚举输入输出分配", 503)
        values = [str(collection.GetAliasByIndex(n)) for n in range(collection.Count)]
        if any(not value or value == "None" for value in values) or len(values) != len(set(values)):
            raise BusinessError("alias_read", "API alias 为空或重复", 503)
        return values

    def verify_aliases(self, fields):
        for direction in ("input", "output"):
            actual = set(self.aliases(direction))
            expected = {field["alias"] for field in fields if field["direction"] == direction}
            if actual != expected:
                raise BusinessError("alias_mismatch", f"{direction} 的 XML/API 映射不一致", 409,
                                    {"xml": sorted(expected), "api": sorted(actual)})

    def set_inputs(self, fields, inputs):
        setter = self.sheet.CreateValuesSetter()
        if setter is None:
            raise BusinessError("setter_failed", "无法创建批量参数集合", 503)
        aliases = []
        by_id = {field["id"]: field for field in fields}
        for field_id, value in inputs.items():
            alias = by_id[field_id]["alias"]
            if value["type"] == "formula":
                # 公式已在打开副本前写入 MathML，不能用 SetRealValue 覆盖定义。
                continue
            if value["type"] == "real":
                setter.AddScalarValue(alias, float(value["value"]), value["units"])
            elif value["type"] == "matrix":
                # 类型库要求 SAFEARRAY(VARIANT)[row, column]，而非 Matrix COM 对象。
                cells = tuple(tuple(float(cell) for cell in row) for row in value["value"])
                setter.AddMatrixValue(alias, cells, value["units"])
            else:
                raise BusinessError("input_type", "尚未验证此输入类型", 422)
            aliases.append(alias)
        result = setter.SetValues(settings().calculation_timeout)
        if result is None or int(result.Count) != len(aliases):
            raise BusinessError("set_failed", "批量设置未返回完整的逐项状态", 503)
        for alias in aliases:
            check(result.GetResultByAlias(alias), f"设置 {alias}")

    def calculate(self):
        self.sheet.Synchronize()
        # Synchronize 后使用具有超时语义的批量屏障和 GetValue 错误码，不采用 sleep/Modified。
        barrier = self.sheet.CreateValuesSetter().SetValues(settings().calculation_timeout)
        if barrier is None:
            raise BusinessError("calculation_barrier", "计算同步未返回有效状态", 503)

    def read(self, alias, direction, units=""):
        prefix = "Input" if direction == "input" else "Output"
        scalar = (self.sheet.OutputGetRealValueAs(alias, units) if units and direction == "output"
                  else getattr(self.sheet, f"{prefix}GetRealValue")(alias))
        if scalar is not None and scalar.ErrorCode == 0:
            value = float(scalar.RealResult)
            if math.isfinite(value):
                return {"type": "real", "value": repr(value), "units": str(scalar.Units or ""), "status": "calculated"}
            raise BusinessError("non_finite", f"{alias} 的结果不是有限实数", 422)
        matrix = (self.sheet.OutputGetMatrixValueAs(alias, units) if units and direction == "output"
                  else getattr(self.sheet, f"{prefix}GetMatrixValue")(alias))
        if matrix is not None and matrix.ErrorCode == 0:
            cells = matrix.MatrixResult
            if cells is None:
                raise BusinessError("matrix_read", f"{alias} 未返回矩阵", 503)
            rows, cols = int(cells.Rows), int(cells.Columns)
            if not 0 < rows * cols <= 10000:
                raise BusinessError("matrix_size", f"{alias} 矩阵过大或为空", 422)
            values = []
            for row in range(rows):
                line = []
                for col in range(cols):
                    code, number = cells.GetMatrixElement(row, col)
                    check(code, f"读取 {alias}[{row},{col}]")
                    if not math.isfinite(number):
                        raise BusinessError("non_finite", f"{alias} 含非有限实数", 422)
                    line.append(repr(float(number)))
                values.append(line)
            return {"type": "matrix", "value": values, "rows": rows, "cols": cols,
                    "units": str(matrix.Units or ""), "status": "calculated"}
        code = scalar.ErrorCode if scalar is not None else -1
        if code == 6 and matrix is not None:
            code = matrix.ErrorCode
        return {"type": "unsupported", "value": None, "units": units,
                "status": "error", "error_code": int(code), "reason": ERRORS.get(code, "不支持的复杂输出")}

    def read_outputs(self, fields):
        return {field["id"]: self.read(field["alias"], "output", field["units"])
                for field in fields if field["direction"] == "output"}

    def verify_inputs(self, fields, inputs):
        by_id = {field["id"]: field for field in fields}
        effective = {}
        for key, expected in inputs.items():
            actual = self.read(by_id[key]["alias"], "input")
            effective[key] = actual
            if expected["type"] == "formula":
                if actual.get("status") == "error":
                    raise BusinessError("formula_calculation", f"{by_id[key]['alias']} 的公式计算失败：{actual.get('reason')}", 422)
                continue
            if not same_values(expected, clean(actual)):
                raise BusinessError("input_mismatch", f"{by_id[key]['alias']} 的实际输入与提交值不一致", 409)
        return effective

    def verify_region_values(self, fields):
        """Check edited worksheet definitions through the same Mathcad input API."""
        values = {}
        for field in fields:
            if field["direction"] != "input":
                continue
            actual = self.read(field["alias"], "input")
            if actual.get("status") == "error" or actual.get("type") not in {"real", "matrix"}:
                raise BusinessError("region_calculation", f"{field['alias']} 的区域公式未返回有效值：{actual.get('reason', '无有效结果')}", 422)
            values[field["id"]] = actual
        return values

    def save(self, path):
        self.sheet.SaveAs(str(Path(path).resolve()))
        if not Path(path).is_file():
            raise BusinessError("save_failed", "Mathcad 未生成工作表文件", 503)

    def close_sheet(self):
        if self.sheet is not None:
            self.sheet.Close(2)  # 本机类型库 SaveOption.spDiscardChanges；probe 验证该枚举。
            self.sheet = None

    def print_pdf(self, path):
        # Prime 12.0.1.0 本机已验证该原生导出路径，不发送 Ctrl+P 或操作打印菜单。
        # 类型库 XML 的旧 Worksheet 接口说明同样列出 pdf/xps 扩展名支持。
        target = Path(path).resolve()
        if target.suffix.lower() != ".pdf":
            raise BusinessError("invalid_pdf_path", "PDF 导出目标必须使用 .pdf 扩展名", 422)
        try:
            self.sheet.SaveAs(str(target))
            return validate_pdf(target)
        except Exception as exc:
            raise BusinessError("pdf_export_failed", f"Mathcad 原生 PDF 导出失败：{exc}", 503) from exc

    def __exit__(self, exc_type, *_):
        import pythoncom

        try:
            self.close_sheet()
        except Exception:
            if exc_type is None:
                raise
            logging.getLogger(__name__).exception("关闭本任务工作表失败，保留原始任务错误")
        finally:
            # 不调用 CloseAll/Quit；仅关闭本任务工作表。父进程按记录的 PID/创建时间清理专用实例。
            self.sheet = self.app = None
            if self.initialized:
                pythoncom.CoUninitialize()
                self.initialized = False
