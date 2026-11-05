# 사용자 데스크톱 세션에서 e2e 를 돌리고 결과를 출력한다.
# (SSH·서비스 같은 세션 0 셸은 사용자 클립보드에 접근할 수 없어서 일회용 예약 작업으로 실행)
param([int]$TimeoutSec = 240, [string]$Script = 'e2e_session.py', [switch]$DiscardClipboard)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $root 'tmp'
New-Item -ItemType Directory -Force $outDir | Out-Null
$result = Join-Path $outDir 'e2e-result.json'
if (Test-Path $result) { Remove-Item $result }

$pyw = (Get-Command pythonw.exe).Source
$taskName = 'ClipboardHistoryE2E'
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$root\tests\$Script`" `"$result`" `"$outDir`"$(if ($DiscardClipboard) { ' --discard-clipboard' })" -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
try {
    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not (Test-Path $result) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Path $result)) { throw "e2e 결과 파일이 $TimeoutSec 초 안에 생기지 않았습니다. (실행 중인 인스턴스는 최대 10분 뒤 자동으로 기록을 재개합니다)" }
    $r = Get-Content $result -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($s in $r.steps) { '{0} {1} {2}' -f ($(if ($s.ok) { 'PASS' } else { 'FAIL' })), $s.step, $s.detail }
    if ($r.error) { "ERROR: $($r.error)"; $r.traceback }
    "clipboard_restored=$($r.clipboard_restored)"
    if (-not $r.ok) { exit 1 }
} finally {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue  # 시간 초과여도 테스트가 클립보드를 계속 바꾸지 않게
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
