"""검색 창 디자인 확인용 스크린샷(더미 데이터). 사용자 세션에서 실행해야 화면이 잡힌다.

창을 띄울 때 포커스를 가져가지 않도록 하고, 앞 창이 바뀌면 바로 되돌린다.
실행: pythonw tests\\render_preview.py <출력 폴더>
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipboard_history import theme, ui, win32  # noqa: E402
from clipboard_history.filters import SearchIndex  # noqa: E402
from clipboard_history.formats import dib_thumbnail_png  # noqa: E402
from clipboard_history.store import DAY, Entry  # noqa: E402

user32 = win32.user32
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000


def gradient_dib(w=320, h=200) -> bytes:
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    px = bytearray()
    for y in range(h):
        for x in range(w):
            px += bytes((200 - y // 2, 120 + x // 4, 60 + x // 3, 255))
    return header + bytes(px)


def dummy_entries(now: float):
    T = [
        "회의록 초안: 3분기 로드맵 정리. 검색 개편, 결제 모듈 교체, 온보딩 개선 순서로 진행",
        "https://github.com/example/clipboard-history/pull/12",
        "def add(a, b):\n    return a + b\n\nprint(add(2, 3))",
        "배송지: 서울시 성동구 예시로 12, 3층 (더미 주소)",
        "Thanks for the update! I'll review the draft report tonight.",
        "SELECT id, name FROM users WHERE created_at > now() - interval '7 days';",
        "주간 보고서: 지표 요약과 다음 주 계획",
        "오늘 저녁 7시 보고서 리뷰 미팅",
    ]
    ago = [300, 2400, 7200, 26 * 3600, 3 * DAY, 9 * DAY, 20 * DAY, 45 * DAY]
    es = []
    for i, (t, a) in enumerate(zip(T, ago), start=1):
        es.append(Entry(i, t, now - a - 3600, now - a, 1 + (i % 3 == 0) * 2, False, ["chrome.exe", "Code.exe", "KakaoTalk.exe", None][i % 4]))
    es[6].pinned = True
    es.append(Entry(20, "이미지 1920×1080", now - 1200, now - 1200, 1, False, "SnippingTool.exe", "image",
                    {"w": 1920, "h": 1080, "bytes": 8_294_400}, True))
    es.append(Entry(21, "C:\\Users\\me\\Documents\\보고서 최종.pdf\nC:\\Users\\me\\Documents\\첨부.xlsx",
                    now - 5000, now - 5000, 1, False, "explorer.exe", "files", {"count": 2}, True))
    return es


def capture(root, path: Path) -> None:
    """화면을 찍지 않고 창 자신의 내용만 PrintWindow 로 받아 PNG 로 저장한다(다른 창이 찍히지 않음)."""
    from clipboard_history.formats import _png
    for _ in range(5):
        root.update()
        time.sleep(0.1)
    hwnd = win32.GetAncestor(root.winfo_id(), win32.GA_ROOT)
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    user32.GetDC.restype = wt.HDC
    user32.GetDC.argtypes = [wt.HWND]
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT
    bi = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bi_buf = ctypes.create_string_buffer(bi, 40 + 16)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.SelectObject(mem, old)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, bi_buf, 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    raw = buf.raw
    rows = []
    for y in range(h):
        line = raw[y * w * 4:(y + 1) * w * 4]
        out = bytearray(w * 3)
        out[0::3], out[1::3], out[2::3] = line[2::4], line[1::4], line[0::4]
        rows.append(bytes(out))
    path.write_bytes(_png(w, h, rows))
    if not ok:
        path.with_suffix(".printwindow-failed").write_text("1")


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    prev = win32.GetForegroundWindow()
    win32.force_foreground = lambda h: True           # 포커스를 가져가지 않는다
    thumb = dib_thumbnail_png(gradient_dib())
    now = time.time()
    shots = []
    for pal, name in ((theme.PAPER, "light"), (theme.CARBON, "dark")):
        theme.current_palette = lambda pal=pal: pal
        root = tk.Tk()
        root.withdraw()
        root.focus_force = lambda: None
        idx = SearchIndex(dummy_entries(now))
        w = ui.SearchWindow(root, idx, lambda *a: None, lambda e: None, lambda e: None,
                            lambda eid: thumb if eid == 20 else None, None, 90)
        w._maybe_hide_on_blur = lambda: None  # 포커스 없이 띄우므로 '포커스 잃으면 숨기기'를 끈다
        root.update_idletasks()
        hwnd = win32.GetAncestor(root.winfo_id(), win32.GA_ROOT)
        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
        w.show(None)
        root.attributes("-topmost", False)
        # 다른 창들 뒤(맨 아래)로 보내 사용자 화면을 가리지 않게 한다. DWM 은 가려진 창도 그려 둔다.
        user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
        user32.SetWindowPos(hwnd, wt.HWND(1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, NOSIZE|NOMOVE|NOACTIVATE
        if win32.GetForegroundWindow() != prev and prev:
            user32.SetForegroundWindow(prev)
        capture(root, out / f"ui-{name}-1-list.png")
        w.query.set("보고서")
        w.refresh()
        capture(root, out / f"ui-{name}-2-search.png")
        w.query.set("")
        w.refresh()
        img_idx = next(i for i, e in enumerate(w.results) if e.kind == "image")
        w._select(img_idx)
        capture(root, out / f"ui-{name}-3-image.png")
        root.destroy()
        shots.append(name)
    (out / "render-done.txt").write_text(",".join(shots), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        Path(sys.argv[1], "render-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
