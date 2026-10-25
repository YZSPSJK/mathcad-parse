$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pidFile = Join-Path $projectRoot '.runtime\pids.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host '未找到本项目启动记录'; return }
$entries = Get-Content -LiteralPath $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
$processSnapshot = @(Get-CimInstance Win32_Process)
function Stop-OwnedTree([int]$ProcessId) {
    $children = @($processSnapshot | Where-Object ParentProcessId -eq $ProcessId)
    foreach ($child in $children) { Stop-OwnedTree $child.ProcessId }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($entry in $entries) {
    $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq $entry.startTicks) { Stop-OwnedTree $process.Id }
}
Push-Location -LiteralPath $projectRoot
try { & '.\.venv\Scripts\python.exe' -m backend.cleanup } finally { Pop-Location }
Remove-Item -LiteralPath $pidFile
Write-Host '项目服务已停止，源文件与版本产物保留。运行中的任务会在下次启动时标为中断。'
