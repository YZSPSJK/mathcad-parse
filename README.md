# Mathcad Workbench

Windows 单机的 Mathcad 工作表管理、参数计算与 PDF 预览系统。使用 Next.js App Router + TypeScript、FastAPI、SQLAlchemy/SQLite、独立串行 Mathcad worker；业务数据全部写入可配置的 `DATA_DIR`。

已实现上传、安全 ZIP/XML 解析、源文件原字节保留、参数/受限公式表单、公式与 XML 双向转换、草稿保存与乐观锁、持久化任务、不可变计算版本、MCDX/PDF 产物缓存、下载、失败恢复及 Windows 自动化适配。网页支持已有公式输入的右侧表达式编辑，布局保持不变；计算仍由 Mathcad 执行。

**本机标量计算及原生 PDF 导出已通过验收。** Prime 12.0.1.0 使用 `Dispatch`，真实任务已完成批量设置、计算、MCDX 保存重开、`SaveAs(.pdf)` 导出及缓存复用。本机 `.env` 已启用验证配置；新环境模板仍默认 `INTEGRATION_VERIFIED=false`。矩阵/单位及删除 result.xml 实验仍待独立验收，矩阵顺序未配置时保持只读。查看 [集成验证记录](docs/integration-verification.md)。

## 安装与启动

需要 Windows 10/11、已安装并激活的 Mathcad Prime 12、Python 3.12、[uv](https://docs.astral.sh/uv/getting-started/installation/)、含 npm 的 Node.js 22/24 LTS，以及原生 PDF 导出所需的 Microsoft Print to PDF。Mathcad 依赖已登录的交互桌面；不要以 Windows 服务 Session 0 方式运行 worker。当前 PDF 导出直接调用 COM，不发送打印快捷键或操作打印菜单。

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

访问 [工作表管理](http://127.0.0.1:3000)，[OpenAPI](http://127.0.0.1:8000/docs)。启动脚本将 API、worker、前端作为独立隐藏进程启动，日志写入 `.runtime/*.out.log` 和 `.runtime/*.err.log`，PID 与进程创建时间写入 `.runtime/pids.json`。停止：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
```

开发模式：`scripts\start.ps1 -Mode Dev`。默认仅监听 `127.0.0.1`，适用于本机或受控内网，不提供公网身份认证。无需 Redis/Celery，不使用 Linux 容器运行 Mathcad。

手动启动可在三个终端分别执行，工作目录均为项目根目录（前端命令除外）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m backend.worker
cd frontend
npm run dev
```

`setup.ps1` 使用已提交的 `uv.lock` 和 `frontend/package-lock.json`，执行 `uv sync --frozen`、数据库迁移、`npm ci` 与生产构建。首次运行复制 `.env.example` 为 `.env`，不覆盖已有配置。`start.ps1` 在启动服务前为已有文档补充公式编辑元数据，保留字段 ID、数值草稿及历史版本；手动启动时先执行 `.venv\Scripts\python.exe -m backend.upgrade_formulas`。

## 真实计算启用步骤

本机已验证标量及 PDF 并启用；其他环境先按 [集成验收步骤](docs/integration-verification.md) 核对真实计算、读回、另存、重开和原生 PDF，再启用配置。完整矩阵验收可准备含标量、矩阵及配置输出的真实 MCDX，执行 `scripts/smoke.py`：

```dotenv
INTEGRATION_VERIFIED=true
MATRIX_XML_ORDER=column-major
```

矩阵顺序必须使用报告中的实际值（支持 `column-major` 和 `row-major`）；未经确认的矩阵只读展示原始 XML 与原因。当前原生 PDF 路径不读取旧的 `config/print-uia.json`，无需校准打印窗口。

worker 使用 `Dispatch` 连接已有 Mathcad 实例或启动实例；已有进程不写入任务所有权记录，任务仅关闭自己打开的工作副本。仅当连接前没有 Mathcad 进程时，才记录新进程供超时/退出清理。多个进程无法唯一定位时会明确报错，不任选一个操作。

## 使用与版本规则

1. 上传 `.mcdx` 后直接显示解析表单，不自动启动 Mathcad。文件中已有值标注为“文件中保存的值”，Pending/缺失结果显示“尚无有效结果”。没有 IO 分配的文档仍可管理和预览，页面提示在 Mathcad 配置后重新上传。
2. IO 分配中的有限实数、数值矩阵及受限算术公式可编辑；单位和矩阵尺寸固定。实数框保持纯数值校验，公式框支持 `+ - * /`、括号、一元正负号、科学计数法及前置输入变量。复杂单位、文本、复数和超出支持范围的公式保留原文与原因。
3. 点击“保存草稿”只保存输入。每次显式“计算”都冻结当前完整可编辑输入集合并创建新 revision，即使输入相同。计算期间当前文档不可编辑，其他页面的过期版本请求返回 409。
4. 输入修改后，旧输出和旧 PDF 标为过期；“预览”和“导出最新 MCDX”要求先计算。历史产物可通过“任务与版本”按明确版本下载。
5. 首次预览/导出若尚无成功版本，会提交真实计算任务并显示阶段；上传时的旧输出不会冒充该任务结果。最新版本已有产物时直接复用，不调用 Mathcad。
6. 计算成功先保存结果，再生成 MCDX；保存失败保留计算输出并单独报告。重试需重新计算时创建新 revision。PDF 单独失败仍可下载已生成 MCDX，重试 PDF 使用原 revision。
7. 新版本不会复用旧 PDF；打开版本 MCDX 若发生结果变化，预览失败并要求重新计算，不覆盖已有版本快照。

完整源文件永久保留。所有 ZIP 部件按字节保存在上传快照表；计算后部件另存 `revision:<n>` 快照，未知图片、图表、文本等部件由 Prime 保存，不在本系统中重建。解析器按命名空间 URI 区分节点，不依赖 XML 前缀；alias、region-id、resultRef 分开存储，支持一 region 多 alias，冲突 alias 禁止编辑。

### 公式输入与 XML

例如已有输入 `C := A + 2 B + 12` 会显示为 `A + 2 * B + 12`，可编辑为 `(A+B)*3-6/(B+1)`。变量名称使用工作表中排在当前区域之前的输入定义名称（如 `A`、`B`），页面列出允许名称；不使用 IO alias 代替变量名。乘法必须写 `*`，也接受 `×`、`÷`、中文括号和 Unicode 减号。当前不支持函数、幂、下标、赋值、输出名、自引用或后置变量。

展开“当前公式 XML”可查看 `ml:define` 结构；展开“从 XML 导入公式”可粘贴当前变量的 define、工作表 math 包装或右侧表达式 XML。XML 校验命名空间及运算节点，不能修改左侧变量名，也不允许 DTD/实体。公式转换仅处理结构，不执行 Python 表达式；编辑后保存草稿或点击计算。

计算任务只在独立工作副本替换公式右值，其余 ZIP 部件保持字节不变，再由 Mathcad 设置数值输入、同步重算、另存并重开核验。公式输入的生效数值作为“上次计算值”保存；MCDX 保留公式结构，PDF 使用同一版本的真实计算结果。源文件与旧版本保持原样。

## 计算与结果清理策略

生产候选策略为 `RESULT_STRATEGY=api_recalculate`：复制完整源文件 → 在副本中写入受限公式 XML → 批量 API 设置数值输入并检查逐项返回码 → 同步计算/读取 ErrorCode → 保存 → ZIP/XML 核查（包含公式结构）→ Prime 重开核对输入输出 → 原子发布。只支持通过真实 smoke 后启用此策略。

没有把“直接删除 result.xml”设为默认：删除可能破坏关系或兼容性，且不能证明新结果正确。smoke 会在独立副本执行完整删除实验并保存结果；无论实验成功与否，交付文件始终经过真实 Mathcad 计算和 SaveAs。不会用 XML 改数字后打包替代原生计算。PDF 使用本机已实测的 `Worksheet.SaveAs("file.pdf")` 原生导出，并校验文件头、可解析性和页数。本机类型库旧 Worksheet 接口注释明确列出 pdf/xps 支持，且生成文件与打印前后结果已实际核对，未仅凭菜单或文件扩展名假设成功。

计算完成路径使用同步/批量 API、错误码和保存重开核对，不采用 Modified、文件时间戳或固定 sleep 作为完成依据。本机标量样本已通过实际任务验证；其他工作表类型仍应按目标环境验证。PDF 原生导出卡住时受任务进程总超时限制。

## 持久化与恢复

```text
DATA_DIR/
  mathcad.sqlite3                 # SQLite；外键、WAL、busy_timeout
  documents/<id>/original/source.mcdx
  documents/<id>/revisions/<n>/<artifact-uuid>.mcdx|pdf
  work/<job-id>/                  # 每任务独立副本及进程所有权记录
  smoke/<uuid>/                  # 真实集成验收报告与实验产物
  worker-heartbeat
```

数据库实体包括 documents、source_parts、regions、io_fields、draft_inputs、calculation_runs、jobs、artifacts。迁移入口：`.venv\Scripts\python.exe -m backend.db`，当前 schema v1，可重复执行；拒绝启动比程序更新的数据库。

任务持久化在 SQLite，用短事务原子领取，不持有事务等待 COM。整台 Windows 主机通过命名互斥锁限制单 worker，再按任务启动隔离进程。`CALCULATION_TIMEOUT` 控制 API 超时，`JOB_TIMEOUT` 控制任务进程总超时。worker 重启后运行中的遗留任务标记中断，排队任务保留。失败输入不会覆盖最近成功输出；已取得的计算结果和文件状态分开记录。

源文件、历史版本与工作目录当前均不自动清理，避免误删业务数据；需由管理员制定保留策略。备份前运行 `stop.ps1`，完整复制 `DATA_DIR`（包括 SQLite、WAL/SHM 若存在、源文件及产物）到备份位置；恢复时停服务后恢复整个目录，再用相同或兼容版本启动。不要仅备份 SQLite 而遗漏文件。

## API

| 方法 | 路径 | 功能 |
|---|---|---|
| POST/GET | `/api/documents` | 上传/列表 |
| GET | `/api/documents/{id}`、`/{id}/form` | 状态/表单 |
| PUT | `/api/documents/{id}/inputs` | 保存完整草稿，携带 expected_version |
| POST | `/api/documents/{id}/formulas/{field_id}/convert` | 公式转 XML 或 XML 转公式，提交 expression/xml 之一 |
| POST | `/api/documents/{id}/calculate` | 当前完整 inputs + expected_version，返回 202/job_id |
| POST | `/api/documents/{id}/preview`、`/{id}/export` | expected_version + revision；缓存命中 200，否则 202 |
| GET | `/api/jobs/{id}` | 阶段、结果、错误 |
| GET | `/api/artifacts/{id}/content` | 版本文件，PDF 支持 Range；`?download=true` 强制下载 |
| GET | `/api/documents/{id}/original` | 原始上传字节 |
| GET | `/api/health` | API、数据库、worker 心跳 |
| GET | `/api/csrf` | 会话双提交校验 token |

统一错误结构：`{"error":{"code":"...","message":"...","details":...}}`。前端通过 Next.js 同源转发访问 FastAPI，不重复实现业务逻辑。所有写操作需先 GET `/api/csrf`，保持返回 cookie，并在请求头发送 `x-csrf-token`。Swagger 或独立客户端调用写接口也需带该 cookie/header；多页面复用同一浏览器 token。下载只接受业务 ID，不能传本地文件路径。

## 验证与故障排查

```powershell
uv run pytest -q
uv run ruff check backend scripts
cd frontend
npm run typecheck
npm run build
```

2026-10-25 验证环境：Python 3.12.14、Node 24.14.0、Next.js 16.4.3、React 19.3.1；其余版本见锁文件。80 项公式/解析/服务/API/适配器测试通过，TypeScript 检查与生产构建通过。测试中的 TestPrime 与合成 PDF 仅验证状态机、缓存和文件容器，完全不用于生产。[公式验收记录](docs/formulas-verification.md) 包含真实 Mathcad 计算和 XML 往返验证；[完整验证记录](docs/verification.md) 包含此前浏览器与数据流证据。

| 现象 | 处理 |
|---|---|
| 排队不执行 | 查看 `/api/health` 的 worker 状态与 `.runtime/worker.err.log` |
| integration_unverified | 执行真实 smoke，排除 COM/许可问题后再启用验证配置 |
| COM 无法启动 | 先手工确认 Prime 能正常运行，检查许可、COM 注册、用户会话和安装版本；不自动安装或激活商业软件 |
| ownership_unknown | 当前存在多个或无法识别 Mathcad 进程，保留一个实例后重试；已有实例不会被当作任务资源关闭 |
| 矩阵只读 | 尚未确认 XML 顺序或包含不支持值；按 smoke 报告配置顺序后重新上传 |
| alias_mismatch | XML 分配与 API 不一致，回到 Prime 核对 Input/Output 配置，不根据变量名猜测方向 |
| 409 冲突 | 刷新获取新草稿版本；如果输入未计算，请先显式计算 |
| PDF 失败 | 确认本机支持原生另存为 PDF 且有 Microsoft Print to PDF；查看任务错误后重试，不会自动退回弹出打印窗口的路径 |
| 超时/崩溃 | 系统中断隔离任务并保留原文件/最近成功版本；检查弹窗及日志后重试 |

技术依据：[Prime Worksheet API](https://support.ptc.com/help/mathcad/r12.0/en/PTC_Mathcad_Help/API/Object_Worksheet.html)、[Prime 批量设置](https://support.ptc.com/help/mathcad/r12.0/en/PTC_Mathcad_Help/API/Object_ValuesSetter.html)、[PDF 打印](https://support.ptc.com/help/mathcad/r12.0/en/PTC_Mathcad_Help/to_print_worksheets.html)、[Next.js 同源转发](https://nextjs.org/docs/app/api-reference/config/next-config-js/rewrites)。
