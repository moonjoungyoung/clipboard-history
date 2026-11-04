"""클립보드 형식 처리(순수 로직): 직렬화, DIB→PNG 썸네일, 파일 목록(CF_HDROP)."""
from __future__ import annotations

import struct
import zlib

# 이름으로 저장한다. 표준 형식은 아래 이름을, 등록 형식은 등록 이름을 그대로 쓴다.
CF_STANDARD = {"CF_UNICODETEXT": 13, "CF_DIB": 8, "CF_DIBV5": 17, "CF_HDROP": 15}
TEXT_EXTRAS = ("HTML Format", "Rich Text Format")   # 텍스트와 함께 보관하는 서식
IMAGE_EXTRAS = ("PNG",)                             # 투명도를 지키려고 같이 보관

THUMB_MAX = 640


# ---------------------------------------------------------------- 직렬화
def pack_formats(formats: dict[str, bytes]) -> bytes:
    out = bytearray(b"CHF1")
    for name, data in formats.items():
        n = name.encode("utf-8")
        out += struct.pack("<HI", len(n), len(data)) + n + data
    return zlib.compress(bytes(out), 1)


def unpack_formats(blob: bytes) -> dict[str, bytes]:
    raw = zlib.decompress(blob)
    if raw[:4] != b"CHF1":
        raise ValueError("알 수 없는 형식 묶음")
    pos, out = 4, {}
    while pos < len(raw):
        nlen, dlen = struct.unpack_from("<HI", raw, pos)
        pos += 6
        name = raw[pos:pos + nlen].decode("utf-8")
        pos += nlen
        out[name] = raw[pos:pos + dlen]
        pos += dlen
    return out


# ---------------------------------------------------------------- 파일 목록
def parse_hdrop(data: bytes) -> list[str]:
    if len(data) < 20:
        return []
    p_files, _x, _y, _nc, wide = struct.unpack_from("<IiiII", data, 0)
    if p_files < 20 or p_files >= len(data):
        return []
    body = data[p_files:]
    text = body.decode("utf-16-le", "replace") if wide else body.decode("mbcs", "replace")
    text = text.split("\x00\x00", 1)[0]  # 목록은 NUL 두 개로 끝난다. 그 뒤 여유 영역은 무시
    return [p for p in text.split("\x00") if p]


def build_hdrop(paths: list[str]) -> bytes:
    body = ("\x00".join(paths) + "\x00\x00").encode("utf-16-le")
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + body


# ---------------------------------------------------------------- 이미지
def dib_info(dib: bytes) -> tuple[int, int, int] | None:
    """(폭, 높이, 비트수). 읽을 수 없으면 None."""
    if len(dib) < 40:
        return None
    _size, w, h, _planes, bpp = struct.unpack_from("<IiiHH", dib, 0)
    return w, abs(h), bpp


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8비트 RGB
    raw = b"".join(b"\x00" + r for r in rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def dib_thumbnail_png(dib: bytes, max_side: int = THUMB_MAX) -> bytes | None:
    """CF_DIB/CF_DIBV5 바이트로 긴 변이 max_side 이하인 PNG 썸네일을 만든다(최근접 표본)."""
    if len(dib) < 40:
        return None
    hsize, w, h, _planes, bpp, comp = struct.unpack_from("<IiiHHI", dib, 0)
    if w <= 0 or h == 0 or bpp not in (24, 32) or comp not in (0, 3) or hsize < 40:  # BI_RGB, BI_BITFIELDS
        return None
    clr_used = struct.unpack_from("<I", dib, 32)[0]
    if comp == 3:
        # 마스크가 표준 BGRA 순서일 때만 해석한다(그 외 배치는 썸네일 생략, 원본 재복사는 그대로 됨)
        if bpp != 32 or len(dib) < 52 or struct.unpack_from("<III", dib, 40) != (0xFF0000, 0xFF00, 0xFF):
            return None
    top_down = h < 0
    h = abs(h)
    offset = hsize + (12 if comp == 3 and hsize == 40 else 0) + clr_used * 4
    stride = ((w * bpp + 31) // 32) * 4
    if len(dib) < offset + stride * h:
        return None
    step = max(1, -(-max(w, h) // max_side))  # 올림 나눗셈
    tw, th = -(-w // step), -(-h // step)
    rows = []
    bytes_pp = bpp // 8
    for ty in range(th):
        sy = ty * step
        src_y = sy if top_down else h - 1 - sy
        start = offset + src_y * stride
        line = dib[start:start + w * bytes_pp]
        if bpp == 32:
            b, g, r = line[0::step * 4], line[1::step * 4], line[2::step * 4]
        else:
            b, g, r = line[0::step * 3], line[1::step * 3], line[2::step * 3]
        out = bytearray(len(r) * 3)
        out[0::3], out[1::3], out[2::3] = r, g, b
        rows.append(bytes(out))
    return _png(tw, th, rows)


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}"
