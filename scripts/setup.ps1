param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw '请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw '请安装含 npm 的 Node.js 22 或 24 LTS' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败' }
& '.\.venv\Scripts\python.exe' -m backend.db
if ($LASTEXITCODE -ne 0) { throw 'SQLite 初始化失败' }
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
$npmFallback = Join-Path $projectRoot '.tools\package\bin\npm-cli.js'
Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
try {
    if ($npmCommand) { & $npmCommand.Source ci --no-audit --no-fund }
    elseif (Test-Path -LiteralPath $npmFallback) { node $npmFallback ci --no-audit --no-fund }
    else { throw '未找到 npm，请修复 Node.js 安装后重试' }
    if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }
    if (-not $SkipBuild) {
        $env:NEXT_TELEMETRY_DISABLED = '1'
        if ($npmCommand) { & $npmCommand.Source run build }
        else { node $npmFallback run build }
        if ($LASTEXITCODE -ne 0) { throw '前端生产构建失败' }
    }
} finally { Pop-Location }
Write-Host '环境已准备，请在已登录的 Windows 桌面运行 scripts\start.ps1'
