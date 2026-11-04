# 자동 실행 해제 + 실행 중인 인스턴스 종료. -RemoveData 를 주면 저장된 기록(데이터 폴더)도 삭제한다.
param([switch]$RemoveData)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$taskName = 'ClipboardHistoryAtLogon'

# 1) 정상 종료 요청(같은 데스크톱 세션에서 실행할 때 동작)
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if ($py) {
    Push-Location $root
    try { & $py -m clipboard_history quit 2>$null | Out-Null } catch {} finally { Pop-Location }
}
# 2) 남아 있으면 프로세스 종료
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*clipboard_history.pyw*' -or $_.CommandLine -like '*-m clipboard_history run*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue; "프로세스 종료: $($_.ProcessId)" }

# 3) 예약 작업 삭제
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    "예약 작업 삭제: $taskName"
}

# 3b) use-win-v.ps1 로 Win+V 를 가져왔었다면 Windows 기본으로 되돌린다(탐색기 재시작 또는 다시 로그인 후 적용)
$adv = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced'
$dataForMarker = if ($env:CLIPHIST_DATA_DIR) { $env:CLIPHIST_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'ClipboardHistory' }
$dh = (Get-ItemProperty -Path $adv -Name DisabledHotkeys -ErrorAction SilentlyContinue).DisabledHotkeys
if ($dh -and $dh -match 'V' -and (Test-Path (Join-Path $dataForMarker 'win-v-taken'))) {
    $rest = ($dh.ToCharArray() | Where-Object { $_ -ne 'V' -and $_ -ne 'v' }) -join ''
    if ($rest) { Set-ItemProperty -Path $adv -Name DisabledHotkeys -Value $rest } else { Remove-ItemProperty -Path $adv -Name DisabledHotkeys }
    'Windows 기본 Win+V 를 되돌렸습니다. 탐색기를 다시 시작하거나 다시 로그인하면 적용됩니다.'
}

# 4) (선택) 데이터 삭제
# 앱과 같은 규칙으로 데이터 폴더를 정한다(CLIPHIST_DATA_DIR 가 있으면 그 경로)
$data = if ($env:CLIPHIST_DATA_DIR) { $env:CLIPHIST_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'ClipboardHistory' }
if ($RemoveData -and (Test-Path $data)) {
    Remove-Item -LiteralPath $data -Recurse -Force -Confirm:$false
    "데이터 폴더 삭제: $data"
} elseif (Test-Path $data) {
    "기록은 남겨 두었습니다: $data  (지우려면 -RemoveData)"
}
