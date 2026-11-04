"""안티에일리어싱된 둥근 사각형을 그린다(tk Canvas 도형은 계단 현상이 생겨서 이미지로 만든다).

signed distance 로 픽셀마다 덮임 정도를 계산하고, 알고 있는 바탕색과 미리 섞는다.
"""
from __future__ import annotations

import math
import tkinter as tk

from .theme import _hex


def _sdf(px: float, py: float, w: float, h: float, r: float) -> float:
    """(px,py) 에서 w×h, 반지름 r 인 둥근 사각형 경계까지의 부호 있는 거리(안쪽이 음수)."""
    qx = abs(px - w / 2) - (w / 2 - r)
    qy = abs(py - h / 2) - (h / 2 - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def _mix(a, b, t):
    return (a[0] * t + b[0] * (1 - t), a[1] * t + b[1] * (1 - t), a[2] * t + b[2] * (1 - t))


def rounded_rgb(w: int, h: int, r: float, fill: str, bg: str, border: str | None = None,
                border_w: float = 1.0) -> list[list[tuple[float, float, float]]]:
    f, b = _hex(fill), _hex(bg)
    e = _hex(border) if border else None
    rows = []
    edge = r + border_w + 2
    for y in range(h):
        row = []
        near_y = y < edge or y >= h - edge
        for x in range(w):
            if not near_y and edge <= x < w - edge:
                row.append(f)  # 가장자리에서 먼 안쪽은 계산할 필요 없이 채움색
                continue
            d = _sdf(x + 0.5, y + 0.5, w, h, r)
            cover = min(max(0.5 - d, 0.0), 1.0)
            if e is not None:
                inner = min(max(0.5 - (d + border_w), 0.0), 1.0)
                c = _mix(f, e, inner)
            else:
                c = f
            row.append(_mix(c, b, cover))
        rows.append(row)
    return rows


def to_photo(rows, master=None) -> tk.PhotoImage:
    h, w = len(rows), len(rows[0]) if rows else 0
    img = tk.PhotoImage(master=master, width=w, height=h)
    data = " ".join(
        "{" + " ".join("#%02x%02x%02x" % (round(c[0]), round(c[1]), round(c[2])) for c in row) + "}"
        for row in rows
    )
    img.put(data, to=(0, 0))
    return img


class ShapeCache:
    """같은 크기·색의 도형 이미지는 한 번만 만든다."""

    def __init__(self, master):
        self.master = master
        self._cache: dict[tuple, tk.PhotoImage] = {}

    def rounded(self, w: int, h: int, r: float, fill: str, bg: str, border: str | None = None) -> tk.PhotoImage:
        key = (w, h, r, fill, bg, border)
        img = self._cache.get(key)
        if img is None:
            img = to_photo(rounded_rgb(w, h, r, fill, bg, border), self.master)
            self._cache[key] = img
        return img

    def clear(self) -> None:
        self._cache.clear()
