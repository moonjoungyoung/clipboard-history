"""검색 창(tkinter). 단축키로 불러 찾고 Enter 한 번에 사라지는 런처형 창. 메인 스레드에서만 다룬다.

구성(논리 px, 4px 격자):
  ┌──────────────────────────────────────────────┐
  │ [⌕  검색어 입력 ......................  24개] │  검색칸 52px(바탕보다 살짝 어두운 '안으로 들어간' 칸)
  │ 오늘                       ┌───────────────┐ │
  │ ▌첫 줄 미리보기(형광펜)     │ 미리보기 카드  │ │  목록 400px | 카드(종이 한 장)
  │   14:22 · 1,234자 · chrome │               │ │
  │ 어제 ...                   └───────────────┘ │
  │ 1,234개 보관 · 90일   ↵ 붙여넣기 ⇧↵ 복사만 … │  바닥 40px
  └──────────────────────────────────────────────┘
"""
from __future__ import annotations

import base64
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from typing import Callable

from . import theme, win32
from .filters import SearchIndex, preview
from .formats import human_bytes
from .paint import ShapeCache
from .store import Entry
from .theme import Palette, Size

TITLE = "클립보드 기록"
RESULT_LIMIT = 500
PREVIEW_LIMIT = 20_000  # 줄바꿈 없는 긴 글은 Tk Text 줄바꿈 계산이 느려서 미리보기는 앞부분만
MARK_LIMIT = 200
KEYS = (("↵", "붙여넣기"), ("⇧ ↵", "복사만"), ("Ctrl P", "고정"), ("Ctrl D", "삭제"), ("Esc", "닫기"))
ICON_SEARCH, ICON_PIN, ICON_FOLDER, ICON_PAGE, ICON_PHOTO = "", "", "", "", ""


def _fmt_full(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def bucket(ts: float, now: datetime) -> str:
    d = datetime.fromtimestamp(ts).date()
    today = now.date()
    if d == today:
        return "오늘"
    if d == today - timedelta(days=1):
        return "어제"
    if d > today - timedelta(days=7):
        return "지난 7일"
    if d > today - timedelta(days=30):
        return "지난 30일"
    return "그 이전"


def short_time(ts: float, now: datetime) -> str:
    dt = datetime.fromtimestamp(ts)
    if dt.date() == now.date() or dt.date() == now.date() - timedelta(days=1):
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return f"{dt.month}월 {dt.day}일"
    return dt.strftime("%Y-%m-%d")


def app_name(source: str | None) -> str:
    if not source:
        return ""
    return source[:-4] if source.lower().endswith(".exe") else source


def entry_title(e: Entry) -> str:
    if e.kind == "files":
        paths = e.text.split("\n")
        name = paths[0].rstrip("\\/").rsplit("\\", 1)[-1]
        return name + (f"  외 {len(paths) - 1}개" if len(paths) > 1 else "")
    if e.kind == "image":
        w, h = e.meta.get("w"), e.meta.get("h")
        return f"이미지  {w} × {h}" if w else "이미지"
    return preview(e.text[:2000], 400)


def entry_meta(e: Entry, now: datetime) -> str:
    parts = [short_time(e.last_used_at, now)]
    if e.kind == "text":
        parts.append(f"{e.chars:,}자")
        lines = e.text.count("\n") + 1
        if lines > 1:
            parts.append(f"{lines:,}줄")
    elif e.kind == "image":
        if e.meta.get("bytes"):
            parts.append(human_bytes(int(e.meta["bytes"])))
    elif e.kind == "files":
        first = e.text.split("\n", 1)[0]
        parent = first.rstrip("\\/").rsplit("\\", 1)[0]
        parts.append(parent)
    if e.copy_count > 1:
        parts.append(f"{e.copy_count}회")
    name = app_name(e.source)
    if name:
        parts.append(name)
    return " · ".join(parts)


class SearchWindow:
    def __init__(self, root: tk.Tk, index: SearchIndex,
                 on_copy: Callable[[Entry, bool, object], None],
                 on_delete: Callable[[Entry], None],
                 on_pin: Callable[[Entry], None],
                 load_thumb: Callable[[int], bytes | None] | None = None,
                 icon_path: str | None = None, retention_days: int = 90, position_mode: str = "caret"):
        self.root = root
        self.index = index
        self.on_copy = on_copy
        self.on_delete = on_delete
        self.on_pin = on_pin
        self.load_thumb = load_thumb or (lambda _id: None)
        self.retention_days = retention_days
        self.position_mode = position_mode
        self.results: list[Entry] = []
        self.rows: list[tuple] = []        # ("header", 라벨, y) / ("entry", 결과 인덱스, y)
        self.sel = 0
        self.hover = -1
        self.scroll = 0
        self.total = 0
        self.prev_hwnd = None
        self._pending = None
        self._flash_job = None
        self._menu_open = False
        self._thumbs: dict[tuple[int, str], tk.PhotoImage] = {}
        self._dirty = True
        self._styled = None
        self.pal: Palette | None = None

        self.s = max(1.0, root.winfo_fpixels("1i") / 96)
        fams = set(tkfont.families(root))
        self.family = theme.pick_font(fams, theme.FONT_CHAIN)
        self.family_med = theme.pick_font(fams, theme.FONT_MEDIUM_CHAIN)
        self.icon_family = theme.pick_font(fams, ("Segoe Fluent Icons", "Segoe MDL2 Assets"))
        f = lambda fam, px: tkfont.Font(root, family=fam, size=-self.px(px))  # noqa: E731
        self.f_query = f(self.family, Size.T_QUERY)
        self.f_body = f(self.family, Size.T_BODY)
        self.f_meta = f(self.family, Size.T_META)
        self.f_caption = f(self.family_med, Size.T_CAPTION)
        self.f_key = f(self.family_med, Size.T_CAPTION)
        self.f_preview = f(self.family, Size.T_PREVIEW)
        self.f_icon = f(self.icon_family, 14)
        self.f_icon_lg = f(self.icon_family, 22)

        root.title(TITLE)
        if icon_path:
            try:
                root.iconbitmap(icon_path)
            except tk.TclError:
                pass
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.shapes = ShapeCache(root)
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self._schedule_refresh())
        self._build(theme.current_palette())
        root.bind("<FocusOut>", lambda e: root.after(150, self._maybe_hide_on_blur))

    # ------------------------------------------------------------------ 배치
    def px(self, v: float) -> int:
        return int(round(v * self.s))

    def _build(self, pal: Palette) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        self.pal = pal
        self.shapes.clear()
        self._thumbs.clear()
        p = self.px
        W, H, PAD, G = p(Size.W), p(Size.H), p(Size.PAD), p(Size.GAP)
        self.W, self.H = W, H
        self.root.configure(bg=pal.paper)

        c = self.chrome = tk.Canvas(self.root, width=W, height=H, bg=pal.paper, highlightthickness=0, bd=0)
        c.pack(fill="both", expand=True)

        # 검색칸
        sh = p(Size.SEARCH_H)
        c.create_image(PAD, PAD, anchor="nw", image=self.shapes.rounded(W - 2 * PAD, sh, p(Size.R_FIELD), pal.well, pal.paper))
        c.create_text(PAD + p(18), PAD + sh // 2, text=ICON_SEARCH, font=self.f_icon, fill=pal.pencil, anchor="w")
        self.count_id = c.create_text(W - PAD - p(16), PAD + sh // 2, text="", font=self.f_meta, fill=pal.pencil, anchor="e")
        self.entry = tk.Entry(c, textvariable=self.query, font=self.f_query, bd=0, relief="flat",
                              bg=pal.well, fg=pal.ink, insertbackground=pal.accent, insertwidth=p(2),
                              selectbackground=pal.text_selection, selectforeground=pal.ink,
                              highlightthickness=0)
        ex, ew = PAD + p(46), W - 2 * PAD - p(46) - p(96)
        c.create_window(ex, PAD + sh // 2, window=self.entry, anchor="w", width=ew, height=p(28))
        self.placeholder = tk.Label(c, text="복사했던 내용을 검색하세요", font=self.f_query, bg=pal.well, fg=pal.graphite, bd=0)
        self.placeholder_id = c.create_window(ex + p(5), PAD + sh // 2, window=self.placeholder, anchor="w")
        self._homes = {self.placeholder_id: (ex + p(5), PAD + sh // 2)}
        self.placeholder.bind("<Button-1>", lambda e: self.entry.focus_set())

        # 본문: 목록 | 카드
        top = PAD + sh + G
        foot = p(Size.FOOTER_H)
        body_h = H - top - foot
        self.list_w, self.list_h = p(Size.LIST_W), body_h
        self.list = tk.Canvas(c, width=self.list_w, height=body_h, bg=pal.paper, highlightthickness=0, bd=0)
        c.create_window(PAD, top, window=self.list, anchor="nw")

        cx = PAD + self.list_w + G
        cw = W - cx - PAD
        self.card = (cx, top, cw, body_h)
        c.create_image(cx, top, anchor="nw",
                       image=self.shapes.rounded(cw, body_h, p(Size.R_FIELD), pal.sheet, pal.paper, pal.sheet_edge))
        inset = p(18)
        self.card_title = c.create_text(cx + inset, top + p(24), text="", font=self.f_caption, fill=pal.ink_2, anchor="w")
        self.card_meta = c.create_text(cx + inset, top + p(44), text="", font=self.f_meta, fill=pal.pencil, anchor="w")
        self.card_pin = c.create_text(cx + cw - inset, top + p(24), text="", font=self.f_icon, fill=pal.accent, anchor="e")
        content_y = top + p(64)
        self.content_box = (cx + inset - p(2), content_y, cw - 2 * inset + p(4), body_h - (content_y - top) - p(14))
        bx, by, bw, bh = self.content_box
        self.text = tk.Text(c, font=self.f_preview, wrap="word", bd=0, relief="flat", bg=pal.sheet, fg=pal.ink,
                            highlightthickness=0, padx=0, pady=0, spacing1=0, spacing2=p(4), spacing3=p(2),
                            selectbackground=pal.text_selection, selectforeground=pal.ink,
                            insertwidth=0, cursor="arrow")
        self.text.tag_configure("mark", background=pal.marker, foreground=pal.marker_ink)
        self.text_id = c.create_window(bx, by, window=self.text, anchor="nw", width=bw, height=bh)
        self.image_label = tk.Label(c, bg=pal.sheet, bd=0)
        self.image_id = c.create_window(bx + bw // 2, by + bh // 2, window=self.image_label, anchor="center")
        self._homes[self.text_id] = (bx, by)
        self._homes[self.image_id] = (bx + bw // 2, by + bh // 2)
        self._toggle(self.image_id, False)
        self.card_empty = c.create_text(cx + cw // 2, top + body_h // 2, text="", font=self.f_meta, fill=pal.graphite,
                                        justify="center")

        # 바닥: 상태 | 키캡
        fy = H - foot // 2 - p(2)
        self.status_id = c.create_text(PAD + p(4), fy, text="", font=self.f_meta, fill=pal.pencil, anchor="w")
        x = W - PAD - p(2)
        for key, label in reversed(KEYS):
            lw = self.f_meta.measure(label)
            c.create_text(x, fy, text=label, font=self.f_meta, fill=pal.pencil, anchor="e")
            x -= lw + p(6)
            kw = self.f_key.measure(key) + p(12)
            c.create_image(x - kw, fy - p(10), anchor="nw",
                           image=self.shapes.rounded(kw, p(20), p(Size.R_KEY), pal.keycap, pal.paper))
            c.create_text(x - kw // 2, fy, text=key, font=self.f_key, fill=pal.ink_2)
            x -= kw + p(16)

        self.menu = tk.Menu(self.root, tearoff=False, bg=pal.sheet, fg=pal.ink, activebackground=pal.row_selected,
                            activeforeground=pal.ink, bd=0, relief="flat", font=self.f_meta)
        self.menu.add_command(label="붙여넣기", command=lambda: self._copy(True))
        self.menu.add_command(label="복사만", command=lambda: self._copy(False))
        self.menu.add_command(label="고정 / 고정 해제", command=self._pin)
        self.menu.add_separator()
        self.menu.add_command(label="삭제", command=self._delete)

        self._bind_keys()
        self.list.bind("<MouseWheel>", self._on_wheel)
        self.list.bind("<Motion>", self._on_motion)
        self.list.bind("<Leave>", lambda e: self._set_hover(-1))
        self.list.bind("<Button-1>", self._on_click)
        self.list.bind("<Double-Button-1>", lambda e: self._copy(True))
        self.list.bind("<Button-3>", self._on_context)
        self.root.geometry(f"{W}x{H}")

    def _toggle(self, item: int, visible: bool) -> None:
        """캔버스의 창 항목(Entry/Label/Text)은 state=hidden 이 안 먹어서 좌표를 화면 밖으로 옮겨 숨긴다."""
        home = self._homes[item]
        self.chrome.coords(item, *(home if visible else (-10000, -10000)))

    def _bind_keys(self) -> None:
        for w in (self.entry, self.text, self.list, self.root):
            w.bind("<Down>", lambda e: self._move(1))
            w.bind("<Up>", lambda e: self._move(-1))
            w.bind("<Next>", lambda e: self._move(6))
            w.bind("<Prior>", lambda e: self._move(-6))
            w.bind("<Return>", lambda e: self._copy(True))       # 바로 붙여넣기
            w.bind("<KP_Enter>", lambda e: self._copy(True))
            w.bind("<Shift-Return>", lambda e: self._copy(False))  # 클립보드에 복사만
            w.bind("<Escape>", lambda e: self._escape())
            for k in ("p", "P"):
                w.bind(f"<Control-{k}>", lambda e: self._pin())
            for k in ("d", "D"):
                w.bind(f"<Control-{k}>", lambda e: self._delete())

    # ------------------------------------------------------------------ 표시
    def visible(self) -> bool:
        return self.root.state() != "withdrawn"

    def _own_hwnd(self):
        return win32.GetAncestor(self.root.winfo_id(), win32.GA_ROOT)

    def show(self, prev_hwnd=None, caret=None, keep_target: bool = False) -> None:
        was_visible = self.visible()
        own = self._own_hwnd()
        if keep_target and was_visible:
            pass                          # 창이 떠 있을 때 단축키를 다시 누름: 원래 붙여넣을 창 유지
        elif prev_hwnd and prev_hwnd != own:
            self.prev_hwnd = prev_hwnd
        else:
            self.prev_hwnd = None         # 트레이·CLI 로 열면 붙여넣을 대상 없음(복사만)
        pal = theme.current_palette()
        if pal is not self.pal:
            q = self.query.get()
            self._build(pal)
            self.query.set(q)
        if not was_visible:
            if self.query.get():
                self.query.set("")          # 새로 열 때는 빈 검색부터
                self._dirty = True
            if self._pending:
                self.root.after_cancel(self._pending)
                self._pending = None
            if self._dirty or self.sel != 0 or self.scroll:
                self.scroll = 0
                self.refresh()              # 기록이 바뀌었거나 위치가 달라졌을 때만 다시 계산
                self._dirty = False
        if not was_visible or caret:
            x, y = self._position(caret)
            self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self.root.update_idletasks()
        own = self._own_hwnd()
        if own:
            if self._styled != own:  # 창 모양(둥근 모서리 등)은 창마다 한 번만
                win32.style_popup(own, self.pal.frame, self.pal.dark)
                self._styled = own
            win32.force_foreground(own)
        self.root.focus_force()
        self.entry.focus_set()
        self.entry.icursor("end")
        self.root.after(1500, self._check_activated)

    def _check_activated(self) -> None:
        """포그라운드 전환이 막혀 한 번도 활성화되지 못했으면 '항상 위'를 풀어 다른 창을 가리지 않게 한다."""
        if not self.visible():
            return
        if win32.GetForegroundWindow() == self._own_hwnd():
            self.root.attributes("-topmost", True)
        else:
            self.root.attributes("-topmost", False)

    def _position(self, caret) -> tuple[int, int]:
        """입력 커서 근처(아래, 모자라면 위). 커서를 모르면 마우스가 있는 화면의 위쪽 가운데."""
        if caret and self.position_mode == "caret":
            area = win32.work_area_at((caret[0] + caret[2]) // 2, caret[3] - 1)
            if area:
                return theme.place_near_caret(caret, area, (self.W, self.H), gap=self.px(8),
                                              margin=self.px(8), inset=self.px(Size.PAD + 46))
        area = win32.work_area_at_cursor()
        if area:
            l, t, r, b = area
        else:
            l, t, r, b = 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return l + (r - l - self.W) // 2, t + max(self.px(24), int((b - t) * 0.18))

    def hide(self) -> None:
        self.root.withdraw()

    def _escape(self) -> str:
        if self.query.get():
            self.query.set("")
        else:
            self.hide()
        return "break"

    def _maybe_hide_on_blur(self) -> None:
        if self._menu_open or not self.visible():
            return
        if self.root.focus_get() is None:
            self.hide()

    def flash(self, message: str) -> None:
        self.chrome.itemconfigure(self.status_id, text=message, fill=self.pal.accent)
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(2500, self._update_status)

    def _update_status(self) -> None:
        self._flash_job = None
        self.chrome.itemconfigure(self.status_id, fill=self.pal.pencil,
                                  text=f"{len(self.index):,}개 보관 중 · 마지막 사용 후 {self.retention_days}일 보관")

    # ------------------------------------------------------------------ 데이터
    def _schedule_refresh(self) -> None:
        has = bool(self.query.get())
        self._toggle(self.placeholder_id, not has)
        if self._pending:
            self.root.after_cancel(self._pending)
        self._pending = self.root.after(90, self.refresh)

    def _flush(self) -> None:
        """입력 직후 Enter 를 눌러도 방금 친 검색어 기준 결과로 동작하게 미뤄 둔 갱신을 바로 실행한다."""
        if self._pending:
            self.root.after_cancel(self._pending)
            self.refresh(keep_selection=False)

    def data_changed(self) -> None:
        if self.visible():
            self.refresh(keep_selection=True)
        else:
            self._dirty = True  # 숨어 있을 때는 다음에 열 때 한 번만 다시 계산

    def prewarm(self) -> None:
        """화면 밖에서 한 번 띄웠다 숨긴다(처음 열 때의 창 생성·그리기 비용을 미리 치름)."""
        self.refresh()
        self._dirty = False
        self.root.geometry(f"{self.W}x{self.H}+-32000+-32000")
        self.root.deiconify()
        self.root.update()
        own = self._own_hwnd()
        if own:
            win32.style_popup(own, self.pal.frame, self.pal.dark)
            self._styled = own
        self.root.withdraw()

    def refresh(self, keep_selection: bool = False) -> None:
        if self._pending:
            # 직접 호출된 경우, 예약돼 있던 갱신이 나중에 또 돌아 선택을 되돌리지 않게 취소한다.
            try:
                self.root.after_cancel(self._pending)
            except tk.TclError:
                pass
        self._pending = None
        keep = self.selected().id if keep_selection and self.selected() else None
        q = self.query.get()
        results, self.total = self.index.search(q, RESULT_LIMIT)
        if not q.strip():
            # 고정 항목은 최근 500개 밖에 있어도 항상 맨 위에 보인다
            pinned = self.index.pinned()
            ids = {e.id for e in pinned}
            results = pinned + [e for e in results if e.id not in ids]
        self.results = results
        self.sel = 0
        self.hover = -1
        if keep is not None:
            self.sel = next((i for i, e in enumerate(results) if e.id == keep), 0)
        self._layout_rows(pinned_first=not q.strip())
        self._ensure_visible()
        self._render_list()
        self._render_card()
        label = f"{self.total:,}개" if q.strip() else ""
        if self.total > RESULT_LIMIT:
            label = f"최근 {RESULT_LIMIT}개 / {self.total:,}개"
        self.chrome.itemconfigure(self.count_id, text=label)
        self._toggle(self.placeholder_id, not q)
        if not self._flash_job:
            self._update_status()

    def selected(self) -> Entry | None:
        return self.results[self.sel] if 0 <= self.sel < len(self.results) else None

    def _layout_rows(self, pinned_first: bool) -> None:
        now = datetime.now()
        rows, y, last = [], 0, None
        hh, rh = self.px(Size.HEADER_H), self.px(Size.ROW_H)
        for i, e in enumerate(self.results):
            group = "고정" if pinned_first and e.pinned else bucket(e.last_used_at, now)
            if group != last:
                rows.append(("header", group, y))
                y += hh
                last = group
            rows.append(("entry", i, y))
            y += rh
        self.rows = rows
        self.content_h = y

    # ------------------------------------------------------------------ 목록 그리기
    def _row_of(self, idx: int):
        for r in self.rows:
            if r[0] == "entry" and r[1] == idx:
                return r
        return None

    def _ensure_visible(self) -> None:
        r = self._row_of(self.sel)
        if not r:
            self.scroll = 0
            return
        rh = self.px(Size.ROW_H)
        top = r[2]
        # 그룹의 첫 항목이면 머리글도 같이 보이게
        prev = self.rows[self.rows.index(r) - 1] if self.rows.index(r) > 0 else None
        if prev and prev[0] == "header":
            top = prev[2]
        if top < self.scroll:
            self.scroll = top
        elif r[2] + rh > self.scroll + self.list_h:
            self.scroll = r[2] + rh - self.list_h
        self._clamp_scroll()

    def _clamp_scroll(self) -> None:
        self.scroll = max(0, min(self.scroll, max(0, self.content_h - self.list_h)))

    def _fit(self, text: str, font: tkfont.Font, width: int) -> str:
        if font.measure(text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

    def _terms(self) -> list[str]:
        return [t for t in self.query.get().casefold().split() if t]

    def _render_list(self) -> None:
        c, pal, p = self.list, self.pal, self.px
        c.delete("all")
        if not self.results:
            q = self.query.get().strip()
            msg = (f"‘{q}’ 와 일치하는 기록이 없습니다" if q
                   else "아직 기록이 없습니다\n텍스트나 이미지를 복사하면 여기에 쌓입니다")
            c.create_text(self.list_w // 2, self.list_h // 2 - p(10), text=msg, font=self.f_meta,
                          fill=pal.pencil, justify="center", width=self.list_w - p(40))
            return
        now = datetime.now()
        rh, row_w = p(Size.ROW_H), self.list_w - p(10)
        terms = self._terms()
        for kind, val, y0 in self.rows:
            y = y0 - self.scroll
            if y > self.list_h or y + rh < 0:
                continue
            if kind == "header":
                c.create_text(p(14), y + p(Size.HEADER_H) - p(10), text=val, font=self.f_caption,
                              fill=pal.pencil, anchor="w")
                continue
            i, e = val, self.results[val]
            if i == self.sel:
                c.create_image(0, y + p(2), anchor="nw",
                               image=self.shapes.rounded(row_w, rh - p(4), p(Size.R_ROW), pal.row_selected, pal.paper))
                c.create_rectangle(p(5), y + p(16), p(8), y + rh - p(16), fill=pal.accent, outline="")
            elif i == self.hover:
                c.create_image(0, y + p(2), anchor="nw",
                               image=self.shapes.rounded(row_w, rh - p(4), p(Size.R_ROW), pal.row_hover, pal.paper))
            x = p(16)
            if e.kind == "image":
                img = self._thumb(e, "list")
                if img is not None:
                    c.create_image(x + p(20), y + rh // 2, image=img)
                else:
                    c.create_text(x + p(20), y + rh // 2, text=ICON_PHOTO, font=self.f_icon_lg, fill=pal.graphite)
                x += p(52)
            elif e.kind == "files":
                c.create_text(x + p(14), y + rh // 2, text=ICON_FOLDER, font=self.f_icon_lg, fill=pal.pencil)
                x += p(40)
            right = row_w - p(14)
            if e.pinned:
                c.create_text(right, y + p(20), text=ICON_PIN, font=self.f_icon, fill=pal.accent, anchor="e")
                right -= p(22)
            title = self._fit(entry_title(e), self.f_body, right - x)
            ty = y + p(20)
            if terms and e.kind != "image":
                self._draw_marks(c, title, x, ty, self.f_body, terms)
            c.create_text(x, ty, text=title, font=self.f_body, fill=pal.ink, anchor="w")
            meta = self._fit(entry_meta(e, now), self.f_meta, row_w - p(14) - x)
            c.create_text(x, y + p(38), text=meta, font=self.f_meta, fill=pal.pencil, anchor="w")
        # 얇은 스크롤 막대(내용이 넘칠 때만)
        if self.content_h > self.list_h:
            track = self.list_h - p(8)
            th = max(p(28), int(track * self.list_h / self.content_h))
            ty = p(4) + int((track - th) * self.scroll / max(1, self.content_h - self.list_h))
            x0 = self.list_w - p(4)
            c.create_rectangle(x0, ty, x0 + p(3), ty + th, fill=pal.thumb, outline="")

    def _draw_marks(self, c: tk.Canvas, text: str, x: int, y: int, font: tkfont.Font, terms: list[str]) -> None:
        low = text.casefold() if len(text.casefold()) == len(text) else text.lower()
        half = font.metrics("linespace") // 2
        for t in terms:
            start = 0
            while True:
                i = low.find(t, start)
                if i < 0:
                    break
                x0 = x + font.measure(text[:i])
                x1 = x + font.measure(text[:i + len(t)])
                c.create_rectangle(x0 - 1, y - half + self.px(2), x1 + 1, y + half - self.px(1),
                                   fill=self.pal.marker, outline="")
                start = i + len(t)

    def _thumb(self, e: Entry, size: str) -> tk.PhotoImage | None:
        key = (e.id, size)
        if key in self._thumbs:
            return self._thumbs[key]
        img = None
        try:
            if size == "list":
                full = self._thumb(e, "card")
                if full is not None:
                    k = max(1, -(-max(full.width(), full.height()) // self.px(40)))
                    img = full.subsample(k)
            else:
                data = self.load_thumb(e.id)
                if data:
                    full = tk.PhotoImage(master=self.root, data=base64.b64encode(data).decode("ascii"))
                    bw, bh = self.content_box[2], self.content_box[3]
                    k = max(1, -(-full.width() // bw), -(-full.height() // bh))
                    img = full.subsample(k) if k > 1 else full
        except tk.TclError:
            img = None
        if len(self._thumbs) > 300:
            self._thumbs.clear()
        self._thumbs[key] = img
        return img

    # ------------------------------------------------------------------ 카드
    def _render_card(self) -> None:
        c, pal = self.chrome, self.pal
        e = self.selected()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self._toggle(self.image_id, False)
        self._toggle(self.text_id, True)
        if not e:
            c.itemconfigure(self.card_title, text="")
            c.itemconfigure(self.card_meta, text="")
            c.itemconfigure(self.card_pin, text="")
            self._toggle(self.text_id, False)
            c.itemconfigure(self.card_empty, text="선택한 항목이 여기에 보입니다" if self.results else "")
            self.text.configure(state="disabled")
            return
        c.itemconfigure(self.card_empty, text="")
        kind = {"text": "텍스트", "image": "이미지", "files": "파일"}.get(e.kind, e.kind)
        c.itemconfigure(self.card_title, text=f"{kind} · {_fmt_full(e.last_used_at)}")
        meta = []
        if e.kind == "text":
            meta.append(f"{e.chars:,}자 · {e.text.count(chr(10)) + 1:,}줄")
        elif e.kind == "image" and e.meta.get("w"):
            meta.append(f"{e.meta['w']} × {e.meta['h']} · {human_bytes(int(e.meta.get('bytes', 0)))}")
        elif e.kind == "files":
            meta.append(f"{len(e.text.splitlines())}개 항목")
        meta.append(f"처음 {_fmt_full(e.created_at)}")
        if app_name(e.source):
            meta.append(app_name(e.source))
        bw = self.card[2] - self.px(36)
        c.itemconfigure(self.card_meta, text=self._fit(" · ".join(meta), self.f_meta, bw))
        c.itemconfigure(self.card_pin, text=ICON_PIN if e.pinned else "")
        if e.kind == "image":
            img = self._thumb(e, "card")
            self._toggle(self.text_id, False)
            if img is not None:
                self.image_label.configure(image=img)
                self._toggle(self.image_id, True)
            else:
                c.itemconfigure(self.card_empty, text="미리보기를 만들 수 없는 이미지입니다\n(Enter 로 원본을 다시 복사할 수 있습니다)")
        else:
            body = e.text if len(e.text) <= PREVIEW_LIMIT else (
                e.text[:PREVIEW_LIMIT] + f"\n\n… 미리보기는 앞 {PREVIEW_LIMIT:,}자까지")
            self.text.insert("1.0", body)
            self.text.yview_moveto(0)
            self._mark_text(body)  # 첫 일치 위치로 스크롤(맨 위로 되돌리지 않음)
        self.text.configure(state="disabled")

    def _mark_text(self, body: str) -> None:
        terms = self._terms()
        if not terms:
            return
        low = body.casefold() if len(body.casefold()) == len(body) else body.lower()
        count, first = 0, None
        for t in terms:
            start = 0
            while count < MARK_LIMIT:
                i = low.find(t, start)
                if i < 0:
                    break
                self.text.tag_add("mark", f"1.0+{i}c", f"1.0+{i + len(t)}c")
                first = i if first is None else min(first, i)
                count += 1
                start = i + len(t)
        if first is not None:
            self.text.see(f"1.0+{first}c")

    # ------------------------------------------------------------------ 입력
    def _set_hover(self, idx: int) -> None:
        if idx != self.hover:
            self.hover = idx
            self._render_list()

    def _hit(self, y: int) -> int:
        y += self.scroll
        rh = self.px(Size.ROW_H)
        for kind, val, y0 in self.rows:
            if kind == "entry" and y0 <= y < y0 + rh:
                return val
        return -1

    def _on_motion(self, event) -> None:
        self._set_hover(self._hit(event.y))

    def _on_click(self, event) -> None:
        i = self._hit(event.y)
        if i >= 0:
            self._select(i)
        self.entry.focus_set()

    def _on_context(self, event) -> None:
        i = self._hit(event.y)
        if i < 0:
            return
        self._select(i)
        self._menu_open = True
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
            self._menu_open = False

    def _on_wheel(self, event) -> None:
        self.scroll -= int(event.delta / 120 * self.px(Size.ROW_H) * 1.5)
        self._clamp_scroll()
        self._render_list()

    def _select(self, idx: int) -> None:
        if not self.results:
            return
        self.sel = max(0, min(idx, len(self.results) - 1))
        self._ensure_visible()
        self._render_list()
        self._render_card()

    def _move(self, delta: int) -> str:
        self._flush()
        self._select(self.sel + delta)
        return "break"

    def _copy(self, paste: bool) -> str:
        self._flush()
        e = self.selected()
        if e:
            self.on_copy(e, paste, self.prev_hwnd)
        return "break"

    def _pin(self) -> str:
        self._flush()
        e = self.selected()
        if e:
            self.on_pin(e)
            if not e.pinned:
                age = (datetime.now().timestamp() - e.last_used_at) / 86400
                if age > self.retention_days:
                    self.flash("고정을 풀었습니다. 보존 기간이 지나 다음 정리 때 삭제됩니다.")
            self.refresh(keep_selection=True)
        return "break"

    def _delete(self) -> str:
        self._flush()
        e = self.selected()
        if e:
            idx = self.sel
            self.on_delete(e)
            self.refresh()
            self._select(min(idx, len(self.results) - 1))
            self.flash("삭제했습니다")
        return "break"

