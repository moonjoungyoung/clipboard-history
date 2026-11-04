"""형식 묶음·이미지·파일 목록, 저장소 v2, 설정 검증, 이벤트 루프 내성 테스트(더미 데이터)."""
import json
import queue
import sqlite3
import struct
import tempfile
import time
import unittest
from pathlib import Path

from clipboard_history import config, crypto, formats
from clipboard_history.store import DAY, Store


def dib32(w, h, top_down=False):
    header = struct.pack("<IiiHHIIiiII", 40, w, -h if top_down else h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    px = bytearray()
    for y in range(h):
        for x in range(w):
            px += bytes((x % 256, y % 256, 128, 255))  # B G R A
    return header + bytes(px)


def dib24(w, h):
    stride = ((w * 24 + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, stride * h, 0, 0, 0, 0)
    rows = b"".join(bytes((10, 20, 30)) * w + b"\x00" * (stride - w * 3) for _ in range(h))
    return header + rows


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


class FormatsTest(unittest.TestCase):
    def test_pack_roundtrip(self):
        f = {"CF_UNICODETEXT": "안녕".encode("utf-16-le"), "HTML Format": b"<b>x</b>", "PNG": bytes(range(256))}
        self.assertEqual(formats.unpack_formats(formats.pack_formats(f)), f)

    def test_hdrop_roundtrip(self):
        paths = ["C:\\더미\\a.txt", "D:\\b c\\d.png"]
        self.assertEqual(formats.parse_hdrop(formats.build_hdrop(paths)), paths)
        self.assertEqual(formats.parse_hdrop(b"short"), [])

    def test_thumbnail_sizes(self):
        self.assertEqual(png_size(formats.dib_thumbnail_png(dib32(100, 50))), (100, 50))
        tw, th = png_size(formats.dib_thumbnail_png(dib32(1300, 700)))
        self.assertTrue(320 <= max(tw, th) <= formats.THUMB_MAX and abs(tw / th - 1300 / 700) < 0.05)
        self.assertEqual(png_size(formats.dib_thumbnail_png(dib32(40, 30, top_down=True))), (40, 30))
        self.assertEqual(png_size(formats.dib_thumbnail_png(dib24(33, 17))), (33, 17))
        self.assertIsNone(formats.dib_thumbnail_png(b"x" * 10))
        self.assertEqual(formats.dib_info(dib32(7, 9)), (7, 9, 32))

    def test_human_bytes(self):
        self.assertEqual(formats.human_bytes(512), "512B")
        self.assertEqual(formats.human_bytes(8_294_400), "7.9MB")


class StoreV2Test(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_image_and_files_roundtrip(self):
        s = Store(self.dir)
        dib = dib32(20, 10)
        thumb = formats.dib_thumbnail_png(dib)
        e, new = s.add("이미지 20×10", "a.exe", kind="image", formats={"CF_DIB": dib}, thumb=thumb,
                       meta={"w": 20, "h": 10, "bytes": len(dib)}, identity=dib)
        self.assertTrue(new and e.has_payload)
        e2, new2 = s.add("이미지 20×10", None, kind="image", formats={"CF_DIB": dib}, thumb=thumb,
                         meta={"w": 20, "h": 10}, identity=dib)
        self.assertFalse(new2)
        self.assertEqual(e.id, e2.id)
        got = s.get(e.id)
        self.assertEqual((got.kind, got.meta["w"]), ("image", 20))
        self.assertEqual(s.formats_for(got), {"CF_DIB": dib})
        self.assertEqual(s.thumb(e.id), thumb)
        files, _ = s.add("C:\\x\\a.txt\nC:\\x\\b.txt", None, kind="files", meta={"count": 2})
        hd = s.formats_for(s.get(files.id))["CF_HDROP"]
        self.assertEqual(formats.parse_hdrop(hd), ["C:\\x\\a.txt", "C:\\x\\b.txt"])
        txt, _ = s.add("서식 있는 더미", None, formats={"HTML Format": b"<i>x</i>"})
        f = s.formats_for(s.get(txt.id))
        self.assertEqual(f["HTML Format"], b"<i>x</i>")
        self.assertEqual(f["CF_UNICODETEXT"].decode("utf-16-le").rstrip("\x00"), "서식 있는 더미")
        # 디스크에 평문 이미지가 남지 않는다
        s.db.execute("PRAGMA wal_checkpoint(FULL)")
        raw = b"".join(p.read_bytes() for p in self.dir.iterdir() if p.name.startswith("history.db"))
        self.assertNotIn(dib[40:200], raw)
        s.close()

    def test_migrates_v1_database(self):
        key = crypto.load_or_create_key(self.dir / "key.bin")
        db = sqlite3.connect(str(self.dir / "history.db"))
        db.executescript("""
            CREATE TABLE entries(id INTEGER PRIMARY KEY AUTOINCREMENT, digest BLOB NOT NULL UNIQUE,
              blob BLOB NOT NULL, chars INTEGER NOT NULL, created_at REAL NOT NULL, last_used_at REAL NOT NULL,
              copy_count INTEGER NOT NULL DEFAULT 1, pinned INTEGER NOT NULL DEFAULT 0, source TEXT);
            PRAGMA user_version=1;""")
        now = time.time()
        db.execute("INSERT INTO entries(digest, blob, chars, created_at, last_used_at) VALUES(?,?,?,?,?)",
                   (crypto.digest(key, "v1 dummy"), crypto.protect("v1 dummy".encode()), 8, now, now))
        db.commit()
        db.close()
        s = Store(self.dir)
        self.assertEqual(s.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(s.db.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        [e] = s.load_all()
        self.assertEqual((e.text, e.kind, e.has_payload), ("v1 dummy", "text", False))
        _, new = s.add("v1 dummy")
        self.assertFalse(new)   # 기존 항목과 중복 판별이 이어진다
        s.close()

    def test_clock_jump_does_not_wipe(self):
        s = Store(self.dir)
        now = time.time()
        s.add("a", None, now=now - 10)
        s.add("b", None, now=now - 20)
        # 시계가 1년 앞으로 튄 것처럼 정리하면, 전부 지워지는 대신 아무것도 지우지 않는다
        self.assertEqual(s.purge(90, now=now + 365 * DAY), [])
        self.assertEqual(len(s.load_all()), 2)
        s.close()

    def test_broken_key_recovers(self):
        s = Store(self.dir)
        s.add("x")
        s.close()
        (self.dir / "key.bin").write_bytes(b"corrupted")
        with self.assertRaises(crypto.KeyInvalid):   # CLI 등은 복구하지 않고 실패만 한다
            Store(self.dir)
        self.assertTrue((self.dir / "key.bin").exists())
        s2 = Store(self.dir, allow_recover=True)
        self.assertIsNotNone(s2.recovered_from)
        self.assertEqual(s2.load_all(), [])
        self.assertTrue(any(p.name.startswith("history.db.broken-") for p in self.dir.iterdir()))
        s2.close()


class ConfigTest(unittest.TestCase):
    def test_bad_types_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"retention_days": None, "hotkey": 5, "exclude_patterns": "a",
                                     "max_chars": "9O", "zzz": 1}), encoding="utf-8")
            cfg, warns = config.load_config(Path(d))
            self.assertEqual((cfg.retention_days, cfg.hotkey, cfg.exclude_patterns, cfg.max_chars),
                             (90, "win+alt+v", [], 1_000_000))
            self.assertGreaterEqual(len(warns), 5)
            p.write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(config.load_config(Path(d))[0].retention_days, 90)


class PollResilienceTest(unittest.TestCase):
    def test_poll_survives_handler_error_and_quits(self):
        # 리뷰 HIGH-1: 이벤트 하나가 예외를 내도 다시 예약되고, quit 은 반드시 처리된다.
        from clipboard_history.app import App

        class FakeRoot:
            def __init__(self):
                self.scheduled, self.quitted = 0, False

            def after(self, ms, fn):
                self.scheduled += 1

            def quit(self):
                self.quitted = True

        stub = type("Stub", (), {})()
        stub.events = queue.Queue()
        stub.root = FakeRoot()
        handled = []

        def handle(kind, payload):
            handled.append(kind)
            if kind == "boom":
                raise RuntimeError("테스트 예외")

        stub._handle = handle
        stub._poll = lambda: None
        for k in ("boom", "clip", "quit"):
            stub.events.put((k, None))
        App._poll(stub)
        self.assertEqual(handled, ["boom", "clip"])
        self.assertTrue(stub.root.quitted)
        self.assertEqual(stub.root.scheduled, 1)


if __name__ == "__main__":
    unittest.main()
