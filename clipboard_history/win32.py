"""Win32 ctypes 바인딩: 클립보드 읽기/쓰기, 변경 감시 창, 전역 단축키, 트레이 아이콘."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import queue
import threading
import time
from typing import Callable

from .filters import Snapshot
from .formats import (CF_STANDARD, TEXT_EXTRAS, TEXT_IMAGE_MAX_CHARS, build_hdrop, dib_info,
                      dib_thumbnail_png, parse_hdrop)

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

CF_UNICODETEXT = 13
CF_DIB = 8
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_HOTKEY = 0x0312
WM_CLIPBOARDUPDATE = 0x031D
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_APP_TRAY = WM_APP + 1
WM_APP_OPEN = WM_APP + 2   # 외부(CLI)에서 검색 창 열기 요청
WM_APP_PAUSE = WM_APP + 3  # 외부에서 기록 일시정지(wParam=1)/재개(0)
WM_APP_IS_PAUSED = WM_APP + 4  # 지금 일시정지 상태인지(반환값 1/0)
WM_APP_RELOAD = WM_APP + 5     # 다른 프로세스가 DB 에 항목을 넣었으니 다시 불러오기

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
VK_SHIFT, VK_CONTROL, VK_MENU, VK_RETURN, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x0D, 0x5B, 0x5C
KEYEVENTF_KEYUP = 0x2

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10

MF_STRING, MF_SEPARATOR, MF_POPUP, MF_GRAYED = 0x0, 0x800, 0x10, 0x1
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x10, 0x40
IDI_APPLICATION = 32512
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT), ("lPrivate", wt.DWORD)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]


def _proto(dll, name, res, *args):
    fn = getattr(dll, name)
    fn.restype = res
    fn.argtypes = list(args)
    return fn


RegisterClassExW = _proto(user32, "RegisterClassExW", wt.ATOM, ctypes.POINTER(WNDCLASSEXW))
CreateWindowExW = _proto(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
DefWindowProcW = _proto(user32, "DefWindowProcW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
DestroyWindow = _proto(user32, "DestroyWindow", wt.BOOL, wt.HWND)
GetMessageW = _proto(user32, "GetMessageW", wt.BOOL, ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT)
TranslateMessage = _proto(user32, "TranslateMessage", wt.BOOL, ctypes.POINTER(MSG))
DispatchMessageW = _proto(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(MSG))
PostMessageW = _proto(user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
SendMessageTimeoutW = _proto(user32, "SendMessageTimeoutW", LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
                             wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t))
PostQuitMessage = _proto(user32, "PostQuitMessage", None, ctypes.c_int)
FindWindowW = _proto(user32, "FindWindowW", wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
RegisterWindowMessageW = _proto(user32, "RegisterWindowMessageW", wt.UINT, wt.LPCWSTR)

AddClipboardFormatListener = _proto(user32, "AddClipboardFormatListener", wt.BOOL, wt.HWND)
RemoveClipboardFormatListener = _proto(user32, "RemoveClipboardFormatListener", wt.BOOL, wt.HWND)
OpenClipboard = _proto(user32, "OpenClipboard", wt.BOOL, wt.HWND)
CloseClipboard = _proto(user32, "CloseClipboard", wt.BOOL)
EmptyClipboard = _proto(user32, "EmptyClipboard", wt.BOOL)
GetClipboardData = _proto(user32, "GetClipboardData", wt.HANDLE, wt.UINT)
SetClipboardData = _proto(user32, "SetClipboardData", wt.HANDLE, wt.UINT, wt.HANDLE)
IsClipboardFormatAvailable = _proto(user32, "IsClipboardFormatAvailable", wt.BOOL, wt.UINT)
RegisterClipboardFormatW = _proto(user32, "RegisterClipboardFormatW", wt.UINT, wt.LPCWSTR)
GetClipboardOwner = _proto(user32, "GetClipboardOwner", wt.HWND)
GetClipboardSequenceNumber = _proto(user32, "GetClipboardSequenceNumber", wt.DWORD)
EnumClipboardFormats = _proto(user32, "EnumClipboardFormats", wt.UINT, wt.UINT)
GetClipboardFormatNameW = _proto(user32, "GetClipboardFormatNameW", ctypes.c_int, wt.UINT, wt.LPWSTR, ctypes.c_int)
CountClipboardFormats = _proto(user32, "CountClipboardFormats", ctypes.c_int)

GlobalAlloc = _proto(kernel32, "GlobalAlloc", wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
GlobalLock = _proto(kernel32, "GlobalLock", wt.LPVOID, wt.HGLOBAL)
GlobalUnlock = _proto(kernel32, "GlobalUnlock", wt.BOOL, wt.HGLOBAL)
GlobalSize = _proto(kernel32, "GlobalSize", ctypes.c_size_t, wt.HGLOBAL)
GlobalFree = _proto(kernel32, "GlobalFree", wt.HGLOBAL, wt.HGLOBAL)

GetWindowThreadProcessId = _proto(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
OpenProcess = _proto(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
QueryFullProcessImageNameW = _proto(kernel32, "QueryFullProcessImageNameW", wt.BOOL,
                                    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
CloseHandle = _proto(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
CreateMutexW = _proto(kernel32, "CreateMutexW", wt.HANDLE, wt.LPVOID, wt.BOOL, wt.LPCWSTR)
GetModuleHandleW = _proto(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
GetCurrentThreadId = _proto(kernel32, "GetCurrentThreadId", wt.DWORD)

RegisterHotKey = _proto(user32, "RegisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int, wt.UINT, wt.UINT)
UnregisterHotKey = _proto(user32, "UnregisterHotKey", wt.BOOL, wt.HWND, ctypes.c_int)

Shell_NotifyIconW = _proto(shell32, "Shell_NotifyIconW", wt.BOOL, wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
LoadImageW = _proto(user32, "LoadImageW", wt.HANDLE, wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                    ctypes.c_int, ctypes.c_int, wt.UINT)
LoadIconW = _proto(user32, "LoadIconW", wt.HICON, wt.HINSTANCE, wt.LPVOID)
CreatePopupMenu = _proto(user32, "CreatePopupMenu", wt.HMENU)
AppendMenuW = _proto(user32, "AppendMenuW", wt.BOOL, wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR)
TrackPopupMenu = _proto(user32, "TrackPopupMenu", ctypes.c_int, wt.HMENU, wt.UINT,
                        ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID)
DestroyMenu = _proto(user32, "DestroyMenu", wt.BOOL, wt.HMENU)
GetCursorPos = _proto(user32, "GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
SetForegroundWindow = _proto(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
GetForegroundWindow = _proto(user32, "GetForegroundWindow", wt.HWND)
IsWindow = _proto(user32, "IsWindow", wt.BOOL, wt.HWND)
GetParent = _proto(user32, "GetParent", wt.HWND, wt.HWND)
GetAncestor = _proto(user32, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
GetAsyncKeyState = _proto(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
keybd_event = _proto(user32, "keybd_event", None, wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t)
AttachThreadInput = _proto(user32, "AttachThreadInput", wt.BOOL, wt.DWORD, wt.DWORD, wt.BOOL)
GetGUIThreadInfo = _proto(user32, "GetGUIThreadInfo", wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO))

GA_ROOT = 2
HWND_MESSAGE = wt.HWND(-3)
SM_CXSMICON = 49
GetSystemMetrics = _proto(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)

FMT_EXCLUDE = RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
FMT_CAN_INCLUDE_HISTORY = RegisterClipboardFormatW("CanIncludeInClipboardHistory")
FMT_VIEWER_IGNORE = RegisterClipboardFormatW("Clipboard Viewer Ignore")


# ---------------------------------------------------------------- 클립보드
def _open_clipboard(hwnd, attempts: int = 20) -> bool:
    for _ in range(attempts):
        if OpenClipboard(hwnd):
            return True
        time.sleep(0.03)
    return False


def _read_dword(fmt: int) -> int | None:
    h = GetClipboardData(fmt)
    if not h:
        return None
    p = GlobalLock(h)
    if not p:
        return None
    try:
        if GlobalSize(h) < 4:
            return None
        return ctypes.c_uint32.from_address(p).value
    finally:
        GlobalUnlock(h)


def process_name_of(hwnd) -> str | None:
    if not hwnd:
        return None
    pid = wt.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return None
    finally:
        CloseHandle(h)


def _read_bytes(fmt: int, cap: int) -> bytes | None:
    """열린 클립보드에서 형식 하나를 읽는다. cap 보다 크면 None."""
    if not IsClipboardFormatAvailable(fmt):
        return None
    h = GetClipboardData(fmt)
    if not h:
        return None
    size = GlobalSize(h)
    if size == 0 or size > cap:
        return None
    p = GlobalLock(h)
    if not p:
        return None
    try:
        return ctypes.string_at(p, size)
    finally:
        GlobalUnlock(h)


_CF_NAMES = {v: k for k, v in CF_STANDARD.items()}


def _format_order() -> list[str]:
    """열린 클립보드에 원래 앱이 넣은 형식 이름을 넣은 순서대로(붙여넣는 앱은 이 순서로 고른다)."""
    names, fmt = [], 0
    buf = ctypes.create_unicode_buffer(256)
    for _ in range(512):
        fmt = EnumClipboardFormats(fmt)
        if not fmt:
            break
        if fmt in _CF_NAMES:
            names.append(_CF_NAMES[fmt])
        elif GetClipboardFormatNameW(fmt, buf, 256):
            names.append(buf.value)
    return names


def _fmt_id(name: str) -> int:
    return CF_STANDARD.get(name) or RegisterClipboardFormatW(name)


TEXT_EXTRAS_MAX_CHARS = 500_000  # 이보다 긴 텍스트는 HTML/RTF 서식을 받지 않는다(원본 앱이 수백 MB 를 렌더링하지 않게)


def read_clipboard(hwnd=None, max_chars: int = 1_000_000, max_bytes: int = 64 * 1024 * 1024,
                   exclude_processes: frozenset[str] = frozenset()) -> Snapshot:
    seq = GetClipboardSequenceNumber()  # 첫 읽기 '전'에 잰다(읽는 사이 새 복사가 오면 재시도로 덮지 않게)
    snap = _read_clipboard_raw(hwnd, max_chars, max_bytes, exclude_processes)
    # 엑셀 등은 Copy 처리가 끝나기 전에 변경 알림이 와서 일부 형식(Biff12 등)을 아직 못 준다. 잠깐 뒤 다시 읽는다.
    for _ in range(4):
        if not snap.incomplete:
            break
        time.sleep(0.25)
        if GetClipboardSequenceNumber() != seq:
            break  # 그 사이 클립보드가 또 바뀜: 새 알림이 따로 온다
        again = _read_clipboard_raw(hwnd, max_chars, max_bytes, exclude_processes)
        if again.text is None:
            break
        if again.text == snap.text:
            merged = dict(again.formats)
            for k, v in snap.formats.items():  # 이전 시도에서만 받은 형식은 유지(순서는 뒤에 붙임)
                merged.setdefault(k, v)
            again.formats = merged
        snap = again
    # 썸네일은 클립보드를 닫은 뒤에 만든다(그동안 다른 앱이 클립보드를 못 여는 시간을 줄임)
    if snap.kind == "image" and snap.text is not None and "CF_DIB" in snap.formats:
        snap.thumb = dib_thumbnail_png(snap.formats["CF_DIB"])
    return snap


def _read_clipboard_raw(hwnd, max_chars: int, max_bytes: int, exclude_processes: frozenset[str]) -> Snapshot:
    """현재 클립보드를 읽는다. 제외 표식이 있으면 내용을 읽지 않고 이유만 돌려준다.

    우선순위: 파일 목록 > 텍스트(+HTML/RTF 서식) > 이미지. Win+V 와 같은 구성이다.
    """
    if not _open_clipboard(hwnd):
        return Snapshot(None, None, "open_failed")
    try:
        # 열기 전에 확인하면 그 사이 다른 앱이 클립보드를 바꿨을 때 출처를 잘못 판단한다.
        owner = GetClipboardOwner()
        source = None if (owner and hwnd and owner == hwnd) else process_name_of(owner)  # 다시 복사는 출처 유지
        if source and source.lower() in exclude_processes:
            return Snapshot(None, source, "excluded_process")  # 제외 앱이면 내용을 아예 읽지 않는다
        # 표식이 목록에 있으면 기록하지 않는다. 단, 엑셀은 이 형식을 목록에만 올리고 데이터는 NULL 로 두므로
        # (실측) 엑셀이 소유한 클립보드에서 데이터가 NULL 인 경우만 표식이 아닌 것으로 본다.
        excel_owned = (source or "").lower() == "excel.exe"
        for fmt, why in ((FMT_EXCLUDE, "excluded_format"), (FMT_VIEWER_IGNORE, "viewer_ignore")):
            if IsClipboardFormatAvailable(fmt) and not (excel_owned and not GetClipboardData(fmt)):
                return Snapshot(None, source, why)
        if IsClipboardFormatAvailable(FMT_CAN_INCLUDE_HISTORY) and _read_dword(FMT_CAN_INCLUDE_HISTORY) == 0:
            return Snapshot(None, source, "history_disallowed")

        drop = _read_bytes(CF_HDROP, 4 * 1024 * 1024)
        if drop:
            paths = parse_hdrop(drop)
            if paths:
                return Snapshot("\n".join(paths), source, None, kind="files",
                                formats={"CF_HDROP": build_hdrop(paths)}, meta={"count": len(paths)})

        if IsClipboardFormatAvailable(CF_UNICODETEXT):
            raw = _read_bytes(CF_UNICODETEXT, (max_chars + 1) * 2)
            if raw is None:
                return Snapshot(None, source, "too_large")
            raw = raw[: len(raw) // 2 * 2]  # 홀수 길이로 할당한 앱 대비
            text = raw.decode("utf-16-le", "surrogatepass").split("\x00", 1)[0]
            # 서식·엑셀 형식(과 짧은 텍스트면 함께 온 그림)을 원래 순서대로 보관한다.
            wanted = set(TEXT_EXTRAS) | {"CF_UNICODETEXT"}
            if len(text) <= TEXT_IMAGE_MAX_CHARS:
                wanted |= {"CF_DIB", "PNG"}
            formats: dict[str, bytes] = {}
            missing = False
            budget = max_bytes if len(text) <= TEXT_EXTRAS_MAX_CHARS else 0
            if budget:
                for name in _format_order():
                    if name not in wanted or name in formats:
                        continue
                    if name == "CF_UNICODETEXT":
                        formats[name] = text.encode("utf-16-le", "surrogatepass") + b"\x00\x00"
                        continue
                    if name in ("Biff8", "Biff5", "XML Spreadsheet") and "Biff12" in formats:
                        continue  # 최신 엑셀 형식을 받았으면 큰 옛 형식은 렌더링시키지 않는다
                    fid = _fmt_id(name)
                    data = _read_bytes(fid, budget)
                    if data:
                        formats[name] = data
                        budget -= len(data)
                    elif name in ("Biff12", "HTML Format", "Rich Text Format") and not GetClipboardData(fid):
                        missing = True  # 목록엔 있는데 원래 앱이 아직 데이터를 못 만들어 줌(엑셀 Copy 직후 등)
            if set(formats) == {"CF_UNICODETEXT"}:
                formats = {}  # 순수 텍스트는 따로 보관할 필요 없음
            return Snapshot(text, source, None, kind="text", formats=formats, incomplete=missing)

        dib = _read_bytes(CF_DIB, max_bytes)
        png = _read_bytes(RegisterClipboardFormatW("PNG"), max_bytes - (len(dib) if dib else 0))
        if dib or png:
            formats = {}
            if dib:
                formats["CF_DIB"] = dib
            if png:
                formats["PNG"] = png
            info = dib_info(dib) if dib else None
            w, h = (info[0], info[1]) if info else (0, 0)
            size = sum(len(v) for v in formats.values())
            label = f"이미지 {w}×{h}" if w else "이미지"
            return Snapshot(label, source, None, kind="image", formats=formats,
                            meta={"w": w, "h": h, "bytes": size})
        return Snapshot(None, source, "unsupported")
    finally:
        CloseClipboard()


def _hglobal(data: bytes):
    h = GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not h:
        raise MemoryError("GlobalAlloc 실패")
    p = GlobalLock(h)
    if not p:
        GlobalFree(h)
        raise MemoryError("GlobalLock 실패")
    ctypes.memmove(p, data, len(data))
    GlobalUnlock(h)
    return h


class ClipboardChanged(OSError):
    """expect_seq 와 다른 순번: 그 사이 누군가 클립보드를 바꿨다."""


def set_clipboard_formats(formats: dict[str, bytes], hwnd=None, expect_seq: int | None = None) -> None:
    """이름→바이트 형식 묶음을 한 번에 클립보드에 넣는다.

    메모리를 모두 준비한 다음에 클립보드를 비우므로, 준비 단계에서 실패하면 기존 내용이 그대로 남는다.
    hwnd 가 없으면 임시 메시지 창을 소유자로 쓴다(소유자 없이 비우면 SetClipboardData 가 실패할 수 있음).
    """
    handles = []
    try:
        for name, data in formats.items():
            handles.append((_fmt_id(name), _hglobal(data)))
    except Exception:
        for _fmt, h in handles:
            GlobalFree(h)
        raise
    temp = None
    if not hwnd:
        temp = hwnd = CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
    try:
        if not _open_clipboard(hwnd):
            for _fmt, h in handles:
                GlobalFree(h)
            raise OSError("클립보드를 열 수 없습니다(다른 프로그램이 사용 중).")
        try:
            if expect_seq is not None and GetClipboardSequenceNumber() != expect_seq:
                # 클립보드를 연(=다른 쓰기를 막은) 상태에서 확인하므로 확인과 쓰기 사이에 틈이 없다
                for _fmt, h in handles:
                    GlobalFree(h)
                raise ClipboardChanged("클립보드가 그 사이 바뀌었습니다.")
            if not EmptyClipboard():
                for _fmt, h in handles:
                    GlobalFree(h)
                raise OSError(ctypes.get_last_error(), "EmptyClipboard 실패")
            for i, (fmt, h) in enumerate(handles):
                if not SetClipboardData(fmt, h):
                    err = ctypes.get_last_error()
                    for _f, rest in handles[i:]:  # 넘기지 못한 것만 해제(넘긴 것은 시스템 소유)
                        GlobalFree(rest)
                    raise OSError(err, f"SetClipboardData 실패({i}/{len(handles)} 형식만 들어감)")
        finally:
            CloseClipboard()
    finally:
        if temp:
            DestroyWindow(temp)


def set_clipboard(text: str, extra: dict[str, bytes] | None = None, hwnd=None) -> None:
    """클립보드에 텍스트(와 선택적 추가 형식)를 넣는다."""
    formats = {"CF_UNICODETEXT": text.encode("utf-16-le", "surrogatepass") + b"\x00\x00"}
    formats.update(extra or {})
    set_clipboard_formats(formats, hwnd)


# ---------------------------------------------------------------- 기타
def single_instance(name: str) -> object | None:
    """이미 같은 이름의 인스턴스가 있으면 None. 아니면 뮤텍스 핸들(프로세스 종료까지 보관)."""
    ctypes.set_last_error(0)
    h = CreateMutexW(None, False, name)
    if not h:
        log.warning("뮤텍스 생성 실패(%s), 단일 실행 확인 없이 계속", ctypes.get_last_error())
        return object()
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        CloseHandle(h)
        return None
    return h


def parse_hotkey(spec: str) -> tuple[int, int]:
    mods, vk = 0, 0
    for part in spec.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "windows"):
            mods |= MOD_WIN
        elif len(part) == 1 and part.isascii() and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        elif part == "space":
            vk = 0x20
        else:
            raise ValueError(f"알 수 없는 키: {part}")
    if not vk:
        raise ValueError("단축키에 일반 키가 없습니다.")
    return mods | MOD_NOREPEAT, vk


def force_foreground(hwnd) -> bool:
    if SetForegroundWindow(hwnd) and GetForegroundWindow() == hwnd:
        return True
    # 포그라운드 잠금 우회 1: 지금 앞 창의 입력 큐에 잠깐 붙어서 전환한다(키 입력 부작용 없음).
    fg = GetForegroundWindow()
    fg_tid = GetWindowThreadProcessId(fg, None) if fg else 0
    me = GetCurrentThreadId()
    if fg_tid and fg_tid != me and AttachThreadInput(me, fg_tid, True):
        try:
            SetForegroundWindow(hwnd)
        finally:
            AttachThreadInput(me, fg_tid, False)
        if GetForegroundWindow() == hwnd:
            return True
    # 우회 2: 의미 없는 키(VK 0xFF)를 합성하면 '최근 입력' 조건이 풀린다(ALT 와 달리 메뉴가 켜지지 않음).
    log.info("포그라운드 전환 우회 사용")
    keybd_event(0xFF, 0, 0, 0)
    keybd_event(0xFF, 0, KEYEVENTF_KEYUP, 0)
    return bool(SetForegroundWindow(hwnd))


GetClassNameW = _proto(user32, "GetClassNameW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)


_CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow",
                    "mintty", "VirtualConsoleClass"}
_FOLDER_CLASSES = {"CabinetWClass", "ExploreWClass", "Progman", "WorkerW"}


def risky_paste(hwnd, kind: str, text: str) -> str | None:
    """바로 붙여넣으면 위험한 조합이면 이유(사용자에게 보여 줄 말), 아니면 None."""
    buf = ctypes.create_unicode_buffer(64)
    GetClassNameW(GetAncestor(hwnd, GA_ROOT) or hwnd, buf, 64)
    cls = buf.value
    if cls in _CONSOLE_CLASSES and kind == "text" and "\n" in text.strip():
        return "여러 줄을 터미널에 넣으면 줄마다 실행될 수 있어"
    if cls in _FOLDER_CLASSES and kind == "files":
        return "파일을 폴더·바탕화면에 붙이면 실제로 복사되므로"
    return None


def is_shell_window(hwnd) -> bool:
    """작업 표시줄·바탕화면처럼 창을 숨긴 직후 잠깐 앞에 올 수 있는 셸 창인지."""
    buf = ctypes.create_unicode_buffer(64)
    GetClassNameW(hwnd, buf, 64)
    return buf.value in ("Shell_TrayWnd", "Progman", "WorkerW", "Shell_SecondaryTrayWnd")


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


SendInput = _proto(user32, "SendInput", wt.UINT, wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)


def send_ctrl_v() -> None:
    """Ctrl 누름·V 누름·V 뗌·Ctrl 뗌을 한 번에 넣는다(중간에 사용자 키 입력이 끼지 않음)."""
    seq = [(VK_CONTROL, 0), (ord("V"), 0), (ord("V"), KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)]
    arr = (_INPUT * len(seq))()
    for i, (vk, flags) in enumerate(seq):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].u.ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
    if SendInput(len(seq), arr, ctypes.sizeof(_INPUT)) != len(seq):
        log.info("붙여넣기 키 입력이 막혔습니다(관리자 권한 창 등)")


def key_down(vk: int) -> bool:
    return bool(GetAsyncKeyState(vk) & 0x8000)


# ---------------------------------------------------------------- 상주 창
MENU_OPEN, MENU_PAUSE, MENU_FOLDER, MENU_QUIT = 1, 2, 3, 4
MENU_RECENT_BASE = 1000


class NativeHost:
    """숨은 최상위 창 하나로 클립보드 변경·단축키·트레이를 처리하는 스레드.

    이벤트는 emit(kind, payload) 로 내보낸다(메인 스레드 큐에 넣는 함수).
      ("clip", Snapshot) / ("open", 이전 포그라운드 hwnd) / ("menu", 명령) /
      ("recent", entry_id) / ("hotkey_failed", spec) / ("quit", None)
    """

    def __init__(self, title: str, emit: Callable[[str, object], None], hotkey: str,
                 icon_path: str | None, paused_icon_path: str | None, max_chars: int,
                 max_bytes: int = 64 * 1024 * 1024, exclude_processes=()):
        self.title = title
        self.exclude_processes = frozenset(p.lower() for p in exclude_processes)
        self.locate_caret = True
        self.popup_hwnd = lambda: None  # 검색 창 hwnd(메인 스레드가 채움)
        self.emit = emit
        self.hotkey = hotkey
        self.max_chars = max_chars
        self.max_bytes = max_bytes
        self.icon_path = icon_path
        self.paused_icon_path = paused_icon_path
        self.hwnd = None
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._recent: list[tuple[int, str]] = []
        self._paused = False
        self._hotkey_label = hotkey
        self._icons: dict[bool, int] = {}
        self._wndproc = WNDPROC(self._proc)  # GC 방지
        self._taskbar_created = RegisterWindowMessageW("TaskbarCreated")
        self._thread = threading.Thread(target=self._run, name="native-host", daemon=True)
        self._read_wanted = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="clip-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self.ready.wait(10)
        self._reader.start()

    def request_read(self) -> None:
        self._read_wanted.set()

    def _read_loop(self) -> None:
        """변경 알림이 연달아 와도 마지막 상태를 한 번 읽는다(앞의 것을 읽는 중에 또 바뀌면 다시 읽음)."""
        while True:
            self._read_wanted.wait()
            self._read_wanted.clear()
            with self._lock:
                paused = self._paused
            if paused or not self.hwnd:
                continue
            try:
                self.emit("clip", read_clipboard(self.hwnd, self.max_chars, self.max_bytes, self.exclude_processes))
            except Exception:  # noqa: BLE001
                log.exception("클립보드 읽기 실패")

    # 메인 스레드에서 호출
    def set_recent(self, items: list[tuple[int, str]]) -> None:
        with self._lock:
            self._recent = list(items)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused
        if self.hwnd:
            self._tray(NIM_MODIFY)

    def balloon(self, title: str, message: str) -> None:
        if not self.hwnd:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def stop(self) -> None:
        hwnd = self.hwnd
        if hwnd:
            PostMessageW(hwnd, WM_CLOSE, 0, 0)

    # ---- 스레드 내부
    def _run(self) -> None:
        try:
            self._run_inner()
        except Exception:
            log.exception("상주 창 스레드 오류")
        finally:
            self.hwnd = None
            self.ready.set()
            self.emit("quit", None)  # 스레드가 끝나면 앱도 끝낸다(좀비 방지)

    def _run_inner(self) -> None:
        hinst = GetModuleHandleW(None)
        cls = "ClipboardHistoryHostWnd"
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = cls
        RegisterClassExW(ctypes.byref(wc))  # 이미 등록돼 있어도 계속 진행
        self.hwnd = CreateWindowExW(0, cls, self.title, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            log.error("상주 창 생성 실패: %s", ctypes.get_last_error())
            return
        self._load_icons()
        if not AddClipboardFormatListener(self.hwnd):
            log.error("AddClipboardFormatListener 실패: %s", ctypes.get_last_error())
        self._register_hotkey()
        self._tray(NIM_ADD)
        self.ready.set()
        msg = MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))

    def _register_hotkey(self) -> None:
        candidates = []
        for spec in (self.hotkey, "win+alt+v", "ctrl+alt+shift+v"):
            if isinstance(spec, str) and spec.lower() not in candidates:
                candidates.append(spec.lower())
        bad_config = not isinstance(self.hotkey, str)
        for spec in candidates:
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as exc:
                log.warning("단축키 설정 오류(%s): %s", spec, exc)
                bad_config = True
                continue
            if RegisterHotKey(self.hwnd, 1, mods, vk):
                self._hotkey_label = spec
                log.info("단축키 등록: %s", spec)
                if spec != str(self.hotkey).lower():
                    self.emit("hotkey_failed", (self.hotkey, bad_config))
                return
            log.warning("단축키 등록 실패(%s): %s", spec, ctypes.get_last_error())
        self._hotkey_label = ""
        self.emit("hotkey_failed", (self.hotkey, bad_config))

    @property
    def hotkey_label(self) -> str:
        return self._hotkey_label

    def _load_icons(self) -> None:
        default = LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        for paused, path in ((False, self.icon_path), (True, self.paused_icon_path)):
            h = None
            if path and os.path.exists(path):
                size = GetSystemMetrics(SM_CXSMICON) or 16
                h = LoadImageW(None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            self._icons[paused] = h or default

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _tray(self, op: int) -> None:
        with self._lock:
            paused = self._paused
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._icons.get(paused) or 0
        tip = "클립보드 기록" + (" (일시정지)" if paused else "")
        if self._hotkey_label:
            tip += f" · {self._hotkey_label}"
        nid.szTip = tip[:127]
        Shell_NotifyIconW(op, ctypes.byref(nid))

    def _menu(self) -> None:
        with self._lock:
            recent = list(self._recent)
            paused = self._paused
        menu = CreatePopupMenu()
        sub = CreatePopupMenu()
        try:
            label = "검색 창 열기" + (f"\t{self._hotkey_label}" if self._hotkey_label else "")
            AppendMenuW(menu, MF_STRING, MENU_OPEN, label)
            AppendMenuW(menu, MF_SEPARATOR, 0, None)
            if recent:
                for i, (_eid, text) in enumerate(recent):
                    AppendMenuW(sub, MF_STRING, MENU_RECENT_BASE + i, text.replace("\t", " "))
            else:
                AppendMenuW(sub, MF_STRING | MF_GRAYED, 0, "(비어 있음)")
            AppendMenuW(menu, MF_POPUP, sub or 0, "최근 항목 다시 복사")
            AppendMenuW(menu, MF_SEPARATOR, 0, None)
            AppendMenuW(menu, MF_STRING, MENU_PAUSE, "기록 재개" if paused else "기록 일시정지")
            AppendMenuW(menu, MF_STRING, MENU_FOLDER, "데이터 폴더 열기")
            AppendMenuW(menu, MF_SEPARATOR, 0, None)
            AppendMenuW(menu, MF_STRING, MENU_QUIT, "종료")
            pt = wt.POINT()
            GetCursorPos(ctypes.byref(pt))
            SetForegroundWindow(self.hwnd)  # 메뉴 밖 클릭 시 메뉴가 닫히게
            cmd = TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                 pt.x, pt.y, 0, self.hwnd, None)
            PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            DestroyMenu(menu)  # 하위 메뉴도 함께 해제된다
        if cmd >= MENU_RECENT_BASE:
            idx = cmd - MENU_RECENT_BASE
            if idx < len(recent):
                self.emit("recent", recent[idx][0])
        elif cmd:
            self.emit("menu", cmd)

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_CLIPBOARDUPDATE:
                with self._lock:
                    paused = self._paused
                if not paused:  # 일시정지 중에는 내용을 아예 읽지 않는다
                    self._read_wanted.set()  # 실제 읽기는 읽기 스레드가(원래 앱이 늦게 렌더링해도 이 스레드는 안 막힘)
                return 0
            if msg == WM_HOTKEY:
                fg = GetForegroundWindow()
                pid = wt.DWORD()
                GetWindowThreadProcessId(fg, ctypes.byref(pid)) if fg else None
                if not fg or pid.value == os.getpid():
                    # 우리 창(검색 창·트레이 메뉴)이 앞이면 단축키를 다시 누른 것: 붙여넣을 대상은 그대로 둔다
                    self.emit("open", (None, None, True))
                else:
                    caret = caret_rect(fg) if self.locate_caret else None
                    self.emit("open", (fg, caret, False))
                return 0
            if msg == WM_APP_OPEN:
                self.emit("open", (None, None, False))
                return 0
            if msg == WM_APP_RELOAD:
                self.emit("reload", None)
                return 0
            if msg == WM_APP_IS_PAUSED:
                with self._lock:
                    return 1 if self._paused else 0
            if msg == WM_APP_PAUSE:
                self.emit("pause", (bool(wparam), int(lparam)))  # lParam: 자동 재개까지 초(0 이면 없음)
                return 0
            if msg == WM_APP_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.emit("open", (None, None, False))  # 트레이로 열면 붙여넣을 대상 없음(복사만)
                elif lparam == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == self._taskbar_created and msg:
                self._tray(NIM_ADD)  # 탐색기 재시작 후 아이콘 복구
                return 0
            if msg == WM_CLOSE:
                DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                RemoveClipboardFormatListener(hwnd)
                UnregisterHotKey(hwnd, 1)
                Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
                self.hwnd = None
                PostQuitMessage(0)
                return 0
        except Exception:  # 창 프로시저에서 예외가 새면 안 된다
            log.exception("창 메시지 처리 오류(msg=0x%x)", msg)
            return 0
        return DefWindowProcW(hwnd, msg, wparam, lparam)


# ---------------------------------------------------------------- DWM (Windows 11 창 모양)
dwmapi = ctypes.WinDLL("dwmapi")
DwmSetWindowAttribute = _proto(dwmapi, "DwmSetWindowAttribute", ctypes.c_long, wt.HWND, wt.DWORD,
                               ctypes.c_void_p, wt.DWORD)
DWMWA_TRANSITIONS_FORCEDISABLED = 3
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2


def _dwm_int(hwnd, attr: int, value: int) -> None:
    v = ctypes.c_int(value)
    DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))


def style_popup(hwnd, border_hex: str, dark: bool) -> None:
    """둥근 모서리 + 얇은 테두리색 + (다크면) 다크 모드. Windows 10 에서는 조용히 무시된다."""
    try:
        _dwm_int(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        _dwm_int(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
        _dwm_int(hwnd, DWMWA_TRANSITIONS_FORCEDISABLED, 1)  # 단축키 창은 애니메이션 없이 바로
        r, g, b = int(border_hex[1:3], 16), int(border_hex[3:5], 16), int(border_hex[5:7], 16)
        _dwm_int(hwnd, DWMWA_BORDER_COLOR, r | (g << 8) | (b << 16))
    except OSError:
        pass


# ---------------------------------------------------------------- 입력 커서(캐럿) 위치
ClientToScreen = _proto(user32, "ClientToScreen", wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT))
ole32 = ctypes.WinDLL("ole32")
oleacc = ctypes.WinDLL("oleacc")
CoInitializeEx = _proto(ole32, "CoInitializeEx", ctypes.c_long, wt.LPVOID, wt.DWORD)
CoUninitialize = _proto(ole32, "CoUninitialize", None)
AccessibleObjectFromWindow = _proto(oleacc, "AccessibleObjectFromWindow", ctypes.c_long,
                                    wt.HWND, wt.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
OBJID_CARET = 0xFFFFFFF8
IID_IAccessible = GUID(0x618736E0, 0x3C3D, 0x11CF, (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))


class _VARIANT_VALUE(ctypes.Union):
    # BRECORD(포인터 2개)가 가장 커서 x64 에서 16바이트, x86 에서 8바이트
    _fields_ = [("lVal", ctypes.c_long), ("llVal", ctypes.c_longlong), ("rec", ctypes.c_void_p * 2)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("val", _VARIANT_VALUE)]


assert ctypes.sizeof(VARIANT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)


_ACC_LOCATION = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long),
                                   ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_long),
                                   ctypes.POINTER(ctypes.c_long), VARIANT)
_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


GUI_CARETBLINKING = 0x1
try:
    SetThreadDpiAwarenessContext = _proto(user32, "SetThreadDpiAwarenessContext", wt.HANDLE, wt.HANDLE)
    GetWindowDpiAwarenessContext = _proto(user32, "GetWindowDpiAwarenessContext", wt.HANDLE, wt.HWND)
    LogicalToPhysicalPointForPerMonitorDPI = _proto(user32, "LogicalToPhysicalPointForPerMonitorDPI", wt.BOOL,
                                                    wt.HWND, ctypes.POINTER(wt.POINT))
except AttributeError:  # Windows 10 1607 이전
    SetThreadDpiAwarenessContext = None


def _client_to_physical(hwnd, pt: wt.POINT) -> bool:
    """대상 창의 클라이언트 좌표 → 물리 화면 좌표. DPI 를 모르는 앱은 논리 좌표로 주므로 그 창의 DPI 문맥에서 바꾼다."""
    if SetThreadDpiAwarenessContext is None:
        return bool(ClientToScreen(hwnd, ctypes.byref(pt)))
    old = SetThreadDpiAwarenessContext(GetWindowDpiAwarenessContext(hwnd))
    try:
        ok = ClientToScreen(hwnd, ctypes.byref(pt))
    finally:
        if old:
            SetThreadDpiAwarenessContext(old)
    if ok:
        LogicalToPhysicalPointForPerMonitorDPI(hwnd, ctypes.byref(pt))  # 이미 물리 좌표인 창이면 그대로
    return bool(ok)


def _caret_from_thread(fg, require_visible: bool = True) -> tuple[tuple[int, int, int, int] | None, int | None]:
    """(시스템 캐럿 사각형, 포커스 창). 메모장 같은 일반 Win32 앱은 여기서 바로 나온다."""
    tid = GetWindowThreadProcessId(fg, None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not tid or not GetGUIThreadInfo(tid, ctypes.byref(info)):
        return None, None
    focus = info.hwndFocus or None
    if info.hwndCaret and (info.flags & GUI_CARETBLINKING or not require_visible):  # 숨은(옛) 캐럿 위치는 쓰지 않음
        rc = info.rcCaret
        tl, br = wt.POINT(rc.left, rc.top), wt.POINT(rc.right, rc.bottom)
        if _client_to_physical(info.hwndCaret, tl) and _client_to_physical(info.hwndCaret, br):
            if br.y > tl.y:
                return (tl.x, tl.y, max(br.x, tl.x + 1), br.y), focus
    return None, focus


def _caret_from_msaa(hwnd) -> tuple[int, int, int, int] | None:
    """접근성(MSAA) 캐럿. Chrome·Edge·VS Code·Office 처럼 시스템 캐럿을 안 쓰는 앱용."""
    ptr = ctypes.c_void_p()
    if AccessibleObjectFromWindow(hwnd, OBJID_CARET, ctypes.byref(IID_IAccessible), ctypes.byref(ptr)) != 0 \
            or not ptr.value:
        return None
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    try:
        x, y, w, h = (ctypes.c_long() for _ in range(4))
        self_child = VARIANT()
        self_child.vt = 3  # VT_I4, lVal=0 → CHILDID_SELF
        hr = _ACC_LOCATION(vtbl[22])(ptr, ctypes.byref(x), ctypes.byref(y), ctypes.byref(w), ctypes.byref(h),
                                     self_child)
        if hr != 0 or (x.value == 0 and y.value == 0) or h.value <= 0:
            return None
        return x.value, y.value, x.value + max(w.value, 1), y.value + h.value
    finally:
        _RELEASE(vtbl[2])(ptr)


def _on_some_monitor(rect) -> bool:
    l, t, r, b = rect
    return bool(MonitorFromPoint(wt.POINT(l, b - 1), 0))  # MONITOR_DEFAULTTONULL


class _CaretWorker:
    """캐럿 조회 전용 스레드(COM 은 한 번만 초기화해 재사용).

    UI Automation·접근성 조회는 대상 앱에 요청을 보내므로 앱이 느리거나 멈추면 오래 걸린다. 호출 쪽은 시간 제한까지만
    기다리고, 늦게 끝난 조회는 버린다(그래도 대상 앱의 접근성 준비는 끝나 있어 다음 번은 빨라진다).
    """

    def __init__(self):
        self._jobs: "queue.Queue" = queue.Queue()
        self.busy = False
        self._thread = threading.Thread(target=self._run, name="caret", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        hr = CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED (UI Automation 클라이언트 권장)
        com_ok = hr in (0, 1)
        from . import uia
        while True:
            fg, box = self._jobs.get()
            self.busy = True
            rect = None
            try:
                rect, focus = _caret_from_thread(fg)          # 1) 시스템 캐럿(메모장 등): 즉시
                field = None
                if rect is None and com_ok:
                    rect, field = uia.focused_info()           # 2) UI Automation(Chrome·Edge·VS Code 등)
                if rect is None and com_ok:
                    for target in dict.fromkeys(h for h in (focus, fg) if h):
                        rect = _caret_from_msaa(target)        # 3) 접근성(MSAA)
                        if rect:
                            break
                if rect is None and field:
                    rect = field  # 3') 커서는 못 찾았지만 입력칸 위치는 앎: 입력칸 위나 아래에
                if rect and not _on_some_monitor(rect):
                    rect = None
            except Exception:  # noqa: BLE001  대상 앱이 이상한 값을 줘도 창은 열려야 한다
                log.debug("캐럿 조회 실패", exc_info=True)
                rect = None
            box[0] = rect
            box[1].set()
            self.busy = False

    def query(self, fg, timeout: float):
        box = [None, threading.Event()]
        self._jobs.put((fg, box))
        box[1].wait(timeout)
        return box[0]


_caret_worker: _CaretWorker | None = None


def caret_rect(fg, timeout: float = 0.25) -> tuple[int, int, int, int] | None:
    """앞 창의 텍스트 입력 커서 화면 좌표(left, top, right, bottom). 찾지 못하면 None."""
    global _caret_worker
    if not fg:
        return None
    rect, _focus = _caret_from_thread(fg)  # 가장 흔한 경우는 스레드 전환 없이 바로
    if rect and _on_some_monitor(rect):
        return rect
    if _caret_worker is None:
        _caret_worker = _CaretWorker()
    if _caret_worker.busy:
        return None  # 앞 조회가 아직 안 끝남(대상 앱이 멈춤): 기다리지 않고 가운데에 띄운다
    return _caret_worker.query(fg, timeout)


GetWindowRect = _proto(user32, "GetWindowRect", wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))


def window_rect(hwnd) -> tuple[int, int, int, int] | None:
    r = wt.RECT()
    if not hwnd or not GetWindowRect(hwnd, ctypes.byref(r)) or r.right - r.left < 50:
        return None
    return r.left, r.top, r.right, r.bottom


def work_area_at(x: int, y: int) -> tuple[int, int, int, int] | None:
    mon = MonitorFromPoint(wt.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not mon or not GetMonitorInfoW(mon, ctypes.byref(info)):
        return None
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


# ---------------------------------------------------------------- 모니터
class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


MonitorFromPoint = _proto(user32, "MonitorFromPoint", wt.HANDLE, wt.POINT, wt.DWORD)
GetMonitorInfoW = _proto(user32, "GetMonitorInfoW", wt.BOOL, wt.HANDLE, ctypes.POINTER(MONITORINFO))


def work_area_at_cursor() -> tuple[int, int, int, int] | None:
    """마우스가 있는 모니터의 작업 영역(left, top, right, bottom). 단축키 창을 그 화면에 띄운다."""
    pt = wt.POINT()
    if not GetCursorPos(ctypes.byref(pt)):
        return None
    mon = MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not mon or not GetMonitorInfoW(mon, ctypes.byref(info)):
        return None
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def query_paused(hwnd) -> bool | None:
    """실행 중인 인스턴스가 일시정지 상태인지. 응답이 없으면 None."""
    out = ctypes.c_size_t()
    if not SendMessageTimeoutW(hwnd, WM_APP_IS_PAUSED, 0, 0, 0x0002, 2000, ctypes.byref(out)):  # SMTO_ABORTIFHUNG
        return None
    return bool(out.value)
