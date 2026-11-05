"""저장 여부 판단과 검색(순수 로직, Win32 의존 없음)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .store import Entry


@dataclass
class Snapshot:
    """클립보드에서 읽은 결과. text 가 None 이면 skip_reason 에 이유가 들어 있다.

    kind: text(텍스트, 서식은 formats 에) / image(formats 에 CF_DIB 등, thumb 에 PNG) / files(경로 목록)
    text: 검색·표시용 문자열(이미지는 '이미지 W×H', 파일은 경로를 줄바꿈으로 이은 것)
    """
    text: str | None
    source: str | None = None
    skip_reason: str | None = None
    kind: str = "text"
    formats: dict = field(default_factory=dict)
    thumb: bytes | None = None
    meta: dict = field(default_factory=dict)
    incomplete: bool = False  # 원래 앱이 일부 형식을 아직 만들어 주지 못함(잠시 뒤 다시 읽을 대상)


def should_store(snap: Snapshot, exclude_processes: Iterable[str],
                 exclude_patterns: Iterable[re.Pattern]) -> str | None:
    """저장하면 None, 아니면 건너뛰는 이유."""
    if snap.skip_reason:
        return snap.skip_reason
    if snap.text is None:
        return "no_text"
    if not snap.text.strip():
        return "blank"
    if snap.source:
        excluded = {p.lower() for p in exclude_processes}
        if snap.source.lower() in excluded:
            return "excluded_process"
    if snap.kind == "text":
        for pat in exclude_patterns:
            if pat.search(snap.text) or pat.search(snap.text.strip()):  # 터미널 복사의 끝 공백·줄바꿈 대비
                return "excluded_pattern"
    return None


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return out


class SearchIndex:
    """복호화한 항목을 메모리에 두고 검색한다. 검색어는 공백으로 나눠 모두 포함(AND)."""

    def __init__(self, entries: Iterable[Entry] = ()):
        self._items: dict[int, tuple[Entry, str]] = {}
        for e in entries:
            self.put(e)

    def replace_all(self, entries: Iterable[Entry]) -> None:
        self._items.clear()
        for e in entries:
            self.put(e)

    def put(self, entry: Entry) -> None:
        self._items[entry.id] = (entry, entry.text.casefold())

    def remove(self, entry_id: int) -> None:
        self._items.pop(entry_id, None)

    def get(self, entry_id: int) -> Entry | None:
        item = self._items.get(entry_id)
        return item[0] if item else None

    def __len__(self) -> int:
        return len(self._items)

    def search(self, query: str, limit: int = 500) -> tuple[list[Entry], int]:
        """(최근 사용 순 결과 limit 개, 전체 일치 수)."""
        terms = [t for t in query.casefold().split() if t]
        matched = [e for e, folded in self._items.values() if all(t in folded for t in terms)]
        matched.sort(key=lambda e: (e.last_used_at, e.id), reverse=True)
        return matched[:limit], len(matched)

    def pinned(self) -> list[Entry]:
        items = [e for e, _ in self._items.values() if e.pinned]
        items.sort(key=lambda e: (e.last_used_at, e.id), reverse=True)
        return items

    def recent(self, n: int) -> list[Entry]:
        return self.search("", limit=n)[0]


_WS = re.compile(r"\s+")


def preview(text: str, width: int = 90) -> str:
    one = _WS.sub(" ", text).strip()
    return one if len(one) <= width else one[: width - 1] + "…"


def format_time(ts: float, now: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts)
    ref = datetime.fromtimestamp(now) if now else datetime.now()
    if dt.date() == ref.date():
        return dt.strftime("%H:%M")
    if dt.year == ref.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


def list_label(e: Entry, width: int = 90, now: float | None = None) -> str:
    mark = "★ " if e.pinned else ""
    return f"{format_time(e.last_used_at, now):>11}  {mark}{preview(e.text, width)}"


def menu_label(e: Entry, width: int = 50) -> str:
    # 메뉴에서 & 는 단축키 표시라 두 번 써야 그대로 보인다.
    return preview(e.text, width).replace("&", "&&")
