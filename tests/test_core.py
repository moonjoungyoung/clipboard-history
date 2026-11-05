"""저장소·암호화·필터·검색 단위 테스트(더미 문자열만 사용)."""
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from clipboard_history import config, crypto, win32
from clipboard_history.filters import SearchIndex, Snapshot, compile_patterns, list_label, menu_label, should_store
from clipboard_history.icons import ensure_icons
from clipboard_history.store import DAY, Store


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class CryptoTest(TempDirCase):
    def test_roundtrip(self):
        data = "더미-비밀-아님 dummy 🎉".encode()
        blob = crypto.protect(data)
        self.assertNotIn(data, blob)
        self.assertEqual(crypto.unprotect(blob), data)

    def test_key_persists(self):
        k1 = crypto.load_or_create_key(self.dir / "key.bin")
        k2 = crypto.load_or_create_key(self.dir / "key.bin")
        self.assertEqual(k1, k2)
        self.assertNotIn(k1, (self.dir / "key.bin").read_bytes())


class StoreTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.dir)

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_add_and_plaintext_not_on_disk(self):
        marker = "DUMMY-MARKER-plaintext-check-12345"
        e, new = self.store.add(marker, "notepad.exe")
        self.assertTrue(new)
        self.store.db.execute("PRAGMA wal_checkpoint(FULL)")
        raw = b"".join(p.read_bytes() for p in self.dir.iterdir() if p.name.startswith("history.db"))
        self.assertNotIn(marker.encode(), raw)
        self.assertNotIn(marker.encode("utf-16-le"), raw)
        self.assertEqual(self.store.get(e.id).text, marker)

    def test_dedupe_bumps(self):
        e1, new1 = self.store.add("same dummy", None, now=1000)
        e2, new2 = self.store.add("same dummy", "a.exe", now=2000)
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(e1.id, e2.id)
        got = self.store.get(e1.id)
        self.assertEqual((got.copy_count, got.last_used_at, got.created_at, got.source), (2, 2000, 1000, "a.exe"))
        self.assertEqual(self.store.stats()["count"], 1)

    def test_reopen_keeps_data(self):
        self.store.add("persist dummy 한글", None)
        self.store.close()
        self.store = Store(self.dir)
        self.assertEqual([e.text for e in self.store.load_all()], ["persist dummy 한글"])

    def test_purge_respects_retention_and_pin(self):
        now = time.time()
        old, _ = self.store.add("old dummy", None, now=now - 91 * DAY)
        pinned, _ = self.store.add("old pinned dummy", None, now=now - 200 * DAY)
        self.store.set_pinned(pinned.id, True)
        recent, _ = self.store.add("recent dummy", None, now=now - 89 * DAY)
        removed = self.store.purge(90, now=now)
        self.assertEqual(removed, [old.id])
        self.assertEqual({e.id for e in self.store.load_all()}, {pinned.id, recent.id})

    def test_reused_old_item_survives(self):
        now = time.time()
        e, _ = self.store.add("reused dummy", None, now=now - 120 * DAY)
        self.store.add("reused dummy", None, now=now - 1 * DAY)
        self.assertEqual(self.store.purge(90, now=now), [])
        self.assertIsNotNone(self.store.get(e.id))

    def test_surrogates_and_large(self):
        odd = "a\ud800b"  # 짝 없는 서로게이트도 깨지지 않아야 함
        e, _ = self.store.add(odd, None)
        self.assertEqual(self.store.get(e.id).text, odd)
        big = "x" * 300_000
        e2, _ = self.store.add(big, None)
        self.assertEqual(self.store.get(e2.id).chars, 300_000)

    def test_undecryptable_row_skipped(self):
        self.store.add("good dummy", None)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO entries(digest, blob, chars, created_at, last_used_at) VALUES(?,?,?,?,?)",
                (b"x" * 32, b"not-a-dpapi-blob", 3, 1, 1))
        self.assertEqual([e.text for e in self.store.load_all()], ["good dummy"])


class FilterTest(unittest.TestCase):
    def test_should_store(self):
        pats = compile_patterns([r"^ghp_[A-Za-z0-9]{10,}$", "(broken"])
        self.assertEqual(len(pats), 1)
        ok = Snapshot("hello", "notepad.exe")
        self.assertIsNone(should_store(ok, ["KeePass.exe"], pats))
        self.assertEqual(should_store(Snapshot("  \n", None), [], pats), "blank")
        self.assertEqual(should_store(Snapshot("x", "keepass.EXE"), ["KeePass.exe"], pats), "excluded_process")
        self.assertEqual(should_store(Snapshot("ghp_abcdefghijklmn", None), [], pats), "excluded_pattern")
        self.assertEqual(should_store(Snapshot(None, None, "excluded_format"), [], pats), "excluded_format")

    def test_search_and_order(self):
        from clipboard_history.store import Entry
        es = [Entry(1, "Apple 사과 dummy", 1, 10, 1, False, None),
              Entry(2, "banana 바나나 dummy", 1, 30, 1, False, None),
              Entry(3, "APPLE pie", 1, 20, 1, True, None)]
        idx = SearchIndex(es)
        self.assertEqual([e.id for e in idx.search("")[0]], [2, 3, 1])
        self.assertEqual([e.id for e in idx.search("apple")[0]], [3, 1])
        self.assertEqual([e.id for e in idx.search("apple 사과")[0]], [1])
        self.assertEqual(idx.search("없는말")[1], 0)
        res, total = idx.search("dummy", limit=1)
        self.assertEqual((len(res), total), (1, 2))
        idx.remove(2)
        self.assertEqual(len(idx), 2)
        self.assertIn("★", list_label(es[2]))
        self.assertEqual(menu_label(Entry(4, "A & B", 1, 1, 1, False, None)), "A && B")


class MiscTest(TempDirCase):
    def test_hotkey_parse(self):
        mods, vk = win32.parse_hotkey("win+alt+v")
        self.assertEqual(vk, ord("V"))
        self.assertTrue(mods & win32.MOD_WIN and mods & win32.MOD_ALT)
        self.assertEqual(win32.parse_hotkey("ctrl+shift+f9")[1], 0x78)
        with self.assertRaises(ValueError):
            win32.parse_hotkey("ctrl+alt")

    def test_config_defaults_and_override(self):
        cfg, _ = config.load_config(self.dir)
        self.assertEqual(cfg.retention_days, 90)
        (self.dir / "config.json").write_text('{"retention_days": 30, "unknown": 1}', encoding="utf-8")
        self.assertEqual(config.load_config(self.dir)[0].retention_days, 30)
        self.assertNotEqual(config.instance_tag(self.dir), config.instance_tag(self.dir / "x"))

    def test_icons(self):
        a, b = ensure_icons(self.dir)
        self.assertEqual(a.read_bytes()[:4], b"\x00\x00\x01\x00")
        self.assertGreater(b.stat().st_size, 4000)


if __name__ == "__main__":
    unittest.main()


class PlacementTest(unittest.TestCase):
    """입력 커서 근처 배치: 아래 → 모자라면 위로 뒤집기, 좌우는 화면 안으로."""
    AREA = (0, 0, 1920, 1040)      # 작업 표시줄을 뺀 작업 영역
    SIZE = (760, 484)

    def place(self, caret, area=AREA, size=SIZE):
        from clipboard_history.theme import place_near_caret
        return place_near_caret(caret, area, size, gap=8, margin=8, inset=58)

    def test_below_caret(self):
        self.assertEqual(self.place((400, 200, 401, 220)), (342, 228))

    def test_flips_above_near_bottom(self):
        x, y = self.place((400, 900, 401, 920))
        self.assertEqual(y, 900 - 8 - 484)          # 커서 위로
        self.assertLessEqual(y + 484, 900)           # 커서를 가리지 않음

    def test_pushed_in_from_right_and_left(self):
        self.assertEqual(self.place((1900, 200, 1901, 220))[0], 1920 - 8 - 760)
        self.assertEqual(self.place((10, 200, 11, 220))[0], 8)

    def test_second_monitor_offsets(self):
        area = (-1920, 0, 0, 1080)                   # 왼쪽 보조 모니터
        x, y = self.place((-100, 1000, -99, 1020), area=area)
        self.assertEqual(x, -8 - 760)
        self.assertEqual(y, 1000 - 8 - 484)

    def test_no_room_either_side_stays_on_screen(self):
        x, y = self.place((400, 300, 401, 320), area=(0, 0, 1280, 600))
        self.assertGreaterEqual(y, 8)
        self.assertLessEqual(y + 484, 600 - 8)
