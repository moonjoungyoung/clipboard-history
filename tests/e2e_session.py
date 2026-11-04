"""실제 데스크톱 세션(세션 1)에서 도는 e2e 검증. 더미 문자열만 쓰고 임시 데이터 폴더를 쓴다.

순서: 앱 실행 → 더미 복사(제외 형식 포함) → 저장 확인 → 강제 종료 후 재시작(재부팅 가정)
     → 검색 → CLI 다시 복사 → 검색 창 열기·입력·Enter 다시 복사 → 정상 종료 → 보존 기간 정리 확인.
실행: pythonw tests\\e2e_session.py <결과 json 경로> [스크린샷 폴더]
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REAL_DIR = None
if "CLIPHIST_DATA_DIR" not in os.environ:
    REAL_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "ClipboardHistory"
TMP = Path(tempfile.mkdtemp(prefix="cliphist-e2e-"))
os.environ["CLIPHIST_DATA_DIR"] = str(TMP)
os.environ["PYTHONIOENCODING"] = "utf-8"  # 자식 CLI 출력(파이프)을 utf-8 로

from clipboard_history import win32  # noqa: E402
from clipboard_history.config import window_title  # noqa: E402
from clipboard_history.filters import SearchIndex  # noqa: E402
from clipboard_history.formats import build_hdrop, parse_hdrop  # noqa: E402
from clipboard_history.store import DAY, Store  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
PYW = str(Path(sys.executable).with_name("pythonw.exe"))
PY = str(Path(sys.executable).with_name("python.exe"))
TITLE = window_title(TMP)

user32 = win32.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]

results: dict = {"steps": [], "ok": False, "data_dir": str(TMP)}


def step(name: str, ok: bool, detail: str = "") -> None:
    results["steps"].append({"step": name, "ok": bool(ok), "detail": detail})
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def wait(pred, timeout=8.0, interval=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def start_app() -> subprocess.Popen:
    proc = subprocess.Popen([PYW, str(ROOT / "clipboard_history.pyw"), "run"], env=os.environ.copy())
    hwnd = wait(lambda: win32.FindWindowW(None, TITLE), timeout=15)
    step("앱 시작(상주 창 확인)", bool(hwnd), f"pid={proc.pid}")
    time.sleep(0.5)
    return proc


def cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "clipboard_history", *args], cwd=str(ROOT), env=os.environ.copy(),
                          capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW,
                          timeout=30)


def clip_text() -> str | None:
    return win32.read_clipboard().text


def entries(store: Store, tag: str):
    return {e.text: e for e in store.load_all() if tag in e.text}


def visible_windows_of(pid: int, title: str) -> list[int]:
    found = []

    def cb(hwnd, _):
        p = wt.DWORD()
        win32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def screenshot(hwnd, path: Path) -> None:
    try:
        from PIL import ImageGrab
    except ImportError:
        return
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(path)


def focus_hwnd(hwnd) -> int | None:
    tid = win32.GetWindowThreadProcessId(hwnd, None)
    info = win32.GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if win32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return info.hwndFocus
    return None


def dib32(w: int, h: int, seed: int) -> bytes:
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    return header + bytes(((x * 7 + y * 3 + seed) % 256) for y in range(h) for x in range(w * 4))


def read_formats(names: list[str]) -> dict:
    """테스트 확인용: 지금 클립보드에서 지정한 형식들의 원본 바이트를 읽는다."""
    out = {}
    if not win32._open_clipboard(None):
        return out
    try:
        for n in names:
            data = win32._read_bytes(win32._fmt_id(n), 64 * 1024 * 1024)
            if data is not None:
                out[n] = data
    finally:
        win32.CloseClipboard()
    return out


def real_instance_hwnd():
    if REAL_DIR is None:
        return None
    return win32.FindWindowW(None, window_title(REAL_DIR)) or None


def main() -> None:
    out = Path(sys.argv[1])
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    shots = Path(args[0]) if args else out.parent
    shots.mkdir(parents=True, exist_ok=True)
    original = win32.read_clipboard()  # 끝나고 되돌리기 위해 메모리에만 보관(출력 안 함)
    empty_before = win32.CountClipboardFormats() == 0
    if original.text is None and not empty_before:
        # 제외 표식(비밀번호 등)·미지원 형식·크기 초과면 원래 클립보드를 안전하게 되돌릴 수 없다.
        results["error"] = f"현재 클립보드를 백업할 수 없어 e2e 를 시작하지 않습니다({original.skip_reason})."
        Path(sys.argv[1]).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    tag = uuid.uuid4().hex[:8]
    A = f"CLIPHIST-E2E alpha 사과 {tag}"
    B = f"CLIPHIST-E2E beta 바나나\n둘째 줄 {tag}"
    C = f"CLIPHIST-E2E excluded-format {tag}"
    D = f"CLIPHIST-E2E no-history {tag}"
    proc = None
    store = None
    real = real_instance_hwnd()
    was_paused = win32.query_paused(real) if real else None
    if real and not was_paused:
        # 실제로 쓰고 있는 인스턴스가 더미를 기록하지 않게 테스트 동안 일시정지한다.
        win32.PostMessageW(real, win32.WM_APP_PAUSE, 1, 600)  # 테스트가 죽어도 10분 뒤 자동 재개
        time.sleep(0.3)
    results["real_instance_paused"] = bool(real)
    results["real_instance_was_paused"] = was_paused
    try:
        proc = start_app()
        store = Store(TMP)

        # 1) 복사 → 저장
        win32.set_clipboard(A); time.sleep(0.4)
        win32.set_clipboard(B); time.sleep(0.4)
        win32.set_clipboard(C, {"ExcludeClipboardContentFromMonitorProcessing": b"\x00"}); time.sleep(0.4)
        win32.set_clipboard(D, {"CanIncludeInClipboardHistory": struct.pack("<I", 0)}); time.sleep(0.4)
        win32.set_clipboard(A)
        got = wait(lambda: (lambda m: m if A in m and B in m and m[A].copy_count >= 2 else None)(entries(store, tag)))
        step("텍스트 복사 → 저장", bool(got), f"저장 {len(got or {})}개")
        got = entries(store, tag)
        step("중복 복사는 한 항목으로 합침", got[A].copy_count == 2, f"A copy_count={got[A].copy_count}")
        step("제외 형식(ExcludeClipboardContentFromMonitorProcessing) 무시", C not in got)
        step("CanIncludeInClipboardHistory=0 무시", D not in got)
        raw = b"".join(p.read_bytes() for p in TMP.iterdir() if p.name.startswith("history.db"))
        step("DB 파일에 평문 없음", tag.encode() not in raw and tag.encode("utf-16-le") not in raw)
        id_a, id_b = got[A].id, got[B].id

        # 2) 강제 종료 후 재시작(재부팅 가정)
        proc.kill(); proc.wait(10)
        step("강제 종료", bool(wait(lambda: not win32.FindWindowW(None, TITLE), 5)))
        proc = start_app()
        st = cli("stats")
        stats = json.loads(st.stdout)
        step("재시작 후 기록 유지(stats)", stats["count"] == 2 and stats["running"], f"count={stats['count']}")

        # 3) 검색
        idx = SearchIndex(store.load_all())
        r1 = [e.id for e in idx.search(f"alpha 사과 {tag}")[0]]
        r2 = [e.id for e in idx.search(f"바나나 {tag}")[0]]
        step("검색(AND, 한글)", r1 == [id_a] and r2 == [id_b], f"{r1} {r2}")
        cs = cli("search", "바나나", tag)
        step("CLI search(내용 미출력)", cs.returncode == 0 and str(id_b) in cs.stdout and "바나나" not in cs.stdout)

        # 4) CLI 로 다시 복사
        cp = cli("copy", str(id_b))
        step("CLI copy → 클립보드 반영", cp.returncode == 0 and wait(lambda: clip_text() == B, 3), cp.stderr.strip())
        step("다시 복사하면 최근으로 올라감",
             bool(wait(lambda: store.get(id_b).copy_count == 2, 3)), f"B copy_count={store.get(id_b).copy_count}")

        # 5) (선택: --ui) 검색 창 열기 → 입력 → Enter 로 다시 복사.
        #    창이 포커스를 가져가므로 사용자가 PC 를 쓰는 중이면 끄고(기본) tests/test_ui.py 로 대신 검증한다.
        if "--ui" in sys.argv:
            win32.PostMessageW(win32.FindWindowW(None, TITLE), win32.WM_APP_OPEN, 0, 0)
            wins = wait(lambda: visible_windows_of(proc.pid, "클립보드 기록"), 5)
            step("검색 창 열림", bool(wins))
            time.sleep(0.6)
            screenshot(wins[0], shots / "e2e-1-open.png")
            fh = wait(lambda: focus_hwnd(wins[0]), 3)
            step("검색 창 입력 포커스", bool(fh))
            for ch in f"alpha {tag}":
                win32.PostMessageW(fh, win32.WM_CHAR, ord(ch), 0)
            time.sleep(0.8)
            screenshot(wins[0], shots / "e2e-2-search.png")
            win32.PostMessageW(fh, win32.WM_KEYDOWN, win32.VK_RETURN, 0)
            win32.PostMessageW(fh, win32.WM_KEYUP, win32.VK_RETURN, 0xC0000001)
            step("검색 창 Enter → 클립보드 반영", bool(wait(lambda: clip_text() == A, 3)))
            step("복사 후 창 닫힘", bool(wait(lambda: not visible_windows_of(proc.pid, "클립보드 기록"), 3)))

        # 5b) 이미지 / 파일 목록 / 서식 있는 텍스트
        img = dib32(64, 32, int(tag[:2], 16))
        win32.set_clipboard_formats({"CF_DIB": img})
        got_img = wait(lambda: next((e for e in store.load_all() if e.kind == "image" and e.meta.get("w") == 64), None), 5)
        step("이미지 복사 → 저장(썸네일 포함)", bool(got_img) and store.thumb(got_img.id) is not None,
             f"meta={got_img.meta if got_img else None}")
        paths = [f"C:\\CLIPHIST-E2E-{tag}\\a.txt", f"C:\\CLIPHIST-E2E-{tag}\\b c.png"]
        win32.set_clipboard_formats({"CF_HDROP": build_hdrop(paths)})
        got_files = wait(lambda: next((e for e in store.load_all() if e.kind == "files" and tag in e.text), None), 5)
        step("파일 복사 → 저장", bool(got_files) and got_files.text.split("\n") == paths)
        H = f"CLIPHIST-E2E html {tag}"
        html = f"Version:0.9\r\nStartHTML:0\r\nEndHTML:0\r\n<b>{H}</b>".encode()
        win32.set_clipboard(H, {"HTML Format": html})
        got_html = wait(lambda: next((e for e in store.load_all() if e.text == H), None), 5)
        step("서식 있는 텍스트 → 서식도 저장", bool(got_html) and got_html.has_payload)
        cp = cli("copy", str(got_img.id))
        back = wait(lambda: read_formats(["CF_DIB"]).get("CF_DIB"), 3)
        step("이미지 다시 복사 → 원본 그대로", cp.returncode == 0 and back is not None and back[:len(img)] == img,
             cp.stderr.strip())
        cp = cli("copy", str(got_files.id))
        back = wait(lambda: read_formats(["CF_HDROP"]).get("CF_HDROP"), 3)
        step("파일 다시 복사 → 같은 경로", cp.returncode == 0 and back is not None and parse_hdrop(back) == paths)
        cp = cli("copy", str(got_html.id))
        back = wait(lambda: read_formats(["HTML Format"]).get("HTML Format"), 3)
        step("서식 있는 텍스트 다시 복사 → HTML 유지", cp.returncode == 0 and back is not None and H.encode() in back)

        # 6) 정상 종료(CLI quit)
        q = cli("quit")
        step("CLI quit 정상 종료", q.returncode == 0 and proc.wait(10) is not None, q.stderr.strip())

        # 7) 보존 기간(90일) 정리
        now = time.time()
        old, _ = store.add(f"CLIPHIST-E2E old {tag}", None, now=now - 91 * DAY)
        keep, _ = store.add(f"CLIPHIST-E2E pinned-old {tag}", None, now=now - 200 * DAY)
        store.set_pinned(keep.id, True)
        proc = start_app()
        left = {e.id for e in store.load_all()}
        step("90일 지난 항목 삭제·고정 항목 유지", old.id not in left and keep.id in left and id_a in left)
        st2 = json.loads(cli("stats").stdout)
        step("재시작 후 이미지·파일·서식 기록 유지", st2["count"] >= 5, f"count={st2['count']}")
        cli("quit"); proc.wait(10)
        results["ok"] = True
    except Exception as exc:  # noqa: BLE001
        results["error"] = f"{type(exc).__name__}: {exc}"
        results["traceback"] = traceback.format_exc()
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        if store:
            store.close()
        try:
            if empty_before:
                win32.set_clipboard_formats({})  # 원래 비어 있었으면 비워 둔다
            elif original.text is not None and original.formats is not None:
                restore = {"CF_UNICODETEXT": original.text.encode("utf-16-le", "surrogatepass") + b"\x00\x00"}
                if original.kind == "text":
                    restore.update(original.formats)
                else:
                    restore = original.formats  # 이미지·파일은 원래 형식 그대로
                win32.set_clipboard_formats(restore)
            results["clipboard_restored"] = original.text is not None
        except OSError:
            results["clipboard_restored"] = False
        if real and not was_paused:
            time.sleep(0.5)  # 되돌린 클립보드까지 기록되지 않게 잠시 뒤 재개(원래 멈춰 있었다면 그대로 둠)
            win32.PostMessageW(real, win32.WM_APP_PAUSE, 0, 0)
        log_path = TMP / "app.log"
        if log_path.exists():
            results["app_log"] = log_path.read_text(encoding="utf-8")[-4000:]
        shutil.rmtree(TMP, ignore_errors=True)
        tmp_out = out.with_suffix(".tmp")
        tmp_out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_out, out)  # 다 쓴 다음에 보이게(읽는 쪽이 반쯤 쓴 파일을 읽지 않게)


if __name__ == "__main__":
    main()
