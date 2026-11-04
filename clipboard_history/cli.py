"""명령줄 진입점.

  python -m clipboard_history [run]     상주 실행(기본)
  python -m clipboard_history open      실행 중인 인스턴스의 검색 창 열기
  python -m clipboard_history quit      실행 중인 인스턴스 종료
  python -m clipboard_history pause     기록 일시정지 / resume 재개
  python -m clipboard_history stats     개수·용량 등 통계(내용은 출력 안 함)
  python -m clipboard_history search 검색어 [--show]   일치 항목 id 목록(--show 면 미리보기 포함)
  python -m clipboard_history copy ID   해당 항목을 클립보드에 다시 복사
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .config import data_dir, load_config, window_title


def _setup_logging(folder, to_file: bool) -> None:
    handlers: list[logging.Handler] = []
    if to_file:
        handlers.append(RotatingFileHandler(folder / "app.log", maxBytes=1_000_000, backupCount=2,
                                            encoding="utf-8"))
    elif sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO if to_file else logging.WARNING, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _find_instance(folder):
    from . import win32
    return win32.FindWindowW(None, window_title(folder))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipboard_history", description="클립보드 기록")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("open")
    sub.add_parser("quit")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("stats")
    p_search = sub.add_parser("search")
    p_search.add_argument("query", nargs="*")
    p_search.add_argument("--show", action="store_true", help="미리보기 문자열도 출력")
    p_search.add_argument("--limit", type=int, default=20)
    p_copy = sub.add_parser("copy")
    p_copy.add_argument("id", type=int)
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # 파이프(cp949)로 내보낼 때 이모지 등에서 죽지 않게

    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _setup_logging(folder, to_file=(cmd == "run"))  # 설정을 읽다 실패해도 로그는 남게 먼저
    cfg, warnings = load_config(folder)

    if cmd == "run":
        from .app import run
        try:
            return run(folder, cfg, warnings)
        except Exception:
            logging.getLogger(__name__).exception("비정상 종료")
            return 1

    if cmd in ("pause", "resume"):
        from . import win32
        hwnd = _find_instance(folder)
        if not hwnd:
            print("실행 중인 인스턴스가 없습니다.", file=sys.stderr)
            return 1
        win32.PostMessageW(hwnd, win32.WM_APP_PAUSE, 1 if cmd == "pause" else 0, 0)
        return 0

    if cmd in ("open", "quit"):
        from . import win32
        hwnd = _find_instance(folder)
        if not hwnd:
            print("실행 중인 인스턴스가 없습니다.", file=sys.stderr)
            return 1
        win32.PostMessageW(hwnd, win32.WM_APP_OPEN if cmd == "open" else win32.WM_CLOSE, 0, 0)
        if cmd == "quit":
            for _ in range(50):
                if not _find_instance(folder):
                    return 0
                time.sleep(0.1)
            print("종료를 확인하지 못했습니다.", file=sys.stderr)
            return 1
        return 0

    from .store import Store
    store = Store(folder)
    try:
        if cmd == "stats":
            st = store.stats()
            for key in ("oldest_created_at", "newest_used_at"):
                if st[key]:
                    st[key] = datetime.fromtimestamp(st[key]).isoformat(timespec="seconds")
            st["data_dir"] = str(folder)
            st["retention_days"] = cfg.retention_days
            st["running"] = bool(_find_instance(folder))
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 0
        if cmd == "search":
            from .filters import SearchIndex, preview
            results, total = SearchIndex(store.load_all()).search(" ".join(args.query), args.limit)
            print(f"{total}개 일치")
            for e in results:
                when = datetime.fromtimestamp(e.last_used_at).strftime("%Y-%m-%d %H:%M")
                line = f"{e.id}\t{when}\t{e.kind}\t{e.chars}자\t{e.copy_count}회"
                if args.show:
                    line += "\t" + preview(e.text, 80)
                print(line)
            return 0
        if cmd == "copy":
            from . import win32
            e = store.get(args.id)
            if not e:
                print(f"id {args.id} 항목이 없습니다.", file=sys.stderr)
                return 1
            win32.set_clipboard_formats(store.formats_for(e))
            print(f"id {e.id} 복사함 ({e.kind}, {e.chars}자)")
            return 0
    finally:
        store.close()
    return 0
