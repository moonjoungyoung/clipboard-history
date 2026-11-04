"""DPAPI(현재 Windows 사용자 키) 암호화와 중복 판별용 HMAC 키."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import os
from pathlib import Path

_ENTROPY = b"clipboard-history/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wt.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _call(fn, data: bytes) -> bytes:
    src, _keep = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = DATA_BLOB()
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None,
              CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "DPAPI 호출 실패")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    return _call(_crypt32.CryptProtectData, data)


def unprotect(data: bytes) -> bytes:
    return _call(_crypt32.CryptUnprotectData, data)


# DPAPI 가 "이 데이터는 이 계정 키로 풀 수 없다"고 답하는 오류들(일시적 I/O 오류와 구분)
_KEY_INVALID = {0x80090005, 0x8009000B, 13}  # NTE_BAD_DATA, NTE_BAD_KEY_STATE, ERROR_INVALID_DATA


class KeyInvalid(Exception):
    """key.bin 은 읽혔지만 현재 Windows 계정 키로 풀 수 없음(비밀번호 강제 재설정, 다른 PC 등)."""


def load_or_create_key(path: Path) -> bytes:
    """중복 판별용 HMAC 키. 파일에는 DPAPI 로 감싼 값만 저장한다."""
    if path.exists():
        data = path.read_bytes()  # 읽기 자체가 실패하면(잠김 등) 그대로 예외: 복구 대상 아님
        try:
            return unprotect(data)
        except OSError as exc:
            code = (exc.errno or 0) & 0xFFFFFFFF
            if code in _KEY_INVALID or len(data) < 16:
                raise KeyInvalid(f"0x{code:08x}") from exc
            raise
    key = os.urandom(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(protect(key))
    os.replace(tmp, path)
    return key


def digest(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()


def digest_bytes(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()
