from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src import database as db
from src.config import YOUTUBE_API_KEY


def mask_key(api_key: str) -> str:
    """키를 화면에 노출하지 않도록 일부만 보여준다. 예: AIzaSy****...89Ew"""
    if len(api_key) <= 10:
        return api_key[:2] + "****"
    return f"{api_key[:6]}****...{api_key[-4:]}"


def ensure_default_key_migrated() -> None:
    """DB에 등록된 키가 하나도 없고 .env에 YOUTUBE_API_KEY가 있으면 최초 1회 '기본 키'로 등록한다."""
    if db.get_youtube_api_keys():
        return
    if not YOUTUBE_API_KEY:
        return
    try:
        db.insert_youtube_api_key(YOUTUBE_API_KEY)
    except sqlite3.IntegrityError:
        pass


def add_key(api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("API 키를 입력하세요.")
    try:
        db.insert_youtube_api_key(api_key)
    except sqlite3.IntegrityError:
        raise ValueError("이미 등록된 키입니다.")


def remove_key(key_id: int) -> None:
    db.delete_youtube_api_key(key_id)


def has_any_key() -> bool:
    return bool(db.get_youtube_api_keys())


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def key_status(row: dict, today: str | None = None) -> str:
    """정상 / 오늘 쿼터 소진 / 미확인 중 하나를 반환한다."""
    today = today or _today()
    if row.get("exhausted_date") == today:
        return "오늘 쿼터 소진"
    if row.get("last_used_at"):
        return "정상"
    return "미확인"


def list_keys_with_status() -> list[dict]:
    """등록 순서대로 순번·마스킹된 키·상태를 붙여 반환한다(화면 표시용)."""
    today = _today()
    keys = db.get_youtube_api_keys()
    return [
        {
            **row,
            "position": i,
            "masked": mask_key(row["api_key"]),
            "status": key_status(row, today),
        }
        for i, row in enumerate(keys, start=1)
    ]


def get_next_available_key(exclude_ids: set[int] | None = None) -> dict | None:
    """오늘 기준으로 소진되지 않은 키 중 등록 순서상 가장 앞선 것을 반환한다. 없으면 None."""
    exclude_ids = exclude_ids or set()
    today = _today()
    for row in db.get_youtube_api_keys():
        if row["id"] in exclude_ids:
            continue
        if row.get("exhausted_date") == today:
            continue
        return row
    return None


def mark_key_used(key_id: int) -> None:
    db.touch_youtube_api_key_used(key_id)


def mark_key_exhausted(key_id: int) -> None:
    db.set_youtube_api_key_exhausted(key_id, _today())


def get_current_key_position() -> tuple[int, int] | None:
    """가장 최근에 성공적으로 사용된 키의 (순번, 전체 키 수)를 반환한다. 사용 이력이 없으면 None."""
    keys = db.get_youtube_api_keys()
    used = [r for r in keys if r.get("last_used_at")]
    if not used:
        return None
    latest = max(used, key=lambda r: r["last_used_at"])
    for i, row in enumerate(keys, start=1):
        if row["id"] == latest["id"]:
            return i, len(keys)
    return None
