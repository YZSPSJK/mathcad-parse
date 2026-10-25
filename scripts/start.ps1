param([ValidateSet('Production', 'Dev')][string]$Mode = 'Production', [int]$ApiPort = 8000, [int]$WebPort = 3000)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeDir = Join-Path $projectRoot '.runtime'
$pidFile = Join-Path $runtimeDir 'pids.json'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$nextCli = Join-Path $projectRoot 'frontend\node_modules\next\dist\bin\next'
if ($Mode -eq 'Production' -and $ApiPort -ne 8000) { throw '当前生产构建使用 API 8000 端口；自定义端口请用 Dev 模式，或先以相同 API_BASE_URL 重新构建并调整启动配置' }
if (Test-Path -LiteralPath $pidFile) { throw '存在启动记录，请先执行 scripts\stop.ps1' }
if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $nextCli)) { throw '请先运行 scripts\setup.ps1' }
if ($Mode -eq 'Production' -and -not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\.next\BUILD_ID'))) { throw '请先构建前端，或用 -Mode Dev 启动开发模式' }
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$env:PYTHONUTF8 = '1'
$env:NEXT_TELEMETRY_DISABLED = '1'
$env:API_BASE_URL = "http://127.0.0.1:$ApiPort"
Push-Location $projectRoot
try {
    & $pythonExe -m backend.upgrade_formulas
    if ($LASTEXITCODE -ne 0) { throw '已有文档公式升级失败，请查看上方错误' }
} finally { Pop-Location }
$nodeExe = (Get-Command node).Source
$entries = [System.Collections.Generic.List[object]]::new()
function Start-OwnedProcess([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$Directory) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDir "$Name.out.log") -RedirectStandardError (Join-Path $runtimeDir "$Name.err.log")
    $entries.Add(@{ name = $Name; pid = $process.Id; startTicks = $process.StartTime.ToUniversalTime().Ticks })
    ConvertTo-Json -InputObject @($entries.ToArray()) | Set-Content -LiteralPath $pidFile -Encoding UTF8
}
try {
    Start-OwnedProcess 'api' $pythonExe @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', "$ApiPort") $projectRoot
    Start-OwnedProcess 'worker' $pythonExe @('-m', 'backend.worker') $projectRoot
    $command = if ($Mode -eq 'Dev') { 'dev' } else { 'start' }
    Start-OwnedProcess 'web' $nodeExe @(('"' + $nextCli + '"'), $command, '--hostname', '127.0.0.1', '--port', "$WebPort") (Join-Path $projectRoot 'frontend')
    Start-Sleep -Seconds 2
    foreach ($entry in $entries) {
        if (-not (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue)) { throw "$($entry.name) 启动失败，请查看 .runtime 日志" }
    }
    Write-Host "访问 http://127.0.0.1:$WebPort，API 文档 http://127.0.0.1:$ApiPort/docs"
    Write-Host '服务日志保存在 .runtime；停止时运行 scripts\stop.ps1'
} catch {
    & (Join-Path $PSScriptRoot 'stop.ps1')
    throw
}
