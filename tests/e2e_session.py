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
from clipboard_history.config import load_config  # noqa: E402
from clipboard_history.filters import Snapshot, compile_patterns, should_store  # noqa: E402
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
    if REAL_DIR is not None and (REAL_DIR / "config.json").exists() and not (TMP / "config.json").exists():
        cfg = json.loads((REAL_DIR / "config.json").read_text(encoding="utf-8-sig"))
        cfg["hotkey"] = "ctrl+alt+shift+f12"  # 실제 인스턴스의 단축키와 겹치지 않게
        (TMP / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
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


class UserInterrupted(Exception):
    """테스트가 아닌 쪽(사용자)이 테스트 도중 클립보드를 바꿨다."""


class Session:
    """실제 사용자의 클립보드·기록을 지키는 e2e 공통 준비/정리.

    - 시작: 원래 클립보드를 메모리에 백업(백업 못 하면 시작 안 함), 실제 인스턴스 일시정지(10분 뒤 자동 재개)
    - 진행: 테스트가 쓴 직후의 클립보드 변경 번호를 기억하고, 그 사이 누가 바꾸면 UserInterrupted 로 즉시 중단
    - 끝: 테스트용 인스턴스가 받은 것 중 테스트 더미가 아닌 항목(=그 사이 사용자가 복사한 것)을 실제 기록으로 옮기고,
          사용자가 클립보드를 건드리지 않았을 때만 원래 클립보드를 되돌린 뒤 실제 인스턴스를 재개·다시 불러오기
    """

    # 되돌릴 때 빠져도 되는 형식(Windows 가 자동으로 만들거나, 출처 표시용)
    IGNORABLE = {"DataObject", "Ole Private Data", "Object Descriptor", "Link Source Descriptor",
                 "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard", "Preferred DropEffect",
                 "Shell IDList Array", "FileName", "FileNameW", "Shell Object Offsets", "UniformResourceLocatorW",
                 "UniformResourceLocator", "text/x-moz-url-priv"}

    def __init__(self):
        self.real = real_instance_hwnd()
        self.real_cfg = load_config(REAL_DIR)[0] if REAL_DIR is not None else None
        excl = frozenset(p.lower() for p in (self.real_cfg.exclude_processes if self.real_cfg else ()))
        self.original = win32.read_clipboard(None, exclude_processes=excl)  # 실제 설정의 제외 앱은 읽지 않음
        self.empty_before = win32.CountClipboardFormats() == 0
        missing = self._unrestorable()
        # --discard-clipboard: 사용자가 '지금 클립보드는 버려도 된다'고 한 경우. 백업 확인 없이 시작하고 끝에 비워 둔다.
        self.discard = "--discard-clipboard" in sys.argv
        self.ok_to_start = (self.original.text is not None and not missing) or self.empty_before or self.discard
        if missing and self.original.text is not None:
            self.original.skip_reason = "되돌릴 수 없는 형식: " + ", ".join(sorted(missing)[:5])
        self.seq = win32.GetClipboardSequenceNumber()
        self.init_seq = self.seq
        self.test_ids: set[int] = set()
        self.dummy_blobs: list[bytes] = []   # 테스트가 넣은 이미지 원본(옮기기에서 제외)
        self._beat = 0.0
        self.touched = False
        self.was_paused = win32.query_paused(self.real) if self.real else None

    def _unrestorable(self) -> set[str]:
        """원래 클립보드에 있지만 백업에 들어가지 않은 형식(그대로 되돌릴 수 없음)."""
        if not win32._open_clipboard(None):
            return {"(클립보드를 열 수 없음)"}
        try:
            names = set(win32._format_order())
        finally:
            win32.CloseClipboard()
        have = set(self.original.formats or {}) | ({"CF_UNICODETEXT"} if self.original.kind == "text" else set())
        if self.original.kind == "image":
            have |= {"CF_DIB", "CF_DIBV5", "PNG"}
        return {n for n in names - have if n not in self.IGNORABLE and not n.startswith("Chromium internal")
                and n not in ("ExcludeClipboardContentFromMonitorProcessing",)}

    def start(self) -> None:
        if win32.GetClipboardSequenceNumber() != self.init_seq:
            self.touched = True
            raise UserInterrupted("시작 직전에 클립보드가 바뀌어 시작하지 않았습니다(다시 실행하면 됩니다).")
        if self.real and not self.was_paused:
            win32.PostMessageW(self.real, win32.WM_APP_PAUSE, 1, 600)  # 테스트가 죽어도 10분 뒤 자동 재개
            time.sleep(0.3)
        results["real_instance_paused"] = bool(self.real)
        results["real_instance_was_paused"] = self.was_paused
        self.own()

    def own(self) -> None:
        """방금 클립보드를 바꾼 것은 테스트 자신이다."""
        self.seq = win32.GetClipboardSequenceNumber()

    def own_if(self, ours) -> None:
        """다른 앱(Office)이 테스트를 위해 클립보드를 바꾼 직후: 지금 내용이 정말 테스트 것일 때만 자기 것으로 인정."""
        seq = win32.GetClipboardSequenceNumber()  # 순번을 먼저 재고, 내용 확인 뒤에도 그대로인지 본다
        if not ours() or win32.GetClipboardSequenceNumber() != seq:
            self.touched = True
            raise UserInterrupted("테스트 도중 사용자가 클립보드를 사용해 중단했습니다(다시 실행하면 됩니다).")
        self.seq = seq

    def heartbeat(self) -> None:
        """실제 인스턴스의 자동 재개(10분)가 긴 테스트 도중에 오지 않게 30초마다 일시정지를 다시 보낸다."""
        if self.real and not self.was_paused and time.time() - self._beat > 30:
            win32.PostMessageW(self.real, win32.WM_APP_PAUSE, 1, 600)
            self._beat = time.time()

    def check(self) -> None:
        self.heartbeat()
        if win32.GetClipboardSequenceNumber() != self.seq:
            self.touched = True
            raise UserInterrupted("테스트 도중 사용자가 클립보드를 사용해 중단했습니다(다시 실행하면 됩니다).")

    def put(self, formats: dict) -> None:
        try:
            win32.set_clipboard_formats(formats, expect_seq=self.seq)  # 연 상태에서 순번을 확인하고 쓴다
        except win32.ClipboardChanged:
            self.touched = True
            raise UserInterrupted("테스트 도중 사용자가 클립보드를 사용해 중단했습니다(다시 실행하면 됩니다).")
        self.own()

    def cli_copy(self, entry_id: int):
        self.check()
        r = cli("copy", str(entry_id))
        self.own()
        return r

    def finish(self) -> bool:
        """정리. 반환값: 임시 기록을 지워도 되는지(옮기기에 실패하면 남겨 둔다)."""
        moved, last_user, safe_to_delete = 0, None, True
        # 원래 일시정지해 둔 상태였다면 그 동안의 복사는 기록하지 않는 것이 사용자 뜻이다
        if REAL_DIR is not None and not self.was_paused and any(TMP.glob("history.db")):
            try:
                moved, last_user = transfer_user_copies(self.test_ids, self.real_cfg, self.dummy_blobs)
            except Exception as exc:  # noqa: BLE001
                results["transfer_error"] = f"{type(exc).__name__}"
                results["kept_tmp"] = str(TMP)  # 사용자 복사의 유일한 사본일 수 있으니 지우지 않는다
                safe_to_delete = False
        results["user_copies_moved"] = moved
        if win32.GetClipboardSequenceNumber() != self.seq:
            self.touched = True
        results["user_touched_clipboard"] = self.touched
        try:
            if self.touched:
                results["clipboard_restored"] = False  # 사용자가 새로 복사한 것을 덮어쓰지 않는다
            elif last_user:
                # 테스트 중 사용자가 복사했지만 테스트 복사에 덮였다면, 테스트 전 내용이 아니라 그 마지막 복사를 되돌린다
                win32.set_clipboard_formats(last_user)
                results["clipboard_restored"] = "last_user_copy"
            elif self.empty_before or self.discard:
                win32.set_clipboard_formats({})
                results["clipboard_restored"] = "emptied" if self.discard else True
            elif self.original.text is not None:
                o = self.original
                restore = dict(o.formats) if o.formats else {}
                if o.kind == "text" and "CF_UNICODETEXT" not in restore:
                    restore = {"CF_UNICODETEXT": o.text.encode("utf-16-le", "surrogatepass") + b"\x00\x00", **restore}
                win32.set_clipboard_formats(restore)
                results["clipboard_restored"] = True
        except OSError:
            results["clipboard_restored"] = False
        if self.real:
            if not self.was_paused:
                time.sleep(0.5)  # 되돌린 클립보드까지 기록되지 않게 잠시 뒤 재개(원래 멈춰 있었다면 그대로 둠)
                win32.PostMessageW(self.real, win32.WM_APP_PAUSE, 0, 0)
            if moved:
                win32.PostMessageW(self.real, win32.WM_APP_RELOAD, 0, 0)
        return safe_to_delete


def transfer_user_copies(test_ids: set[int], cfg=None, dummy_blobs=()) -> tuple[int, dict | None]:
    """테스트용 기록 중 테스트가 만든 것이 아닌 항목을 실제 기록으로 옮긴다(내용은 출력하지 않음).

    반환: (옮긴 개수, 마지막 사용자 복사의 클립보드 형식 묶음)
    """
    src, dst = Store(TMP), Store(REAL_DIR)
    n, last = 0, None
    procs = cfg.exclude_processes if cfg else []
    pats = compile_patterns(cfg.exclude_patterns) if cfg else []
    try:
        for e in sorted(src.load_all(), key=lambda e: e.last_used_at):
            if e.id in test_ids or "CLIPHIST-E2E" in e.text:
                continue
            if should_store(Snapshot(e.text, e.source, None, kind=e.kind), procs, pats):
                continue  # 실제 설정에서 제외하는 앱·패턴이면 옮기지 않는다
            fm = src.payload(e.id) if e.has_payload else None
            identity = None
            if e.kind == "image":
                identity = (fm or {}).get("CF_DIB") or (fm or {}).get("PNG")
                if identity in dummy_blobs:
                    continue  # 테스트가 넣은 더미 이미지
            elif e.kind == "files":
                identity = e.text.encode("utf-8", "surrogatepass")
            moved, _ = dst.add(e.text, e.source, now=e.last_used_at, kind=e.kind, formats=fm or None,
                               thumb=src.thumb(e.id), meta=e.meta, identity=identity)
            last = dst.formats_for(moved)
            n += 1
    finally:
        src.close()
        dst.close()
    return n, last


def main() -> None:
    out = Path(sys.argv[1])
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    shots = Path(args[0]) if args else out.parent
    shots.mkdir(parents=True, exist_ok=True)
    sess = Session()  # 원래 클립보드는 메모리에만 보관(출력 안 함)
    if not sess.ok_to_start:
        # 제외 표식(비밀번호 등)·미지원 형식·크기 초과면 원래 클립보드를 안전하게 되돌릴 수 없다.
        results["error"] = f"현재 클립보드를 백업할 수 없어 e2e 를 시작하지 않습니다({sess.original.skip_reason})."
        Path(sys.argv[1]).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    tag = uuid.uuid4().hex[:8]
    A = f"CLIPHIST-E2E alpha 사과 {tag}"
    B = f"CLIPHIST-E2E beta 바나나\n둘째 줄 {tag}"
    C = f"CLIPHIST-E2E excluded-format {tag}"
    D = f"CLIPHIST-E2E no-history {tag}"
    proc = None
    store = None
    try:
        sess.start()
        proc = start_app()
        store = Store(TMP)

        # 1) 복사 → 저장
        def text(s, **extra):
            return {"CF_UNICODETEXT": s.encode("utf-16-le") + b"\x00\x00", **extra}

        sess.put(text(A)); time.sleep(0.4)
        sess.put(text(B)); time.sleep(0.4)
        sess.put(text(C, ExcludeClipboardContentFromMonitorProcessing=b"\x00")); time.sleep(0.4)
        sess.put(text(D, CanIncludeInClipboardHistory=struct.pack("<I", 0))); time.sleep(0.4)
        sess.put(text(A))
        got = wait(lambda: (lambda m: m if A in m and B in m and m[A].copy_count >= 2 else None)(entries(store, tag)))
        step("텍스트 복사 → 저장", bool(got), f"저장 {len(got or {})}개")
        got = entries(store, tag)
        step("중복 복사는 한 항목으로 합침", got[A].copy_count == 2, f"A copy_count={got[A].copy_count}")
        step("제외 형식(ExcludeClipboardContentFromMonitorProcessing) 무시", C not in got)
        step("CanIncludeInClipboardHistory=0 무시", D not in got)
        raw = b"".join(p.read_bytes() for p in TMP.iterdir() if p.name.startswith("history.db"))
        step("DB 파일에 평문 없음", tag.encode() not in raw and tag.encode("utf-16-le") not in raw)
        id_a, id_b = got[A].id, got[B].id
        sess.test_ids |= {id_a, id_b}

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
        cp = sess.cli_copy(id_b)
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
            sess.own_if(lambda: clip_text() == A)
            step("복사 후 창 닫힘", bool(wait(lambda: not visible_windows_of(proc.pid, "클립보드 기록"), 3)))

        # 5b) 이미지 / 파일 목록 / 서식 있는 텍스트
        img = dib32(64, 32, int(tag[:2], 16))
        sess.dummy_blobs.append(img)
        sess.put({"CF_DIB": img})
        got_img = wait(lambda: next((e for e in store.load_all() if e.kind == "image" and e.meta.get("w") == 64), None), 5)
        if got_img:
            sess.test_ids.add(got_img.id)
        step("이미지 복사 → 저장(썸네일 포함)", bool(got_img) and store.thumb(got_img.id) is not None,
             f"meta={got_img.meta if got_img else None}")
        paths = [f"C:\\CLIPHIST-E2E-{tag}\\a.txt", f"C:\\CLIPHIST-E2E-{tag}\\b c.png"]
        sess.put({"CF_HDROP": build_hdrop(paths)})
        got_files = wait(lambda: next((e for e in store.load_all() if e.kind == "files" and tag in e.text), None), 5)
        step("파일 복사 → 저장", bool(got_files) and got_files.text.split("\n") == paths)
        H = f"CLIPHIST-E2E html {tag}"
        html = f"Version:0.9\r\nStartHTML:0\r\nEndHTML:0\r\n<b>{H}</b>".encode()
        sess.put(text(H, **{"HTML Format": html}))
        got_html = wait(lambda: next((e for e in store.load_all() if e.text == H), None), 5)
        step("서식 있는 텍스트 → 서식도 저장", bool(got_html) and got_html.has_payload)
        cp = sess.cli_copy(got_img.id)
        back = wait(lambda: read_formats(["CF_DIB"]).get("CF_DIB"), 3)
        step("이미지 다시 복사 → 원본 그대로", cp.returncode == 0 and back is not None and back[:len(img)] == img,
             cp.stderr.strip())
        cp = sess.cli_copy(got_files.id)
        back = wait(lambda: read_formats(["CF_HDROP"]).get("CF_HDROP"), 3)
        step("파일 다시 복사 → 같은 경로", cp.returncode == 0 and back is not None and parse_hdrop(back) == paths)
        cp = sess.cli_copy(got_html.id)
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
        sess.check()
        results["ok"] = True
    except UserInterrupted as exc:
        results["interrupted"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        results["error"] = f"{type(exc).__name__}: {exc}"
        results["traceback"] = traceback.format_exc()
    finally:
        if proc and proc.poll() is None:
            cli("quit")
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if store:
            store.close()
        keep = not sess.finish()
        log_path = TMP / "app.log"
        if log_path.exists():
            results["app_log"] = log_path.read_text(encoding="utf-8")[-4000:]
        if not keep:
            shutil.rmtree(TMP, ignore_errors=True)
        tmp_out = out.with_suffix(".tmp")
        tmp_out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_out, out)  # 다 쓴 다음에 보이게(읽는 쪽이 반쯤 쓴 파일을 읽지 않게)


if __name__ == "__main__":
    main()
