"""상주 프로그램 본체: Win32 스레드의 이벤트를 메인(tk) 스레드에서 처리한다."""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path

from . import win32
from .config import Config, instance_tag, window_title
from .filters import SearchIndex, Snapshot, compile_patterns, menu_label, should_store
from .icons import ensure_icons
from .store import Entry, Store
from .ui import SearchWindow

log = logging.getLogger(__name__)

POLL_MS = 1000  # 안전용 느린 확인. 평소에는 이벤트가 들어오는 즉시 깨운다(_wake)
PURGE_MS = 3600 * 1000
DUP_WINDOW = 0.5   # 앱이 한 번 복사에 클립보드를 두 번 쓰는 경우(Chrome, Excel 등)를 한 번으로 친다
SAME_COPY_WINDOW = 10.0  # 같은 내용이 이 시간 안에 다시 오면(엑셀이 닫힐 때 다시 올림 등) 새 복사로 세지 않는다
MAX_PENDING_CLIPS = 64  # 저장이 밀릴 때 쌓아 둘 클립보드 이벤트 상한(큰 이미지가 연달아 오면 메모리 보호)
MAX_PENDING_BYTES = 256 * 1024 * 1024  # 대기 중인 복사 데이터 합계 상한


class App:
    def __init__(self, folder: Path, cfg: Config, warnings: list[str] | None = None):
        self.folder = folder
        self.cfg = cfg
        self.paused = False
        self.notices: list[str] = list(warnings or [])
        self.patterns = compile_patterns(cfg.exclude_patterns)
        if len(self.patterns) != len(cfg.exclude_patterns):
            self.notices.append("exclude_patterns 중 잘못된 정규식이 있어 그 패턴은 쓰지 않습니다.")
        self.events: queue.Queue = queue.Queue()
        self._last_sig: tuple | None = None
        self._looping = False
        self._pending_bytes = 0
        self._pending_lock = threading.Lock()
        self._last_sig_at = 0.0

        self.store = Store(folder, allow_recover=True)
        self._clock = (time.time(), time.monotonic())
        if self.store.recovered_from:
            self.notices.append("저장소 키를 복호화할 수 없어 새로 시작했습니다. 이전 파일은 *.broken-* 로 보관했습니다.")
        self._purge()
        self.index = SearchIndex(self.store.load_all())
        log.info("기록 불러옴: %d개", len(self.index))

        icon, paused_icon = ensure_icons(folder)
        self.host = win32.NativeHost(window_title(folder), self._emit, cfg.hotkey,
                                     str(icon), str(paused_icon), cfg.max_chars, cfg.max_bytes,
                                     cfg.exclude_processes)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = self._tk_error
        self.window = SearchWindow(self.root, self.index, self.copy_entry, self.delete_entry,
                                   self.toggle_pin, self.store.thumb, str(icon),
                                   retention_days=cfg.retention_days, position_mode=cfg.popup_position)
        self.host.locate_caret = cfg.popup_position == "caret"
        self.host.popup_hwnd = lambda: self._popup_hwnd

    def _emit(self, kind: str, payload) -> None:
        if kind == "clip":
            size = sum(len(v) for v in payload.formats.values()) if payload.formats else 0
            with self._pending_lock:
                if self.events.qsize() >= MAX_PENDING_CLIPS or self._pending_bytes + size > MAX_PENDING_BYTES:
                    log.warning("처리 대기 중인 복사가 너무 많아 하나를 건너뜀")
                    return
                self._pending_bytes += size
        self.events.put((kind, payload))  # quit 등 제어 이벤트는 항상 넣는다
        self._wake()

    def _wake(self) -> None:
        """다른 스레드에서 메인(tk) 스레드를 바로 깨운다. tkinter 가 메인 스레드로 넘겨 실행한다."""
        if not self._looping:
            return  # mainloop 전·후에는 느린 확인이 처리한다(여기서 기다리면 최대 1초 막힘)
        try:
            self.root.event_generate("<<ClipWake>>", when="tail")
        except (RuntimeError, tk.TclError):
            pass

    def _tk_error(self, exc, val, tb) -> None:
        log.error("UI 오류", exc_info=(exc, val, tb))

    # ---- 실행
    def run(self) -> None:
        self.root.update_idletasks()
        self._popup_hwnd = win32.GetAncestor(self.root.winfo_id(), win32.GA_ROOT)
        self.host.start()
        self._push_recent()
        self.root.bind("<<ClipWake>>", lambda e: self._drain())
        self.window.prewarm()  # 첫 표시 비용(창 생성·글꼴·도형)을 시작할 때 미리 치른다
        self.root.after(POLL_MS, self._poll)
        self.root.after(PURGE_MS, self._hourly)
        self.root.after(0, lambda: setattr(self, "_looping", True))
        log.info("시작 (단축키 %s)", self.host.hotkey_label or "없음")
        for note in self.notices:
            log.warning(note)
        if self.notices:
            self.root.after(1500, lambda: self.host.balloon("클립보드 기록", " ".join(self.notices)[:250]))
        try:
            self.root.mainloop()
        finally:
            self._looping = False
            self.host.stop()
            self.store.close()
            log.info("종료")

    def _poll(self) -> None:
        try:
            self._drain()
        finally:
            self.root.after(POLL_MS, self._poll)

    def _drain(self) -> None:
        for _ in range(200):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "quit":
                self.root.quit()
                return
            try:
                self._handle(kind, payload)
            except Exception:  # 이벤트 하나가 실패해도 기록은 계속돼야 한다
                log.exception("이벤트 처리 실패: %s", kind)

    def _clock_jumped(self) -> bool:
        """실행 중에 벽시계가 실제 흐른 시간(monotonic)보다 하루 넘게 앞서가면 시계가 튄 것으로 본다."""
        wall0, mono0 = self._clock
        return (time.time() - wall0) - (time.monotonic() - mono0) > 86400

    def _purge(self) -> list[int]:
        if getattr(self, "_clock", None) and self._clock_jumped():
            if not getattr(self, "_clock_warned", False):
                self._clock_warned = True
                log.warning("PC 시계가 크게 앞으로 바뀌어 이번 실행 동안 자동 정리를 멈춥니다")
                self.host.balloon("클립보드 기록", "PC 시계가 크게 바뀌어 오래된 기록 자동 정리를 잠시 멈췄습니다. 다시 시작하면 재개됩니다.")
            return []
        purged = self.store.purge(self.cfg.retention_days)
        if purged:
            log.info("보존 기간 경과 항목 삭제: %d개", len(purged))
        return purged

    def _hourly(self) -> None:
        try:
            purged = self._purge()
            for eid in purged:
                self.index.remove(eid)
            if purged:
                self._changed()
        except Exception:
            log.exception("정기 정리 실패")
        finally:
            self.root.after(PURGE_MS, self._hourly)

    def _handle(self, kind: str, payload) -> None:
        if kind == "clip":
            self.on_clip(payload)
        elif kind == "open":
            prev, caret, keep_target = payload
            self.window.show(prev, caret, keep_target)
        elif kind == "recent":
            e = self.index.get(payload)
            if e:
                self.copy_entry(e, False, None)
        elif kind == "menu":
            self.on_menu(payload)
        elif kind == "reload":
            self.index.replace_all(self.store.load_all())
            log.info("기록 다시 불러옴: %d개", len(self.index))
            self._changed()
        elif kind == "pause":
            paused, auto_resume = payload
            self.set_paused(paused, auto_resume)
        elif kind == "hotkey_failed":
            wanted, bad = payload
            label = self.host.hotkey_label
            if bad:
                msg = f"config.json 의 단축키 '{wanted}' 를 이해하지 못했습니다."
            else:
                msg = f"'{wanted}' 를 다른 프로그램이 쓰고 있습니다."
            msg += f" {label} 로 대신 등록했습니다." if label else " 트레이 아이콘을 클릭해 여세요."
            self.host.balloon("클립보드 기록", msg)

    def on_clip(self, snap: Snapshot, retry: bool = True) -> None:
        if snap.formats and retry:
            with self._pending_lock:
                self._pending_bytes = max(0, self._pending_bytes - sum(len(v) for v in snap.formats.values()))
        if self.paused:
            return
        if snap.skip_reason == "open_failed" and retry:
            # 다른 프로그램이 클립보드를 오래 쥐고 있었다. 잠시 뒤 한 번 더 읽는다.
            self.root.after(300, self.host.request_read)
            return
        reason = should_store(snap, self.cfg.exclude_processes, self.patterns)
        if reason:
            if reason not in ("no_text", "unsupported"):
                log.info("저장 안 함: %s", reason)
            return
        h = hashlib.blake2b(snap.kind.encode() + snap.text.encode("utf-8", "surrogatepass"), digest_size=16)
        for k, v in snap.formats.items():  # 순서도 의미가 있으므로(붙여넣는 앱의 선택) 정렬하지 않는다
            h.update(k.encode())
            h.update(v)
        sig = h.digest()  # 길이가 아니라 실제 내용으로 비교(같은 크기의 다른 이미지를 놓치지 않게)
        now = time.time()
        if sig == self._last_sig and now - self._last_sig_at < DUP_WINDOW:
            return
        self._last_sig, self._last_sig_at = sig, now

        identity = None
        if snap.kind == "image":
            identity = snap.formats.get("CF_DIB") or snap.formats.get("PNG")
        elif snap.kind == "files":
            identity = snap.text.encode("utf-8", "surrogatepass")  # 같은 경로 '텍스트'와 구분
        content_key = (snap.kind, hashlib.blake2b((identity or snap.text.encode("utf-8", "surrogatepass")),
                                                  digest_size=16).digest())
        # 같은 앱이 같은 내용을 10초 안에 다시 올린 것(엑셀이 닫히며 다시 올림 등)만 같은 복사의 후속 갱신으로 본다.
        # 기준 시각은 첫 복사 때만 정한다(10초보다 짧은 간격으로 계속 복사해도 매번 한 번으로 묶이지 않게).
        same_copy = (content_key == getattr(self, "_last_content", None)
                     and (snap.source or "") == getattr(self, "_last_content_src", "")
                     and now - getattr(self, "_last_content_at", 0.0) < SAME_COPY_WINDOW)
        if not same_copy:
            self._last_content, self._last_content_at = content_key, now
            self._last_content_src = snap.source or ""
        entry, new = self.store.add(snap.text, snap.source, kind=snap.kind, formats=snap.formats or None,
                                    thumb=snap.thumb, meta=snap.meta, identity=identity, bump=not same_copy,
                                    follow_up=same_copy)
        old = self.index.get(entry.id)
        if old and not new:
            entry.pinned, entry.source = old.pinned, entry.source or old.source
            entry.meta = entry.meta or old.meta
        self.index.put(entry)
        log.info("저장: id=%d %s %d자 %s 형식 %d개%s", entry.id, entry.kind, entry.chars, "신규" if new else "재사용",
                 len(snap.formats), " (일부 형식 못 받음)" if snap.incomplete else "")
        self._changed()

    def on_menu(self, cmd: int) -> None:
        if cmd == win32.MENU_OPEN:
            self.window.show(None, None, False)
        elif cmd == win32.MENU_PAUSE:
            self.set_paused(not self.paused)
        elif cmd == win32.MENU_FOLDER:
            os.startfile(str(self.folder))
        elif cmd == win32.MENU_QUIT:
            self.host.stop()

    def set_paused(self, paused: bool, auto_resume_s: int = 0) -> None:
        self.paused = paused
        self.host.set_paused(paused)
        job = getattr(self, "_resume_job", None)
        if job:
            self.root.after_cancel(job)
            self._resume_job = None
        if paused and auto_resume_s > 0:
            # 외부(테스트 등)가 멈춰 놓고 재개를 못 하고 죽어도 기록이 영영 멈추지 않게
            self._resume_job = self.root.after(auto_resume_s * 1000, lambda: self.set_paused(False))
        log.info("기록 %s%s", "일시정지" if paused else "재개",
                 f" ({auto_resume_s}초 뒤 자동 재개)" if paused and auto_resume_s else "")

    def _changed(self) -> None:
        self._push_recent()
        self.window.data_changed()

    def _push_recent(self) -> None:
        self.host.set_recent([(e.id, menu_label(e)) for e in self.index.recent(self.cfg.recent_menu_count)])

    # ---- 검색 창 동작
    def copy_entry(self, e: Entry, paste: bool, prev_hwnd) -> None:
        try:
            formats = self.store.formats_for(e)
            if not formats:
                raise OSError("저장된 원본이 없습니다.")
            win32.set_clipboard_formats(formats, self.host.hwnd)
        except Exception as exc:
            if self.window.visible():
                self.window.flash(f"복사하지 못했습니다: {exc}")
            else:  # 트레이 '최근 항목'에서 실패하면 창이 없으니 알림으로
                self.host.balloon("클립보드 기록", f"복사하지 못했습니다: {exc}")
            log.warning("복사 실패: id=%d %s", e.id, exc)
            return
        log.info("다시 복사: id=%d%s", e.id, " (붙여넣기)" if paste else "")
        seq = win32.GetClipboardSequenceNumber()
        target = prev_hwnd if paste and prev_hwnd and win32.IsWindow(prev_hwnd) else None
        risky = win32.risky_paste(target, e.kind, e.text) if target else None
        if risky:
            log.info("붙여넣기 대신 복사만: %s", risky)
            self.host.balloon("클립보드 기록", f"{risky} 바로 붙여넣지 않고 복사만 했습니다. 필요하면 Ctrl+V 하세요.")
            target = None
        if not self.window.visible():
            if target:  # 트레이에서 고른 경우 등
                self.root.after(80, lambda: self._paste_into(target, seq))
            return
        self._release_then(lambda: self._paste_into(target, seq) if target else None)

    _MODS = (win32.VK_SHIFT, win32.VK_RETURN, win32.VK_CONTROL, win32.VK_MENU, win32.VK_LWIN, win32.VK_RWIN)

    def _release_then(self, then, tries: int = 0) -> None:
        """Enter·Shift 를 뗀 다음에 창을 숨긴다(누른 채로 숨기면 반복 Enter 가 원래 창에 들어간다)."""
        if tries < 50 and any(win32.key_down(k) for k in (win32.VK_RETURN, win32.VK_SHIFT, 0x01)):  # 0x01: 왼쪽 버튼
            self.root.after(20, lambda: self._release_then(then, tries + 1))
            return
        self.window.hide()
        if then:
            self.root.after(30, then)

    def _paste_into(self, hwnd, seq: int) -> None:
        if not hwnd:
            return
        if win32.GetClipboardSequenceNumber() != seq:
            log.info("그 사이 클립보드가 바뀌어 붙여넣기를 취소했습니다")
            return
        fg = win32.GetForegroundWindow()
        own = self.window._own_hwnd()
        if fg and fg not in (hwnd, own) and not win32.is_shell_window(fg):
            log.info("다른 창으로 옮겨 가 붙여넣기를 취소했습니다(복사는 됨)")
            return
        if any(win32.key_down(k) for k in self._MODS):
            log.info("키가 눌려 있어 붙여넣기를 취소했습니다(복사는 됨)")
            return
        if not win32.force_foreground(hwnd):
            log.info("붙여넣기 대상 창으로 전환하지 못해 복사만 했습니다")
            return

        def paste():
            # 마지막 확인: 대상 창·클립보드·수정 키(Enter 포함)
            if (win32.GetForegroundWindow() != hwnd or win32.GetClipboardSequenceNumber() != seq
                    or any(win32.key_down(k) for k in self._MODS)):
                log.info("붙여넣기 직전 상태가 바뀌어 취소했습니다(복사는 됨)")
                return
            win32.send_ctrl_v()

        self.root.after(60, paste)

    def delete_entry(self, e: Entry) -> None:
        self.store.delete(e.id)
        self.index.remove(e.id)
        log.info("삭제: id=%d", e.id)
        self._push_recent()

    def toggle_pin(self, e: Entry) -> None:
        e.pinned = not e.pinned
        self.store.set_pinned(e.id, e.pinned)
        self.index.put(e)
        log.info("고정 %s: id=%d", "설정" if e.pinned else "해제", e.id)


def run(folder: Path, cfg: Config, warnings: list[str] | None = None) -> int:
    try:
        # 모니터마다 DPI 가 다를 때 좌표·크기를 물리 픽셀로 맞추는 PMv2 를 먼저, 안 되면 PMv1
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    mutex = win32.single_instance(f"Local\\ClipboardHistory-{instance_tag(folder)}")
    if mutex is None:
        log.info("이미 실행 중이라 종료")
        return 0
    App(folder, cfg, warnings).run()
    return 0
