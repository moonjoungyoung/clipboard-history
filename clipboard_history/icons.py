"""트레이/창 아이콘(.ico)을 코드로 그린다(외부 이미지 파일 불필요).

모양: 잉크 블루 둥근 사각형 위에 복사 용지 두 장이 겹친 모습('여러 번 복사한 기록').
크기별(16~64px)로 4×4 슈퍼샘플링해서 가장자리를 부드럽게 만든다.
"""
from __future__ import annotations

import struct
from pathlib import Path

from .paint import _sdf

SIZES = (16, 20, 24, 32, 40, 48, 64)
VERSION = "2"

ACCENT = (47, 91, 211)
PAUSED = (138, 135, 127)
WHITE = (255, 255, 255)


def _mix(a, b, t):
    return tuple(a[i] * t + b[i] * (1 - t) for i in range(3))


def _shapes(base):
    # (x0, y0, x1, y1, 반지름, 색): 단위 좌표(0~1), 그리는 순서대로
    back = _mix(WHITE, base, 0.5)
    line = _mix(base, WHITE, 0.45)
    return [
        (0.04, 0.04, 0.96, 0.96, 0.24, base),
        (0.36, 0.17, 0.79, 0.65, 0.08, back),
        (0.22, 0.31, 0.65, 0.83, 0.08, WHITE),
        (0.31, 0.47, 0.56, 0.53, 0.03, line),
        (0.31, 0.61, 0.50, 0.67, 0.03, line),
    ]


def _render(n: int, base) -> list[list[tuple[int, int, int, int]]]:
    shapes = _shapes(base)
    if n < 24:
        shapes = shapes[:3]  # 아주 작은 크기에서는 글줄을 빼야 뭉개지지 않는다
    ss = 4
    out = []
    for y in range(n):
        row = []
        for x in range(n):
            r = g = b = a = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    u = (x + (sx + 0.5) / ss) / n
                    v = (y + (sy + 0.5) / ss) / n
                    color = None
                    for x0, y0, x1, y1, rad, col in shapes:
                        w, h = x1 - x0, y1 - y0
                        if _sdf(u - x0, v - y0, w, h, rad) < 0:
                            color = col
                    if color is not None:
                        r += color[0]; g += color[1]; b += color[2]; a += 1
            k = ss * ss
            if a:
                row.append((round(r / a), round(g / a), round(b / a), round(255 * a / k)))
            else:
                row.append((0, 0, 0, 0))
        out.append(row)
    return out


def _bmp_entry(px, n: int) -> bytes:
    xor = bytearray()
    for row in reversed(px):
        for (r, g, b, a) in row:
            xor += bytes((b, g, r, a))
    mask_row = ((n + 31) // 32) * 4
    and_mask = bytes(mask_row * n)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + bytes(xor) + and_mask


def ico_bytes(base) -> bytes:
    images = [(_n, _bmp_entry(_render(_n, base), _n)) for _n in SIZES]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for n, data in images:
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return head + entries + blobs


def ensure_icons(folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    normal = folder / f"icon-v{VERSION}.ico"
    paused = folder / f"icon-v{VERSION}-paused.ico"
    for path, color in ((normal, ACCENT), (paused, PAUSED)):
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(ico_bytes(color))
            tmp.replace(path)
    for old in ("icon.ico", "icon-paused.ico"):
        try:
            (folder / old).unlink()
        except OSError:
            pass
    return normal, paused
