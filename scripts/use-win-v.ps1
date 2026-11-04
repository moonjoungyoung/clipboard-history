# Win+V 를 이 프로그램으로 가져온다(Windows 기본 클립보드 기록 창 대신 이 검색 창이 뜨게).
#
#  .\scripts\use-win-v.ps1            # 켜기: 탐색기의 Win+V 를 끄고, 이 프로그램 단축키를 win+v 로
#  .\scripts\use-win-v.ps1 -Undo      # 되돌리기: Windows 기본 Win+V 복원, 단축키 win+alt+v
#
# 원리: HKCU\...\Explorer\Advanced 의 DisabledHotkeys 에 V 를 넣으면 탐색기가 Win+V 를 등록하지 않는다.
# 탐색기를 다시 시작해야 적용된다(작업 표시줄이 잠깐 깜빡인다. 열려 있는 창은 그대로).
param([switch]$Undo, [switch]$NoRestartExplorer)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced'
$dataDir = if ($env:CLIPHIST_DATA_DIR) { $env:CLIPHIST_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'ClipboardHistory' }
$cfgPath = Join-Path $dataDir 'config.json'

# 1) 설정 파일을 먼저 바꾼다. 실패하면 레지스트리·탐색기는 건드리지 않고 멈춘다.
$py = (Get-Command python.exe).Source
$hotkey = if ($Undo) { 'win+alt+v' } else { 'win+v' }
& $py -c "import json,sys,pathlib; p=pathlib.Path(sys.argv[1]); d=json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}; assert isinstance(d, dict), 'config.json 최상위가 객체가 아닙니다'; d['hotkey']=sys.argv[2]; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')" $cfgPath $hotkey
if ($LASTEXITCODE -ne 0) { throw "config.json 을 고치지 못했습니다: $cfgPath (아무것도 바꾸지 않았습니다)" }
"단축키 설정: $hotkey"

# 2) 탐색기 Win+V 끄기/켜기
$current = (Get-ItemProperty -Path $key -Name DisabledHotkeys -ErrorAction SilentlyContinue).DisabledHotkeys
if ($null -eq $current) { $current = '' }
$marker = Join-Path $dataDir 'win-v-taken'   # 이 스크립트가 V 를 넣었는지 기록(원래 있던 V 는 되돌릴 때 지우지 않음)
if ($Undo) {
    $new = if (Test-Path $marker) { ($current.ToCharArray() | Where-Object { $_ -ne 'V' -and $_ -ne 'v' }) -join '' } else { $current }
    Remove-Item $marker -ErrorAction SilentlyContinue
} else {
    if ($current -match 'V') { $new = $current } else { $new = $current + 'V'; Set-Content -Path $marker -Value 'V' }
}
if ($new) { Set-ItemProperty -Path $key -Name DisabledHotkeys -Value $new -Type String }
else { Remove-ItemProperty -Path $key -Name DisabledHotkeys -ErrorAction SilentlyContinue }
"DisabledHotkeys = '$new'"


if (-not $NoRestartExplorer) {
    'Windows 탐색기를 다시 시작합니다(작업 표시줄이 잠깐 사라졌다 돌아오고, 열려 있던 파일 탐색기 창은 닫힙니다).'
    Stop-Process -Name explorer -Force -Confirm:$false
    Start-Sleep -Seconds 2
    if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) { Start-Process explorer.exe }
    Start-Sleep -Seconds 3
}

# 프로그램 다시 시작(새 단축키 등록)
if (Get-ScheduledTask -TaskName ClipboardHistoryAtLogon -ErrorAction SilentlyContinue) {
    Push-Location $root
    try { & $py -m clipboard_history quit 2>$null | Out-Null } catch {} finally { Pop-Location }  # 정상 종료(트레이 아이콘 정리)
    Stop-ScheduledTask -TaskName ClipboardHistoryAtLogon
    Start-Sleep -Seconds 1
    Start-ScheduledTask -TaskName ClipboardHistoryAtLogon
    '클립보드 기록을 다시 시작했습니다.'
}
if ($Undo) { '이제 Win+V 는 Windows 기본 클립보드 기록이고, 이 프로그램은 Win+Alt+V 입니다.' }
else { '이제 Win+V 로 이 프로그램이 열립니다. 되돌리려면: .\scripts\use-win-v.ps1 -Undo' }
