# 검색 창 디자인 스크린샷을 사용자 세션에서 찍는다(더미 데이터, 포커스를 가져가지 않음).
param([int]$TimeoutSec = 90)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'tmp\render'
New-Item -ItemType Directory -Force $out | Out-Null
Get-ChildItem $out -File | Remove-Item -Force
$pyw = (Get-Command pythonw.exe).Source
$taskName = 'ClipboardHistoryRender'
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$root\tests\render_preview.py`" `"$out`"" -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
try {
    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not (Test-Path "$out\render-done.txt") -and -not (Test-Path "$out\render-error.txt") -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Test-Path "$out\render-error.txt") { Get-Content "$out\render-error.txt" -Encoding UTF8; exit 1 }
    if (-not (Test-Path "$out\render-done.txt")) { "스크린샷이 $TimeoutSec 초 안에 끝나지 않았습니다."; exit 1 }
    Get-ChildItem $out -Filter *.png | Select-Object -ExpandProperty FullName
} finally {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
