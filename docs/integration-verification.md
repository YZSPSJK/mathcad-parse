# Mathcad Prime 集成验证记录

日期：2026-10-24。操作系统：Windows 11。安装目录：`C:/Program Files/PTC/Mathcad Prime 12.0.1.0`。

## 已核对的证据

- 本机 `Ptc.MathcadPrime.Automation.tlb`：GUID `{A24EB614-A183-400F-8207-1E58D61945D6}`，类型库 12.0。
- ProgID `MathcadPrime.Application` 的 CLSID 为 `{A00E8B95-D415-433F-A04E-D298A54A7BB7}`。
- `DefaultCalculationTimeout` 在本机类型库中是可写属性，不按网页说明误当作普通方法。
- `ValuesSetter.AddScalarValue(alias, double, units)`、`AddMatrixValue(alias, SAFEARRAY(VARIANT)[,], units)` 返回 void；最终 `SetValues(seconds)` 返回 `SetValueResults`，可用 `GetResultByAlias` 检查每项返回码。
- `Matrix.GetMatrixElement(row, col, out double)` 返回整数状态；Python 适配器按 `(code, value)` 读取，仍需真实 COM 调用确认。
- `SaveOption_spDiscardChanges=2` 已从本机类型库直接读出。
- 本机空白模板使用 worksheet50、math50、result10 和 integration10 命名空间。模板的应用版本为 6.0，不能用它冒充 Prime 12 保存样本。

## 首次集成尝试记录（历史）

首次测试中，沙箱内 `DispatchEx` 返回 `0x80070005`（拒绝访问）；用正常进程权限重试返回 `0x80010100`（系统调用失败），当时未取得工作表对象。后续按用户原脚本使用 `Dispatch` 的补测已成功，见下节；这些历史错误不能用于认定当前环境无法计算。

| 项目 | 预定实现与验收条件 |
|---|---|
| 计算完成 | `Synchronize` 后使用带超时的批量屏障、逐项设置返回码和 GetValue ErrorCode；保存后附加检查 Pending；实际同步语义由 smoke 验证 |
| 标量与矩阵 | 修改真实 input alias，读回数值、维度、单位，保存重开，核对 XML 与 API，确认矩阵顺序 |
| PDF | 当前已改用本机验证成功的原生 SaveAs(.pdf)，不再采用打印窗口自动化；见后续验证记录 |
| 删除结果实验 | 仅对独立副本删除 result.xml，检查打开、重算、保存、重开后的输出；保存原始关系与内容类型用于诊断 |
| 最终生产策略 | 完整工作副本 + API 覆盖所有支持的输入 + 同步重算 + 真实 SaveAs；通过 smoke 后才启用 |

最初附件目录包含调研 Markdown 和图片，没有实际 `test_new.mcdx`。后续上传的真实 `source.mcdx` 已用于静态解析、原始脚本 COM 重算，以及实际 worker 的批量标量任务与原生 PDF 导出验证。矩阵、单位及删除结果实验仍需独立验收。

## 用户原始 Dispatch 脚本补测：通过

2026-10-24，直接使用 Python 3.12.14 / pywin32 311 / Mathcad Prime 12.0.1.0，绕过业务 worker 和验收开关，按原流程执行 `Dispatch → Open → Synchronize → sleep(3) → SaveAs`。仅对独立副本操作，上传原件 SHA-256 保持不变。

| 用例 | 输入与原缓存 | 保存并用 Prime 重开读取的结果 |
|---|---|---|
| 原件副本重算 | A=12、B=2、C 为公式，out 原缓存=30 | A=12、B=2、C=28、out=30，全部 ErrorCode=0 |
| 改输入后重算 | 测试副本将 A 改为20，保留 out 旧缓存30 | A=20、B=2、C=36、out=38，全部 ErrorCode=0 |

第二个用例证明结果确实更新，而非仅保存了旧缓存。两个产物均通过 ZIP CRC/核心部件检查，且由 Prime 重新打开读取核对。

- 可重跑脚本：`scripts/recalculate_original.py --source <真实MCDX路径>`；脚本自动复制原件、在隔离进程运行原流程并输出 `report.json`，总超时默认90秒。
- 原件副本产物：`data/manual-tests/445a1672-5d5b-4fe2-9bf3-53d2501adb42/recalculated.mcdx`。
- 改 A=20 的产物：`data/manual-tests/e64c8315-09e6-4afe-8ccc-65ebe9829e9d/recalculated.mcdx`。
- 重开读取记录：[original-script-verification-2026-10-24.json](original-script-verification-2026-10-24.json)。

该次补测仅对原始脚本和标量样本进行验证。保留3秒等待以忠实复现用户代码，不将它推广为任意工作表的充分完成判据；后续生产批量路径及原生 PDF 验证见下节。

## Dispatch 与原生 PDF 实际任务：通过

用户指出本机菜单支持“另存为 → PDF 格式”后，检查到安装目录的自动化 XML 在 `IMathcadPrimeWorksheet.SaveAs` 注释中明确支持 pdf/xps，并说明需要 PDF 驱动支持。随后直接实测 `Dispatch → Open → SaveAs(.pdf)`：生成 `%PDF-1.7`、1页，包含 A/B/C 定义及 out=30 的公式文本，导出前后 ErrorCode=0、输出一致，工作表 FullName 仍为原 MCDX 副本。

生产适配器已使用 Dispatch，已有 Mathcad 进程不登记为任务拥有；仅在连接前没有现有进程时记录新进程。PDF 直接使用本机验证过的原生 SaveAs，不发送 Ctrl+P，不操作打印或保存菜单，不自动退回 UI 打印。用户按 Esc 停止界面操作后，未再继续进行界面自动操作，已停止相关测试进程。

真实任务 `e1e0a5dc-a10d-4d8b-b0b5-2cf1d2a6a0b1` 在文档 r3 完成批量设置、计算、读取错误码、保存、重开核对和 PDF 导出，状态 succeeded。PDF 为1页，包含结果30；再次预览返回 cached=true，原文件哈希不变。记录：[native-pdf-verification-2026-10-24.json](native-pdf-verification-2026-10-24.json)。

本机 `.env` 已设置 `INTEGRATION_VERIFIED=true` 以启用已验收的标量/PDF 路径。矩阵 XML 顺序仍不配置，保持只读；其他机器的模板仍默认禁用集成，需自行验证。本机原生 PDF 能被严格解析，但 pypdf 提示个别对象头存在多余空白，不影响页数、内容及结果读取。

## 执行验收

1. 在已登录的 Windows 桌面，确认 Prime 12 可手工计算且只运行一个 Mathcad 进程；运行完整 smoke 前停止项目 worker，避免与业务任务并行。
2. 准备至少有一个实数输入、一个 2×3 数值矩阵输入及配置输出的真实工作表。矩阵单元格用互不相同的值；最好包含带单位标量。
3. 参照 `config/smoke-inputs.example.json` 按实际 alias 填写全部输入；所有数值为字符串，单位独立传递。
4. 确认本机原生另存为 PDF 可用，并安装其所需的 Microsoft Print to PDF。当前路径无需 UIA 选择器配置，旧 `config/print-uia.json` 不参与导出。
5. 运行：

```powershell
.\.venv\Scripts\python.exe scripts\probe.py
.\.venv\Scripts\python.exe scripts\smoke.py --sample D:\samples\verified.mcdx --inputs D:\samples\inputs.json
```

smoke 使用独立目录和专用实例，输出 `DATA_DIR/smoke/<uuid>/report.json`、计算产物、PDF 和删除结果实验副本。失败退出码为 1；删除结果实验可以失败，前提是完整副本 API 重算、保存重开、PDF 路径全部通过。

通过后按报告配置 `.env` 的 `MATRIX_XML_ORDER`，设置 `INTEGRATION_VERIFIED=true` 并重启服务。无需启用旧 UIA 打印配置。应另外用带原始图片、图表和文本的实际业务样本人工核对保存结果与 PDF 版式。记录 Prime 更新后的重新验证结果，不得仅改验证开关跳过验收。

官方资料：[Worksheet API](https://support.ptc.com/help/mathcad/r12.0/en/PTC_Mathcad_Help/API/Object_Worksheet.html)、[ValuesSetter](https://support.ptc.com/help/mathcad/r12.0/en/PTC_Mathcad_Help/API/Object_ValuesSetter.html)、[打印工作表](https://support.ptc.com/help/mathcad/r12.0/en/PTC_Mathcad_Help/to_print_worksheets.html)。
