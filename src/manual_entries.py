from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src import database as db
from src.config import (
    DATA_DIR,
    DEFAULT_CHANNEL,
    MANUAL_ENTRY_CATEGORY_FALLBACK,
    MANUAL_ENTRY_DEFAULT_STATUS,
    MANUAL_ENTRY_TAGS,
    UPLOADS_DIR,
)


def _sanitize_filename(name: str) -> str:
    name = Path(name).name  # 경로 구분자가 섞여 들어와도 파일명만 남긴다.
    return re.sub(r"[^\w\-.]", "_", name) or "image"


def _save_uploaded_images(files) -> list[str]:
    """업로드된 이미지들을 data/uploads/<uuid>/ 아래 저장하고, DATA_DIR 기준 상대경로 목록을 반환한다.

    동일한 파일명이 여러 장 올라와도 충돌하지 않도록 업로드 순번을 파일명 앞에 붙인다.
    """
    if not files:
        return []
    folder = UPLOADS_DIR / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    for i, f in enumerate(files):
        filename = f"{i:02d}_{_sanitize_filename(f.name)}"
        dest = folder / filename
        dest.write_bytes(f.getvalue())
        saved_paths.append(str(dest.relative_to(DATA_DIR)).replace("\\", "/"))
    return saved_paths


def create_manual_entry(
    *,
    source_url: str,
    account_name: str,
    platform: str,
    country: str,
    language: str,
    posted_at: str,
    view_count: int | None,
    like_count: int | None,
    comment_count: int | None,
    share_count: int | None,
    follower_count: int,
    images,
    memo: str,
    tags: list[str],
    status: str,
    channel: str = DEFAULT_CHANNEL,
) -> int:
    image_paths = _save_uploaded_images(images)
    now = datetime.now(timezone.utc).isoformat()
    return db.insert_manual_entry(
        {
            "source_url": source_url,
            "account_name": account_name,
            "platform": platform,
            "country": country,
            "language": language,
            "posted_at": posted_at,
            "view_count": view_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "share_count": share_count,
            "follower_count": follower_count,
            "image_paths": json.dumps(image_paths, ensure_ascii=False),
            "memo": memo,
            "tags": ",".join(tags),
            "channel": channel or DEFAULT_CHANNEL,
            "status": status or MANUAL_ENTRY_DEFAULT_STATUS,
            "created_at": now,
            "updated_at": now,
        }
    )


def get_all_manual_entry_categories() -> list[str]:
    """기본 카테고리(config.MANUAL_ENTRY_TAGS) + 사용자가 추가한 커스텀 카테고리를 합쳐 반환한다.

    "기타"는 항상 마지막에 오도록 고정한다(예비 카테고리 역할).
    """
    base = [c for c in MANUAL_ENTRY_TAGS if c != MANUAL_ENTRY_CATEGORY_FALLBACK]
    custom = [
        c for c in db.get_manual_entry_categories()
        if c not in base and c != MANUAL_ENTRY_CATEGORY_FALLBACK
    ]
    return base + custom + [MANUAL_ENTRY_CATEGORY_FALLBACK]


def add_manual_entry_category(name: str) -> None:
    name = name.strip()
    if not name:
        raise ValueError("카테고리 이름을 입력하세요.")
    if name in get_all_manual_entry_categories():
        raise ValueError("이미 있는 카테고리입니다.")
    db.insert_manual_entry_category(name)


def get_image_full_paths(image_paths_json: str | None) -> list[Path]:
    if not image_paths_json:
        return []
    try:
        rel_paths = json.loads(image_paths_json)
    except json.JSONDecodeError:
        return []
    return [DATA_DIR / p for p in rel_paths]


def delete_manual_entry(entry_id: int) -> None:
    """DB 레코드를 삭제하고, 해당 항목의 업로드 폴더도 함께 정리한다."""
    image_paths_json = db.delete_manual_entry(entry_id)
    if not image_paths_json:
        return
    try:
        rel_paths = json.loads(image_paths_json)
    except json.JSONDecodeError:
        return
    if not rel_paths:
        return
    folder = (DATA_DIR / rel_paths[0]).parent
    # UPLOADS_DIR 하위 폴더가 맞는지 확인 후에만 삭제한다(잘못된 경로로 인한 오삭제 방지).
    if folder.exists() and folder.is_dir() and folder.is_relative_to(UPLOADS_DIR):
        shutil.rmtree(folder, ignore_errors=True)
