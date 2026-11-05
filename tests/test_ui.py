"""검색 창 로직 테스트. 창은 화면에 띄우지 않는다(withdrawn 상태로 위젯만 구동)."""
import time
import unittest

from clipboard_history import win32
from clipboard_history.filters import SearchIndex
from clipboard_history.store import DAY, Entry

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.destroy()
    HAS_TK = True
except Exception:  # 데스크톱이 없는 환경
    HAS_TK = False


@unittest.skipIf(not HAS_TK, "Tk 를 만들 수 없는 환경")
class SearchWindowTest(unittest.TestCase):
    def setUp(self):
        from clipboard_history import ui
        self.ui = ui
        now = time.time()
        self.entries = [
            Entry(1, "alpha 사과 dummy", 1, now - 600, 1, False, "notepad.exe"),
            Entry(2, "beta 바나나 dummy\n둘째 줄", 1, now - 60, 2, False, None),
            Entry(3, "gamma 포도", 1, now - 300, 1, False, None),
            Entry(4, "old pinned", 1, now - 40 * DAY, 1, True, None),
            Entry(5, "이미지 10×10", 1, now - 30, 1, False, None, "image", {"w": 10, "h": 10, "bytes": 400}, True),
        ]
        self.index = SearchIndex(self.entries)
        self.copied, self.deleted, self.pinned = [], [], []
        self.root = tk.Tk()
        self.root.withdraw()

        def on_delete(e):
            self.deleted.append(e.id)
            self.index.remove(e.id)

        def on_pin(e):
            e.pinned = not e.pinned
            self.pinned.append(e.id)

        self.w = ui.SearchWindow(self.root, self.index,
                                 lambda e, paste, prev: self.copied.append((e.id, paste, prev)),
                                 on_delete, on_pin, lambda _id: None, None, 90)

    def tearDown(self):
        self.root.destroy()

    def ids(self):
        return [e.id for e in self.w.results]

    def test_pinned_first_then_recent_with_headers(self):
        self.w.refresh()
        self.assertEqual(self.ids(), [4, 5, 2, 3, 1])
        headers = [r[1] for r in self.w.rows if r[0] == "header"]
        self.assertEqual(headers[0], "고정")
        self.assertIn("오늘", headers)
        self.assertEqual(self.w.selected().id, 4)

    def test_enter_right_after_typing_copies_new_result(self):
        # 리뷰 HIGH-2: 갱신 대기(디바운스) 중에 Enter 를 눌러도 방금 친 검색어 기준으로 복사해야 한다.
        self.w.refresh()
        self.w.query.set("사과")      # 갱신은 아직 예약 상태
        self.w._copy(False)
        self.assertEqual(self.copied[-1][:2], (1, False))

    def test_search_marks_and_count(self):
        self.w.query.set("dummy")
        self.w.refresh()
        self.assertEqual(self.ids(), [2, 1])
        self.assertEqual(self.w.chrome.itemcget(self.w.count_id, "text"), "2개")
        self.assertTrue(self.w.text.tag_ranges("mark"))

    def test_move_pin_delete(self):
        self.w.refresh()
        self.w._move(2)
        self.assertEqual(self.w.selected().id, 2)
        self.w._pin()
        self.assertEqual(self.pinned, [2])
        self.assertEqual(self.ids()[:2], [2, 4])   # 고정하면 위로(고정끼리는 최근 순)
        self.w._select(self.ids().index(3))
        self.w._delete()
        self.assertEqual(self.deleted, [3])
        self.assertNotIn(3, self.ids())

    def test_no_match(self):
        self.w.query.set("없는검색어")
        self.w.refresh()
        self.assertEqual(self.w.results, [])
        self.w._copy(False)
        self.assertEqual(self.copied, [])

    def test_escape_clears_then_hides(self):
        self.w.query.set("abc")
        self.w._escape()
        self.assertEqual(self.w.query.get(), "")

    def test_show_keeps_original_target_window(self):
        # 리뷰 MEDIUM-1: 창이 떠 있을 때 단축키를 다시 눌러도 붙여넣을 원래 창을 잃지 않는다.
        orig = win32.force_foreground
        win32.force_foreground = lambda h: True
        try:
            self.w.show(12345)
            self.w.show(None, None, True)          # 떠 있을 때 단축키를 다시 누름
            self.assertEqual(self.w.prev_hwnd, 12345)
            self.w._copy(True)
            self.assertEqual(self.copied[-1][2], 12345)
            self.w.show(None, None, False)         # 트레이로 다시 열면 붙여넣을 대상 없음
            self.assertIsNone(self.w.prev_hwnd)
        finally:
            win32.force_foreground = orig
            self.w.hide()

    def test_keybindings_exist(self):
        for seq in ("<Return>", "<Shift-Return>", "<Escape>", "<Control-p>", "<Control-P>", "<Control-d>", "<Down>"):
            self.assertTrue(self.w.entry.bind(seq), seq)

    def test_helpers(self):
        now = self.ui.datetime.now()
        self.assertEqual(self.ui.bucket(time.time(), now), "오늘")
        self.assertEqual(self.ui.bucket(time.time() - DAY, now), "어제")
        files = Entry(9, "C:\\a\\b.txt\nC:\\a\\c.txt", 1, 1, 1, False, "explorer.exe", "files")
        self.assertEqual(self.ui.entry_title(files), "b.txt  외 1개")
        self.assertIn("C:\\a", self.ui.entry_meta(files, now))


if __name__ == "__main__":
    unittest.main()
