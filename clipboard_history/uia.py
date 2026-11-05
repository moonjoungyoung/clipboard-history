"""UI Automation 으로 입력 커서 위치 찾기(Chrome·Edge·Electron 처럼 시스템 캐럿을 안 쓰는 앱용).

pywin32/comtypes 없이 ctypes 로 COM vtable 을 직접 부른다. 호출하는 스레드에서 CoInitializeEx 가 돼 있어야 한다.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

HRESULT = ctypes.c_long
ole32 = ctypes.WinDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p("{" + s + "}"), ctypes.byref(g))
    return g


CLSID_CUIAutomation = _guid("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_IUIAutomation = _guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")
IID_TextPattern2 = _guid("506a921a-fcc9-409f-b23b-37eb74106872")
IID_TextPattern = _guid("32eba289-3583-42c9-9c59-3b6d9a1e9b6a")
UIA_TextPattern2Id, UIA_TextPatternId = 10024, 10014
TextUnit_Character = 0
TextPatternRangeEndpoint_Start, TextPatternRangeEndpoint_End = 0, 1

ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wt.DWORD, ctypes.POINTER(GUID),
                                   ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = HRESULT
oleaut32.SafeArrayGetUBound.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_long)]
oleaut32.SafeArrayAccessData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
oleaut32.SafeArrayUnaccessData.argtypes = [ctypes.c_void_p]
oleaut32.SafeArrayDestroy.argtypes = [ctypes.c_void_p]


def _vt(obj: ctypes.c_void_p, index: int, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj) -> None:
    if obj and obj.value:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


def _rects(rng) -> list[tuple[float, float, float, float]]:
    """IUIAutomationTextRange::GetBoundingRectangles (vtable 10) → [(x, y, w, h), ...]"""
    sa = ctypes.c_void_p()
    if _vt(rng, 10, ctypes.POINTER(ctypes.c_void_p))(rng, ctypes.byref(sa)) != 0 or not sa.value:
        return []
    try:
        ub = ctypes.c_long()
        oleaut32.SafeArrayGetUBound(sa, 1, ctypes.byref(ub))
        n = ub.value + 1
        if n < 4:
            return []
        data = ctypes.c_void_p()
        oleaut32.SafeArrayAccessData(sa, ctypes.byref(data))
        try:
            vals = (ctypes.c_double * n).from_address(data.value)[:]
        finally:
            oleaut32.SafeArrayUnaccessData(sa)
        return [tuple(vals[i:i + 4]) for i in range(0, n - 3, 4)]
    finally:
        oleaut32.SafeArrayDestroy(sa)


def _is_degenerate(rng) -> bool:
    """범위의 시작과 끝이 같은지(=선택 없이 커서만 있음). CompareEndpoints(vtable 5)"""
    comp = ctypes.c_int(1)
    hr = _vt(rng, 5, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int))(
        rng, TextPatternRangeEndpoint_Start, rng, TextPatternRangeEndpoint_End, ctypes.byref(comp))
    return hr == 0 and comp.value == 0


def _caret_from_range(rng) -> tuple[int, int, int, int] | None:
    rects = _rects(rng)
    degenerate = _is_degenerate(rng)
    if rects and not degenerate:  # 선택 영역이 있으면 그 끝
        x, y, w, h = rects[-1]
        return int(x + w), int(y), int(x + w) + 1, int(y + h)
    if rects:
        # 커서만 있는데 사각형이 왔다: Chrome 은 빈 입력칸에서 '줄 전체' 사각형을 준다. 커서는 그 시작(왼쪽)이다.
        x, y, w, h = rects[0]
        return int(x), int(y), int(x) + 1, int(y + h)
    # 빈 범위(커서만 있음)는 사각형이 없다 → 커서 다음 글자(없으면 앞 글자)로 넓혀서 위치를 잡는다
    for endpoint, delta, use_right in ((TextPatternRangeEndpoint_End, 1, False),
                                       (TextPatternRangeEndpoint_Start, -1, True)):
        clone = ctypes.c_void_p()
        if _vt(rng, 3, ctypes.POINTER(ctypes.c_void_p))(rng, ctypes.byref(clone)) != 0:
            return None
        try:
            moved = ctypes.c_int()
            # MoveEndpointByUnit (vtable 14)
            _vt(clone, 14, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int))(
                clone, endpoint, TextUnit_Character, delta, ctypes.byref(moved))
            rects = _rects(clone) if moved.value else []
            if rects:
                x, y, w, h = rects[0]
                cx = x + w if use_right else x
                return int(cx), int(y), int(cx) + 1, int(y + h)
        finally:
            _release(clone)
    return None


# 입력칸으로 볼 컨트롤 종류. Document(50030, 웹 페이지 본문)는 '선택된 글자' 위치라 커서로 쓰지 않는다.
EDITABLE_TYPES = {50004, 50003}  # Edit, ComboBox


def focused_info():
    """(커서 사각형 또는 None, 포커스된 입력칸 경계 또는 None). 입력칸이 아니면 둘 다 None."""
    uia = ctypes.c_void_p()
    if ole32.CoCreateInstance(ctypes.byref(CLSID_CUIAutomation), None, 1, ctypes.byref(IID_IUIAutomation),
                              ctypes.byref(uia)) != 0:
        return None, None
    el = None
    try:
        el = ctypes.c_void_p()
        if _vt(uia, 8, ctypes.POINTER(ctypes.c_void_p))(uia, ctypes.byref(el)) != 0 or not el.value:
            return None, None
        ct = ctypes.c_int()
        if _vt(el, 21, ctypes.POINTER(ctypes.c_int))(el, ctypes.byref(ct)) != 0 or ct.value not in EDITABLE_TYPES:
            return None, None  # get_CurrentControlType: 입력칸이 아님
        rc = wt.RECT()
        bounds = None
        if _vt(el, 43, ctypes.POINTER(wt.RECT))(el, ctypes.byref(rc)) == 0 and rc.bottom > rc.top:
            bounds = (rc.left, rc.top, rc.right, rc.bottom)  # get_CurrentBoundingRectangle
        return _caret_of(el), bounds
    finally:
        for o in (el, uia):
            _release(o)


def caret_rect_uia() -> tuple[int, int, int, int] | None:
    """포커스된 입력칸의 텍스트 커서 화면 좌표. 못 찾으면 None. (CoInitializeEx 된 스레드에서 호출)"""
    return focused_info()[0]


def _caret_of(el) -> tuple[int, int, int, int] | None:
    """입력칸 요소의 커서 위치: TextPattern2.GetCaretRange, 없으면 TextPattern.GetSelection 의 첫 범위."""
    get_as = _vt(el, 14, ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))  # GetCurrentPatternAs
    pat2 = ctypes.c_void_p()
    if get_as(el, UIA_TextPattern2Id, ctypes.byref(IID_TextPattern2), ctypes.byref(pat2)) == 0 and pat2.value:
        rng = ctypes.c_void_p()
        try:
            active = wt.BOOL()
            # IUIAutomationTextPattern2::GetCaretRange (vtable 10)
            if _vt(pat2, 10, ctypes.POINTER(wt.BOOL), ctypes.POINTER(ctypes.c_void_p))(
                    pat2, ctypes.byref(active), ctypes.byref(rng)) == 0 and rng.value:
                return _caret_from_range(rng)
        finally:
            _release(rng)
            _release(pat2)
    pat = ctypes.c_void_p()
    if get_as(el, UIA_TextPatternId, ctypes.byref(IID_TextPattern), ctypes.byref(pat)) != 0 or not pat.value:
        return None
    arr = ctypes.c_void_p()
    rng = ctypes.c_void_p()
    try:
        if _vt(pat, 5, ctypes.POINTER(ctypes.c_void_p))(pat, ctypes.byref(arr)) != 0 or not arr.value:  # GetSelection
            return None
        length = ctypes.c_int()
        _vt(arr, 3, ctypes.POINTER(ctypes.c_int))(arr, ctypes.byref(length))  # get_Length
        if length.value < 1:
            return None
        if _vt(arr, 4, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(arr, 0, ctypes.byref(rng)) != 0:  # GetElement
            return None
        return _caret_from_range(rng)
    finally:
        _release(rng)
        _release(arr)
        _release(pat)
