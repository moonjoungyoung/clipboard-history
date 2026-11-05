"""SQLite 저장소. 본문은 DPAPI 로 암호화한 BLOB 으로만 저장한다."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from .formats import build_hdrop, pack_formats, unpack_formats

SCHEMA_VERSION = 2
DAY = 86400.0


@dataclass
class Entry:
    id: int
    text: str
    created_at: float
    last_used_at: float
    copy_count: int
    pinned: bool
    source: str | None
    kind: str = "text"            # text / image / files
    meta: dict = field(default_factory=dict)  # 내용이 아닌 정보만(이미지 크기, 파일 수, 바이트 수)
    has_payload: bool = False     # 서식·이미지 등 원본 형식 묶음이 따로 저장돼 있는지

    @property
    def chars(self) -> int:
        return len(self.text)


def _enc(text: str) -> bytes:
    return crypto.protect(text.encode("utf-8", "surrogatepass"))


def _dec(blob: bytes) -> str:
    return crypto.unprotect(blob).decode("utf-8", "surrogatepass")


class Store:
    def __init__(self, folder: Path, allow_recover: bool = False):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "history.db"
        self.recovered_from: str | None = None
        try:
            self._key = crypto.load_or_create_key(folder / "key.bin")
        except crypto.KeyInvalid:
            # Windows 비밀번호 강제 재설정 등으로 DPAPI 키가 바뀌어 이전 기록을 영영 풀 수 없는 경우만 여기로 온다.
            # 상주 앱만 복구하고(CLI 는 그냥 실패), 기존 파일은 지우지 않고 옆으로 옮긴 뒤 새로 시작한다.
            if not allow_recover:
                raise
            stamp = time.strftime("%Y%m%d-%H%M%S")
            # DB 를 먼저 옮긴다. DB 가 잠겨 있어 실패하면 key.bin 은 건드리지 않은 채 중단된다.
            for f in [*sorted(folder.glob("history.db*")), folder / "key.bin"]:
                if f.exists():
                    f.rename(f.with_name(f"{f.name}.broken-{stamp}"))
            self.recovered_from = stamp
            self._key = crypto.load_or_create_key(folder / "key.bin")
        self.db = sqlite3.connect(str(self.path), timeout=10)
        if self.db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            # WAL 로 바꾸기 전에 켜야 적용된다. 기존 DB 는 VACUUM 한 번으로 전환.
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            if self.db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
                self.db.execute("PRAGMA journal_mode=DELETE")
                self.db.execute("VACUUM")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        ver = self.db.execute("PRAGMA user_version").fetchone()[0]
        if ver < 1:
            with self.db:
                self.db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS entries(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        digest BLOB NOT NULL UNIQUE,
                        blob BLOB NOT NULL,
                        chars INTEGER NOT NULL,
                        created_at REAL NOT NULL,
                        last_used_at REAL NOT NULL,
                        copy_count INTEGER NOT NULL DEFAULT 1,
                        pinned INTEGER NOT NULL DEFAULT 0,
                        source TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_entries_last_used ON entries(last_used_at);
                    """
                )
                self.db.execute("PRAGMA user_version=1")
            ver = 1
        if ver < 2:
            with self.db:
                cols = {r[1] for r in self.db.execute("PRAGMA table_info(entries)")}
                for name, decl in (("kind", "TEXT NOT NULL DEFAULT 'text'"), ("payload", "BLOB"),
                                   ("thumb", "BLOB"), ("meta", "TEXT")):
                    if name not in cols:
                        self.db.execute(f"ALTER TABLE entries ADD COLUMN {name} {decl}")
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    # ---- 쓰기 ----
    def add(self, text: str, source: str | None = None, now: float | None = None, *,
            kind: str = "text", formats: dict[str, bytes] | None = None, thumb: bytes | None = None,
            meta: dict | None = None, identity: bytes | None = None, bump: bool = True,
            follow_up: bool = False) -> tuple[Entry, bool]:
        """저장하고 (항목, 새 항목 여부) 를 돌려준다. 같은 내용이면 시각만 갱신한다.

        identity: 중복 판별에 쓸 바이트(이미지 원본 등). 없으면 text 로 판별한다.
        bump: False 면 같은 복사의 후속 갱신으로 보고 복사 횟수를 늘리지 않는다.
        follow_up: 같은 앱이 방금 올린 같은 내용의 후속 갱신(엑셀이 닫히며 다시 올림 등)이면, 형식이 더 적은
            묶음으로 덮어쓰지 않는다. 독립된 새 복사는 항상 새 묶음으로 바꾼다.
        """
        now = time.time() if now is None else now
        if identity is not None:
            dig = crypto.digest_bytes(self._key, kind.encode() + b"|" + identity)
        else:
            dig = crypto.digest(self._key, text)
        payload = crypto.protect(pack_formats(formats)) if formats else None
        thumb_blob = crypto.protect(thumb) if thumb else None
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self.db:
            row = self.db.execute(
                "SELECT id, created_at, copy_count, pinned, payload IS NOT NULL FROM entries WHERE digest=?", (dig,)
            ).fetchone()
            if row:
                eid, created, count, pinned, had_payload = row
                if follow_up and payload is not None and had_payload and formats is not None:
                    # 형식이 더 적은 묶음으로 덮어쓰지 않는다(엑셀은 통합 문서를 닫을 때 텍스트·RTF 만 다시 올린다)
                    try:
                        old = self.payload(eid)
                    except (OSError, ValueError):
                        old = {}
                    if old and set(formats) <= set(old) and len(formats) < len(old):
                        payload = None
                inc = 1 if bump else 0
                self.db.execute(
                    "UPDATE entries SET last_used_at=?, copy_count=copy_count+?, kind=?,"
                    " source=COALESCE(?, source), payload=COALESCE(?, payload), thumb=COALESCE(?, thumb)"
                    " WHERE id=?",
                    (now, inc, kind, source, payload, thumb_blob, eid),
                )
                count += inc - 1
                source = source or self.db.execute("SELECT source FROM entries WHERE id=?", (eid,)).fetchone()[0]
                return Entry(eid, text, created, now, count + 1, bool(pinned), source, kind, meta or {},
                             bool(had_payload or payload)), False
            cur = self.db.execute(
                "INSERT INTO entries(digest, blob, chars, created_at, last_used_at, source, kind, payload, thumb, meta)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (dig, _enc(text), len(text), now, now, source, kind, payload, thumb_blob, meta_json),
            )
            return Entry(cur.lastrowid, text, now, now, 1, False, source, kind, meta or {}, payload is not None), True

    def payload(self, entry_id: int) -> dict[str, bytes]:
        row = self.db.execute("SELECT payload FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not row or row[0] is None:
            return {}
        return unpack_formats(crypto.unprotect(row[0]))

    def formats_for(self, e: "Entry") -> dict[str, bytes]:
        """다시 복사할 때 클립보드에 넣을 형식 묶음."""
        if e.kind == "text":
            text_fmt = {"CF_UNICODETEXT": e.text.encode("utf-16-le", "surrogatepass") + b"\x00\x00"}
            payload = self.payload(e.id) if e.has_payload else {}
            if "CF_UNICODETEXT" in payload:
                return payload  # 원래 앱이 넣은 순서 그대로
            return {**text_fmt, **payload}
        if e.kind == "files":
            return {"CF_HDROP": build_hdrop(e.text.split("\n"))}
        return self.payload(e.id)  # 이미지: CF_DIB(+PNG)

    def thumb(self, entry_id: int) -> bytes | None:
        row = self.db.execute("SELECT thumb FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not row or row[0] is None:
            return None
        try:
            return crypto.unprotect(row[0])
        except OSError:
            return None

    def delete(self, entry_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM entries WHERE id=?", (entry_id,)).rowcount > 0

    def set_pinned(self, entry_id: int, pinned: bool) -> None:
        with self.db:
            self.db.execute("UPDATE entries SET pinned=? WHERE id=?", (int(pinned), entry_id))

    def purge(self, retention_days: int, now: float | None = None) -> list[int]:
        """보존 기간이 지난(고정 안 한) 항목을 지우고 지운 id 목록을 돌려준다."""
        now = time.time() if now is None else now
        cutoff = now - retention_days * DAY
        newest = self.db.execute("SELECT MAX(last_used_at) FROM entries").fetchone()[0]
        if newest is not None and newest < cutoff:
            # 가장 최근 항목까지 전부 보존 기간을 넘었다면 시계가 튀었을 가능성이 크다. 지우지 않는다.
            return []
        with self.db:
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM entries WHERE pinned=0 AND last_used_at < ?", (cutoff,))]
            if ids:
                self.db.execute("DELETE FROM entries WHERE pinned=0 AND last_used_at < ?", (cutoff,))
        if ids:
            self.db.execute("PRAGMA incremental_vacuum").fetchall()  # 반환 페이지마다 행이 나오므로 끝까지 읽어야 다 회수된다
        return ids

    # ---- 읽기 ----
    _COLS = ("id, blob, created_at, last_used_at, copy_count, pinned, source, kind, meta,"
             " payload IS NOT NULL")

    def _row(self, row) -> Entry | None:
        eid, blob, created, used, count, pinned, source, kind, meta, has_payload = row
        try:
            text = _dec(blob)
        except OSError:
            return None  # 다른 계정/PC 에서 옮겨 온 DB 등 복호화 불가 항목은 건너뜀
        try:
            meta_d = json.loads(meta) if meta else {}
        except ValueError:
            meta_d = {}
        return Entry(eid, text, created, used, count, bool(pinned), source, kind or "text", meta_d,
                     bool(has_payload))

    def load_all(self) -> list[Entry]:
        rows = self.db.execute(f"SELECT {self._COLS} FROM entries ORDER BY last_used_at DESC").fetchall()
        return [e for e in map(self._row, rows) if e is not None]

    def get(self, entry_id: int) -> Entry | None:
        row = self.db.execute(f"SELECT {self._COLS} FROM entries WHERE id=?", (entry_id,)).fetchone()
        return self._row(row) if row else None

    def stats(self) -> dict:
        count, oldest, newest, chars, pinned = self.db.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(last_used_at), COALESCE(SUM(chars),0),"
            " COALESCE(SUM(pinned),0) FROM entries"
        ).fetchone()
        return {
            "count": count,
            "pinned": pinned,
            "total_chars": chars,
            "oldest_created_at": oldest,
            "newest_used_at": newest,
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }
