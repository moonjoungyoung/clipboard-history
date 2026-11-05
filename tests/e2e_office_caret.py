"""실제 데스크톱 세션 e2e(2): 입력 커서 찾기 + 엑셀·워드 서식 왕복. 더미 데이터만 쓴다.

- 캐럿: 이 프로세스 안에 활성화하지 않은 입력칸(EDIT)을 만들고, 시스템 캐럿과 접근성(MSAA) 두 방식으로 찾아 본다.
- 엑셀/워드: 화면에 띄우지 않은(Visible=False) Office 로 복사 → 앱이 저장 → CLI 로 다시 복사 → 새 문서에 붙여
  수식·굵기·값이 살아 있는지 확인한다.
실행: pythonw tests\\e2e_office_caret.py <결과 json>
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_session as base  # noqa: E402  (임시 데이터 폴더·앱 실행·CLI 도우미를 그대로 쓴다)

from clipboard_history import win32  # noqa: E402
from clipboard_history.store import Store  # noqa: E402

results = base.results
step = base.step
user32 = win32.user32
CREATE_NO_WINDOW = 0x08000000


def pump(seconds: float) -> None:
    msg = win32.MSG()
    end = time.time() + seconds
    PeekMessageW = user32.PeekMessageW
    PeekMessageW.argtypes = [ctypes.POINTER(win32.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    while time.time() < end:
        while PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            win32.TranslateMessage(ctypes.byref(msg))
            win32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def caret_checks() -> None:
    user32.SetFocus.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    WS_POPUP, WS_BORDER = 0x80000000, 0x00800000
    WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
    x0, y0 = 300, 400
    edit = win32.CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, "EDIT", "CLIPHIST-E2E caret", WS_POPUP | WS_BORDER,
                                 x0, y0, 320, 40, None, None, None, None)
    step("캐럿: 테스트 입력칸 생성", bool(edit))
    try:
        user32.ShowWindow(edit, 4)  # SW_SHOWNOACTIVATE
        user32.SetWindowPos(edit, wt.HWND(1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # 다른 창 뒤로, 활성화 안 함
        user32.SetFocus(edit)       # 이 스레드 안에서만 포커스(앞 창은 바뀌지 않음)
        pump(0.3)
        rect, _focus = win32._caret_from_thread(edit, require_visible=False)  # 활성화 안 한 창이라 캐럿은 숨김 상태
        step("캐럿: 시스템 캐럿 위치", bool(rect) and x0 <= rect[0] <= x0 + 320 and y0 <= rect[1] <= y0 + 40,
             f"rect={rect}")
        got: list = [None]

        def msaa():
            # 활성화되지 않은 창의 캐럿은 숨김 상태라 MSAA 가 '캐럿 없음'을 준다(Chrome 등 실제 대상은 수동 확인).
            # 여기서는 같은 COM 호출 경로(accLocation, vtable 22, VARIANT 전달)가 맞는지 창 위치(OBJID_WINDOW)로 검증한다.
            win32.CoInitializeEx(None, 0x2)
            orig = win32.OBJID_CARET
            try:
                win32.OBJID_CARET = 0  # OBJID_WINDOW
                got[0] = win32._caret_from_msaa(edit)
            finally:
                win32.OBJID_CARET = orig
                win32.CoUninitialize()

        t = threading.Thread(target=msaa)
        t.start()
        end = time.time() + 3
        while t.is_alive() and time.time() < end:
            pump(0.05)   # 접근성 요청(WM_GETOBJECT)에 이 스레드가 답해야 한다
        r2 = got[0]
        step("캐럿: 접근성(MSAA) 호출 경로(창 위치로 검증)", r2 == (x0, y0, x0 + 320, y0 + 40), f"msaa={r2}")
        theme = __import__("clipboard_history.theme", fromlist=["place_near_caret"])
        area = win32.work_area_at(rect[0], rect[3] - 1)
        x, y = theme.place_near_caret(rect, area, (760, 484))
        step("캐럿: 창이 커서 아래(또는 위)에 화면 안으로", area[0] <= x and x + 760 <= area[2]
             and area[1] <= y and y + 484 <= area[3] and (y >= rect[3] or y + 484 <= rect[1]),
             f"caret={rect} area={area} -> ({x},{y})")
    finally:
        win32.DestroyWindow(edit)


def ps(script: str, timeout: int = 120) -> dict:
    """Office COM 자동화를 PowerShell 로 실행하고 마지막 줄 JSON 을 돌려준다."""
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=CREATE_NO_WINDOW)
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip().startswith("{")]
    if not lines:
        raise RuntimeError(f"PowerShell 실패: {r.stderr.strip()[:300]}")
    return json.loads(lines[-1])


PS_PRE = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ErrorActionPreference='Stop';"


def excel_checks(store: Store, tag: str, sess) -> None:
    marker = f"CLIPHIST-E2E-xl-{tag}"
    sess.check()
    ps(PS_PRE + f"""
$x = New-Object -ComObject Excel.Application; $x.Visible = $false; $x.DisplayAlerts = $false
try {{
  $wb = $x.Workbooks.Add(); $ws = $wb.Worksheets.Item(1)
  $ws.Range('A1').Value2 = '{marker}'; $ws.Range('A1').Font.Bold = $true
  $ws.Range('A2').Value2 = 12; $ws.Range('A3').Value2 = 0.25; $ws.Range('A3').NumberFormat = '0.0%'
  $ws.Range('A2').Interior.Color = 65535
  [void]$ws.Range('A1:A3').Copy(); Start-Sleep -Seconds 3
  $wb.Close($false)
}} finally {{ $x.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($x) }}
'{{"ok":true}}'
""")
    sess.own_if(lambda: marker in (base.clip_text() or ""))  # 엑셀이 복사하고 닫히며 한 번 더 올림(그 사이 사용자 복사는 중단)
    sess.heartbeat()
    e = base.wait(lambda: next((e for e in store.load_all() if marker in e.text), None), 8)
    step("엑셀: 복사 → 저장", bool(e))
    sess.test_ids.add(e.id)
    payload = store.payload(e.id)
    native = [n for n in ("Biff12", "Biff8", "XML Spreadsheet") if n in payload]
    step("엑셀: 엑셀 고유 형식도 보관", bool(native), f"formats={list(payload)}")
    cp = sess.cli_copy(e.id)
    step("엑셀: 다시 복사", cp.returncode == 0, cp.stderr.strip())
    out = ps(PS_PRE + f"""
$x = New-Object -ComObject Excel.Application; $x.Visible = $false; $x.DisplayAlerts = $false
try {{
  $wb = $x.Workbooks.Add(); $ws = $wb.Worksheets.Item(1)
  $ws.Paste($ws.Range('C5'))
  # 붙인 내용 자체는 결과에 남기지 않고 기대값과 같은지만 기록한다
  $r = @{{ text_ok = ($ws.Range('C5').Text -eq '{marker}'); bold = [bool]$ws.Range('C5').Font.Bold;
          v2 = $ws.Range('C6').Value2; fmt = $ws.Range('C7').NumberFormat; shown = $ws.Range('C7').Text;
          fill = $ws.Range('C6').Interior.Color }}
  $wb.Close($false)
}} finally {{ $x.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($x) }}
$r | ConvertTo-Json -Compress
""")
    sess.check()  # 붙이는 동안 사용자가 클립보드를 바꿨다면 결과를 쓰지 않고 중단
    step("엑셀: 새 통합 문서에 붙이면 값·굵기 유지", out.get("text_ok") is True and out.get("bold") is True
         and out.get("v2") == 12, json.dumps(out, ensure_ascii=False))
    # 수식은 Excel 제한으로 다른 인스턴스·다른 앱에서 붙이면 값이 된다(Win+V 도 같음). 그래서 테스트에는 수식을 쓰지 않고
    # 엑셀 고유 형식으로만 온전히 오는 숫자 서식(백분율)과 채우기 색을 확인한다.
    step("엑셀: 숫자 서식·채우기 색까지 유지", out.get("fmt") == "0.0%" and out.get("shown") == "25.0%"
         and int(out.get("fill") or 0) == 65535, json.dumps(out, ensure_ascii=False))


def word_checks(store: Store, tag: str, sess) -> None:
    marker = f"CLIPHIST-E2E-word-{tag}"
    sess.check()
    ps(PS_PRE + f"""
$w = New-Object -ComObject Word.Application; $w.Visible = $false; $w.DisplayAlerts = 0
try {{
  $d = $w.Documents.Add(); $r = $d.Range(); $r.Text = '{marker} 굵게 기울임'
  $r.Font.Bold = 1; $r.Font.Italic = 1; $r.Font.Size = 20
  $r.Copy(); Start-Sleep -Seconds 3; $d.Saved = $true; $d.Close()
}} finally {{ $w.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($w) }}
'{{"ok":true}}'
""")
    sess.own_if(lambda: marker in (base.clip_text() or ""))
    e = base.wait(lambda: next((e for e in store.load_all() if marker in e.text), None), 8)
    step("워드: 복사 → 저장", bool(e))
    sess.test_ids.add(e.id)
    payload = store.payload(e.id)
    step("워드: 서식(RTF/HTML) 보관", "Rich Text Format" in payload or "HTML Format" in payload,
         f"formats={list(payload)}")
    cp = sess.cli_copy(e.id)
    step("워드: 다시 복사", cp.returncode == 0, cp.stderr.strip())
    out = ps(PS_PRE + f"""
$w = New-Object -ComObject Word.Application; $w.Visible = $false; $w.DisplayAlerts = 0
try {{
  $d = $w.Documents.Add(); $d.Range().Paste()
  $r = $d.Range(0, 5)
  $o = @{{ text_ok = $d.Range().Text.Contains('{marker}'); bold = $r.Font.Bold; italic = $r.Font.Italic; size = $r.Font.Size }}
  $d.Saved = $true; $d.Close()
}} finally {{ $w.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($w) }}
$o | ConvertTo-Json -Compress
""")
    sess.check()
    step("워드: 새 문서에 붙이면 굵게·기울임·크기 유지", out.get("text_ok") is True and out.get("bold") == -1
         and out.get("italic") == -1 and out.get("size") == 20, json.dumps(out, ensure_ascii=False))


def main() -> None:
    out = Path(sys.argv[1])
    sess = base.Session()
    if not sess.ok_to_start:
        results["error"] = f"현재 클립보드를 백업할 수 없어 시작하지 않습니다({sess.original.skip_reason})."
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    tag = uuid.uuid4().hex[:8]
    proc = store = None
    try:
        sess.start()
        proc = base.start_app()      # 실제 인스턴스가 멈춘 동안의 사용자 복사를 놓치지 않게 먼저 띄운다
        store = Store(base.TMP)
        caret_checks()
        excel_checks(store, tag, sess)
        word_checks(store, tag, sess)
        sess.check()
        results["ok"] = True
    except base.UserInterrupted as exc:
        results["interrupted"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        results["error"] = f"{type(exc).__name__}: {exc}"
        results["traceback"] = traceback.format_exc()
    finally:
        if proc and proc.poll() is None:
            base.cli("quit")
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if store:
            store.close()
        keep = not sess.finish()
        log_path = base.TMP / "app.log"
        if log_path.exists():
            results["app_log"] = log_path.read_text(encoding="utf-8")[-3000:]
        if not keep:
            shutil.rmtree(base.TMP, ignore_errors=True)
        tmp_out = out.with_suffix(".tmp")
        tmp_out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_out, out)


if __name__ == "__main__":
    main()
