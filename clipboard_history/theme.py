"""디자인 토큰: '종이(paper)'(라이트)와 '먹지(carbon)'(다크).

색은 클립보드의 물리적 세계에서 왔다. 바탕은 복사 용지, 선택 표시는 잉크 블루, 검색어 표시는 형광펜이다.
강조색은 잉크 하나뿐이다. 형광펜은 '검색어와 일치함'이라는 의미가 있을 때만 쓴다.
"""
from __future__ import annotations

import winreg
from dataclasses import dataclass


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def blend(fg: str, bg: str, alpha: float) -> str:
    """fg 를 alpha 만큼 bg 위에 얹은 색(tk 는 투명도가 없어서 미리 섞는다)."""
    f, b = _hex(fg), _hex(bg)
    return "#%02x%02x%02x" % tuple(round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3))


@dataclass(frozen=True)
class Palette:
    name: str
    paper: str        # 창 바탕
    well: str         # 입력칸(바탕보다 살짝 어둡게: 안으로 들어간 칸)
    sheet: str        # 미리보기 카드(바탕 위에 올린 종이 한 장)
    sheet_edge: str   # 카드 테두리
    frame: str        # 창 테두리(DWM)
    ink: str          # 본문
    ink_2: str        # 보조
    pencil: str       # 메타 정보
    graphite: str     # 플레이스홀더, 비활성
    accent: str       # 잉크 블루: 선택, 커서
    marker: str       # 형광펜: 검색어 일치
    marker_ink: str
    keycap: str
    dark: bool

    @property
    def row_selected(self) -> str:
        return blend(self.accent, self.paper, 0.13 if self.dark else 0.10)

    @property
    def row_hover(self) -> str:
        return blend(self.ink, self.paper, 0.05)

    @property
    def thumb(self) -> str:
        return blend(self.ink, self.paper, 0.22)

    @property
    def text_selection(self) -> str:
        return blend(self.accent, self.sheet, 0.25)


PAPER = Palette(
    name="paper", paper="#F7F6F3", well="#EDEBE6", sheet="#FFFFFF", sheet_edge="#E5E2DB",
    frame="#D6D3CB", ink="#1C1B19", ink_2="#4F4D48", pencil="#86837C", graphite="#ABA8A0",
    accent="#2F5BD3", marker="#FFE27A", marker_ink="#1C1B19", keycap="#E7E5DF", dark=False,
)

CARBON = Palette(
    name="carbon", paper="#1C1D20", well="#141518", sheet="#24252A", sheet_edge="#313338",
    frame="#3A3C42", ink="#EDECE9", ink_2="#B9B7B2", pencil="#86858A", graphite="#5E5E63",
    accent="#7C9CFF", marker="#6B5714", marker_ink="#FFF3C8", keycap="#2C2D32", dark=True,
)


def system_prefers_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def current_palette() -> Palette:
    return CARBON if system_prefers_dark() else PAPER


# 글꼴: 한글과 라틴 문자가 고르게 나오는 Noto Sans KR 을 먼저 쓰고, 없으면 맑은 고딕을 쓴다.
FONT_CHAIN = ("Noto Sans KR", "맑은 고딕", "Malgun Gothic", "Segoe UI")
FONT_MEDIUM_CHAIN = ("Noto Sans KR Medium", "Noto Sans KR", "맑은 고딕", "Segoe UI Semibold")


def pick_font(available: set[str], chain: tuple[str, ...]) -> str:
    for name in chain:
        if name in available:
            return name
    return "TkDefaultFont"


# 크기(논리 px, 4px 기준 격자). 실제 픽셀은 DPI 배율을 곱한다.
class Size:
    W, H = 760, 484
    PAD = 12
    SEARCH_H = 52
    GAP = 12
    LIST_W = 400
    FOOTER_H = 40
    ROW_H = 54
    HEADER_H = 30
    R_FIELD = 12      # 입력칸, 카드
    R_ROW = 10        # 행 선택 표시 (= 카드 12 - 안쪽 여백 2)
    R_KEY = 5
    # 글자(px): 1.2 비율 단계  11 · 12 · 14 · 16(검색어)
    T_CAPTION = 11
    T_META = 12
    T_BODY = 14
    T_QUERY = 16
    T_PREVIEW = 13
