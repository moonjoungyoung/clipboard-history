"""설정과 데이터 경로.

데이터 폴더: 환경변수 CLIPHIST_DATA_DIR 가 있으면 그 경로, 없으면 %LOCALAPPDATA%\\ClipboardHistory.
설정 파일: <데이터 폴더>\\config.json (없으면 기본값으로 만든다).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "ClipboardHistory"

DEFAULT_EXCLUDE_PROCESSES = [
    "KeePass.exe",
    "KeePassXC.exe",
    "1Password.exe",
    "Bitwarden.exe",
    "Enpass.exe",
]


@dataclass
class Config:
    retention_days: int = 90          # 마지막으로 복사한 지 이 기간이 지난 항목은 삭제(고정 항목 제외)
    hotkey: str = "win+alt+v"         # 검색 창 단축키
    popup_position: str = "caret"     # caret: 입력 커서 근처 / center: 화면 위쪽 가운데
    max_chars: int = 1_000_000        # 이보다 긴 텍스트는 저장하지 않음
    max_bytes: int = 64 * 1024 * 1024  # 이미지·서식 한 형식이 이보다 크면 저장하지 않음
    recent_menu_count: int = 15       # 트레이 '최근 항목' 개수
    exclude_processes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PROCESSES))
    exclude_patterns: list[str] = field(default_factory=list)  # 정규식. 하나라도 맞으면 저장 안 함


def data_dir() -> Path:
    override = os.environ.get("CLIPHIST_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def instance_tag(path: Path | None = None) -> str:
    """데이터 폴더마다 다른 인스턴스 식별자(뮤텍스·창 제목용)."""
    p = str((path or data_dir()).resolve()).lower()
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:12]


def window_title(path: Path | None = None) -> str:
    return f"{APP_NAME}:{instance_tag(path)}"


def load_config(path: Path | None = None) -> tuple[Config, list[str]]:
    """(설정, 경고 목록). 값의 형식이 틀리면 그 항목만 기본값으로 되돌리고 경고를 남긴다."""
    d = path or data_dir()
    d.mkdir(parents=True, exist_ok=True)
    cfg_path = d / "config.json"
    cfg, warnings = Config(), []
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg, warnings
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return cfg, [f"config.json 을 읽지 못해 기본값을 씁니다: {exc}"]
    if not isinstance(raw, dict):
        return cfg, ["config.json 최상위가 객체가 아니라 기본값을 씁니다."]

    def int_field(key, lo, hi):
        v = raw.get(key, getattr(cfg, key))
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            warnings.append(f"{key} 는 숫자여야 합니다. 기본값 {getattr(cfg, key)} 사용")
            return
        setattr(cfg, key, max(lo, min(hi, int(v))))

    def list_field(key):
        v = raw.get(key, getattr(cfg, key))
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            warnings.append(f"{key} 는 문자열 목록이어야 합니다. 기본값 사용")
            return
        setattr(cfg, key, list(v))

    int_field("retention_days", 1, 36500)
    int_field("max_chars", 1, 50_000_000)
    int_field("max_bytes", 1024, 1024 * 1024 * 1024)
    int_field("recent_menu_count", 0, 40)
    list_field("exclude_processes")
    list_field("exclude_patterns")
    hk = raw.get("hotkey", cfg.hotkey)
    if isinstance(hk, str) and hk.strip():
        cfg.hotkey = hk.strip().lower()
    else:
        warnings.append(f"hotkey 는 문자열이어야 합니다. 기본값 {cfg.hotkey} 사용")
    pos = raw.get("popup_position", cfg.popup_position)
    if pos in ("caret", "center"):
        cfg.popup_position = pos
    else:
        warnings.append("popup_position 은 'caret' 또는 'center' 입니다. 기본값 caret 사용")
    unknown = set(raw) - set(asdict(cfg))
    if unknown:
        warnings.append("알 수 없는 설정 무시: " + ", ".join(sorted(unknown)))
    return cfg, warnings
