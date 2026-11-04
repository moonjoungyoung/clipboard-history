# 로그온 시 자동 실행 등록(예약 작업 ClipboardHistoryAtLogon) 후 바로 시작한다.
# 관리자 권한 불필요. 다시 실행하면 설정을 덮어쓴다(Python 경로가 바뀌었을 때 재실행).
param([switch]$NoStart)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$taskName = 'ClipboardHistoryAtLogon'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw 'pythonw.exe 를 찾지 못했습니다. Python 3.10 이상을 설치하고 PATH 에 넣어 주세요.' }
& ($pyw -replace 'pythonw\.exe$', 'python.exe') -c "import sys; assert sys.version_info >= (3, 10)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 이상이 필요합니다.' }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$root\clipboard_history.pyw`" run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT15S'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$settings.Priority = 4  # 예약 작업 기본값(7, 보통 미만) 대신 일반 앱과 같은 우선순위
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description '클립보드 텍스트 기록기(로컬 저장, 외부 전송 없음)' -Force -ErrorAction Stop | Out-Null
"예약 작업 등록: $taskName ($user 로그온 15초 후 실행)"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $taskName
    '지금 시작했습니다. 트레이 아이콘을 확인하세요. 검색 창: Win+Alt+V'
}
"데이터 폴더: $env:LOCALAPPDATA\ClipboardHistory"
