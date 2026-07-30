from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd

from src.config import (
    DB_PATH,
    DEFAULT_CHANNEL,
    MANUAL_ENTRY_CATEGORY_FALLBACK,
    MANUAL_ENTRY_TAGS,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel_title TEXT,
    channel_id TEXT,
    thumbnail_url TEXT,
    published_at TEXT,
    view_count INTEGER,
    like_count INTEGER,
    subscriber_count INTEGER,
    is_short INTEGER,
    views_per_sub REAL,
    channel_avg_multiple REAL,
    views_per_day REAL,
    like_rate REAL,
    viral_score REAL,
    tags TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    channel TEXT NOT NULL DEFAULT '미정',
    saved_at TEXT NOT NULL
);
"""

# 검색 결과로 마주친 영상을 매번 기록해 대시보드 통계(오늘 발견 수, 급상승 영상,
# 반복 등장 키워드)를 산출하는 데 쓰는 발견 이력 테이블.
DISCOVERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS discoveries (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel_title TEXT,
    thumbnail_url TEXT,
    published_at TEXT,
    view_count INTEGER,
    viral_score REAL,
    views_per_day REAL,
    keyword TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
"""

# 해외 카드뉴스 수동 등록 항목. 즐겨찾기(favorites)와는 별개 테이블이며,
# 이미지는 로컬 폴더(UPLOADS_DIR)에 저장하고 여기엔 상대경로 목록(JSON)만 기록한다.
MANUAL_ENTRIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT,
    account_name TEXT NOT NULL,
    platform TEXT,
    country TEXT,
    language TEXT,
    posted_at TEXT,
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    follower_count INTEGER,
    image_paths TEXT DEFAULT '[]',
    memo TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    channel TEXT NOT NULL DEFAULT '미정',
    status TEXT NOT NULL DEFAULT '미검토',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 수동 등록 항목 1건당 AI 분석 결과 1건(최신 결과로 덮어씀).
# manual_entries가 삭제되면 ON DELETE CASCADE로 함께 정리된다(get_conn에서 FK를 켜야 동작).
AI_ANALYSES_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_analyses (
    entry_id INTEGER PRIMARY KEY,
    extracted_texts TEXT DEFAULT '[]',
    hook_type TEXT,
    hook_type_reason TEXT,
    adaptation_ideas TEXT DEFAULT '[]',
    score_hidden_story INTEGER,
    score_title_twist INTEGER,
    score_product_new_view INTEGER,
    score_conflict_resolution INTEGER,
    score_visual_material INTEGER,
    score_purchasable_link INTEGER,
    score_fact_and_fun INTEGER,
    total_score INTEGER,
    is_recommended INTEGER,
    analyzed_at TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES manual_entries (id) ON DELETE CASCADE
);
"""

# 등록된 YouTube API 키 목록. exhausted_date가 오늘 날짜와 같으면 "오늘 쿼터 소진"으로 간주하고
# 자동 전환 로직(youtube_client.execute_with_rotation)이 이 키를 건너뛴다.
YOUTUBE_API_KEYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL,
    last_used_at TEXT,
    exhausted_date TEXT
);
"""

# "전체 바이럴 영상" 탭 캐시. 쇼핑 키워드 search.list로 받아온 결과를 통째로 저장해두고,
# 새로고침할 때마다 전체 교체한다(하루 최대 2회로 제한되므로 이력 누적 없이 스냅샷만 유지).
VIRAL_VIDEOS_SCHEMA = """
CREATE TABLE IF NOT EXISTS viral_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    keyword TEXT,
    title TEXT,
    channel_id TEXT,
    channel_title TEXT,
    thumbnail_url TEXT,
    published_at TEXT,
    view_count INTEGER,
    like_count INTEGER,
    duration_seconds INTEGER,
    is_short INTEGER,
    subscriber_count INTEGER,
    fetched_at TEXT NOT NULL
);
"""

# 마지막 새로고침 시각 1건만 기록하는 단일행 테이블. "하루 최대 2회"(12시간 간격) 제한의 기준점이 된다.
VIRAL_REFRESH_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS viral_refresh_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_refreshed_at TEXT NOT NULL
);
"""

# 사용자가 "새 카테고리 추가"로 직접 등록한 수동 등록 항목용 카테고리.
# 기본 20종(config.MANUAL_ENTRY_TAGS)에 없는 것만 여기 쌓인다.
MANUAL_ENTRY_CATEGORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS manual_entry_categories (
    category TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""

# "급성장 채널 > 채널 직접 검색"에서 저장한 채널 스냅샷. favorites(영상 전용)와는 별개 테이블이다.
SAVED_CHANNELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_channels (
    channel_id TEXT PRIMARY KEY,
    channel_title TEXT NOT NULL,
    thumbnail_url TEXT,
    published_at TEXT,
    subscriber_count INTEGER,
    video_count INTEGER,
    months_active REAL,
    label TEXT,
    outlier_ratio REAL,
    shorts_ratio REAL,
    avg_views_recent REAL,
    median_views_recent REAL,
    upload_count_recent INTEGER,
    saved_at TEXT NOT NULL
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """테이블에 컬럼이 없으면 추가한다(기존 DB에 새 필드를 안전하게 확장하는 마이그레이션)."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_legacy_manual_entry_tags(conn: sqlite3.Connection) -> None:
    """구 태그 체계(요잇템/숨은이야기형 등)로 저장된 값을 새 주제 카테고리 목록에 없으면 "기타"로 매핑한다.

    이미 유효한 값은 그대로 두므로 매번 실행해도 안전하다(멱등). 커스텀 카테고리
    (manual_entry_categories)도 유효한 값으로 취급해 잘못 "기타"로 뭉개지 않게 한다.
    """
    custom = {
        row["category"] for row in conn.execute("SELECT category FROM manual_entry_categories").fetchall()
    }
    valid = set(MANUAL_ENTRY_TAGS) | custom
    rows = conn.execute("SELECT id, tags FROM manual_entries").fetchall()
    for row in rows:
        old_tags = [t for t in (row["tags"] or "").split(",") if t]
        if not old_tags:
            continue
        new_tags = [t if t in valid else MANUAL_ENTRY_CATEGORY_FALLBACK for t in old_tags]
        # 중복 제거(순서 유지) — 여러 구 태그가 전부 "기타"로 뭉개지면 중복이 생길 수 있다.
        deduped = list(dict.fromkeys(new_tags))
        if deduped != old_tags:
            conn.execute(
                "UPDATE manual_entries SET tags = ? WHERE id = ?",
                (",".join(deduped), row["id"]),
            )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(SCHEMA)
        conn.execute(DISCOVERIES_SCHEMA)
        conn.execute(MANUAL_ENTRIES_SCHEMA)
        conn.execute(AI_ANALYSES_SCHEMA)
        conn.execute(YOUTUBE_API_KEYS_SCHEMA)
        # 카테고리(mostPopular) 방식 -> 키워드(search.list) 방식 전환에 따른 1회성 스키마 마이그레이션.
        # 기존 테이블에 옛 컬럼(category_id)이 남아있으면 통째로 비우고 새 스키마로 다시 만든다.
        # 순수 캐시 데이터라 유실돼도 다음 새로고침에서 곧바로 다시 채워지므로 손실 위험이 없다.
        # last_refreshed_at도 함께 지워야 "12시간 이내"로 오인해 자동 재수집을 건너뛰지 않는다.
        existing_viral_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(viral_videos)").fetchall()
        }
        if "category_id" in existing_viral_cols:
            conn.execute("DROP TABLE IF EXISTS viral_videos")
            conn.execute("DROP TABLE IF EXISTS viral_refresh_state")
        conn.execute(VIRAL_VIDEOS_SCHEMA)
        conn.execute(VIRAL_REFRESH_STATE_SCHEMA)
        conn.execute(SAVED_CHANNELS_SCHEMA)
        conn.execute(MANUAL_ENTRY_CATEGORIES_SCHEMA)
        # 초안(대본) 탭 제거에 따른 정리. 이미 삭제된 DB에서는 그대로 no-op된다.
        conn.execute("DROP TABLE IF EXISTS drafts")
        # 기존 DB(채널 컬럼 도입 이전)를 위한 마이그레이션. 신규 DB는 위
        # CREATE TABLE에 이미 포함돼 있으므로 아래는 그대로 no-op된다.
        _ensure_column(conn, "favorites", "channel", "channel TEXT NOT NULL DEFAULT '미정'")
        _ensure_column(conn, "manual_entries", "channel", "channel TEXT NOT NULL DEFAULT '미정'")
        _migrate_legacy_manual_entry_tags(conn)


def is_favorite(video_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM favorites WHERE video_id = ?", (video_id,)
        ).fetchone()
    return row is not None


def _int(v) -> int | None:
    return None if v is None else int(v)


def _float(v) -> float | None:
    return None if v is None else float(v)


def save_favorite(
    video: dict, tags: list[str], memo: str, channel: str = DEFAULT_CHANNEL
) -> None:
    """검색 결과(dict) + 태그 리스트 + 메모 + 채널을 즐겨찾기로 저장/갱신한다.

    dict가 pandas row에서 왔을 경우 numpy 스칼라(int64/float64)가 섞여 있어
    sqlite3가 직접 바인딩하지 못하므로 네이티브 int/float로 변환한다.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO favorites (
                video_id, title, channel_title, channel_id, thumbnail_url,
                published_at, view_count, like_count, subscriber_count, is_short,
                views_per_sub, channel_avg_multiple, views_per_day, like_rate,
                viral_score, tags, memo, channel, saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title=excluded.title,
                channel_title=excluded.channel_title,
                channel_id=excluded.channel_id,
                thumbnail_url=excluded.thumbnail_url,
                published_at=excluded.published_at,
                view_count=excluded.view_count,
                like_count=excluded.like_count,
                subscriber_count=excluded.subscriber_count,
                is_short=excluded.is_short,
                views_per_sub=excluded.views_per_sub,
                channel_avg_multiple=excluded.channel_avg_multiple,
                views_per_day=excluded.views_per_day,
                like_rate=excluded.like_rate,
                viral_score=excluded.viral_score,
                tags=excluded.tags,
                memo=excluded.memo,
                channel=excluded.channel
            """,
            (
                video["video_id"],
                video["title"],
                video.get("channel_title"),
                video.get("channel_id"),
                video.get("thumbnail_url"),
                video.get("published_at"),
                _int(video.get("view_count", 0)),
                _int(video.get("like_count", 0)),
                _int(video.get("subscriber_count", 0)),
                int(bool(video.get("is_short", False))),
                _float(video.get("views_per_sub")),
                _float(video.get("channel_avg_multiple")),
                _float(video.get("views_per_day")),
                _float(video.get("like_rate")),
                _float(video.get("viral_score")),
                ",".join(tags),
                memo,
                channel or DEFAULT_CHANNEL,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def update_tags_memo(video_id: str, tags: list[str], memo: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE favorites SET tags = ?, memo = ? WHERE video_id = ?",
            (",".join(tags), memo, video_id),
        )


def remove_favorite(video_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM favorites WHERE video_id = ?", (video_id,))


def get_favorite_ids() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT video_id FROM favorites").fetchall()
    return {row["video_id"] for row in rows}


def get_favorite(video_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM favorites WHERE video_id = ?", (video_id,)
        ).fetchone()
    return dict(row) if row else None


def get_all_favorites() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM favorites ORDER BY saved_at DESC", conn
        )
    return df


def log_discoveries(videos: pd.DataFrame, keyword: str) -> None:
    """검색 결과를 발견 이력에 기록한다.

    최초 발견일(first_seen_at)과 검색 키워드는 최초 삽입 시점 값을 보존하고,
    조회수/점수/최종 확인일만 매번 갱신한다(재검색 시 "오늘 새로 발견"이 중복 집계되지 않도록).
    """
    if videos.empty:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        for _, row in videos.iterrows():
            conn.execute(
                """
                INSERT INTO discoveries (
                    video_id, title, channel_title, thumbnail_url, published_at,
                    view_count, viral_score, views_per_day, keyword,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    view_count=excluded.view_count,
                    viral_score=excluded.viral_score,
                    views_per_day=excluded.views_per_day,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    row["video_id"],
                    row["title"],
                    row.get("channel_title"),
                    row.get("thumbnail_url"),
                    row.get("published_at"),
                    _int(row.get("view_count", 0)),
                    _float(row.get("viral_score")),
                    _float(row.get("views_per_day")),
                    keyword,
                    now,
                    now,
                ),
            )


def get_new_discoveries_count(on_date: str | None = None) -> int:
    """지정한 날짜(UTC, 기본값 오늘)에 처음 발견된 영상 수."""
    day = on_date or datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM discoveries WHERE date(first_seen_at) = ?",
            (day,),
        ).fetchone()
    return row["cnt"] if row else 0


def get_rising_videos(days: int = 7, limit: int = 8) -> pd.DataFrame:
    """최근 N일 이내 업로드된 영상 중 바이럴 점수가 높은 순."""
    with get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT * FROM discoveries
            WHERE date(published_at) >= date('now', ?)
            ORDER BY viral_score DESC
            LIMIT ?
            """,
            conn,
            params=(f"-{days} days", limit),
        )
    return df


def get_unreviewed_favorites_count() -> int:
    """태그도 메모도 없는(아직 정리/검토하지 않은) 즐겨찾기 수."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM favorites
            WHERE (tags IS NULL OR tags = '') AND (memo IS NULL OR memo = '')
            """
        ).fetchone()
    return row["cnt"] if row else 0


def insert_manual_entry(entry: dict) -> int:
    """수동 등록 항목을 저장하고 새로 생성된 id를 반환한다."""
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO manual_entries (
                source_url, account_name, platform, country, language, posted_at,
                view_count, like_count, comment_count, share_count, follower_count,
                image_paths, memo, tags, channel, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.get("source_url"),
                entry.get("account_name"),
                entry.get("platform"),
                entry.get("country"),
                entry.get("language"),
                entry.get("posted_at"),
                _int(entry.get("view_count")),
                _int(entry.get("like_count")),
                _int(entry.get("comment_count")),
                _int(entry.get("share_count")),
                _int(entry.get("follower_count", 0)),
                entry.get("image_paths", "[]"),
                entry.get("memo", ""),
                entry.get("tags", ""),
                entry.get("channel") or DEFAULT_CHANNEL,
                entry.get("status", "미검토"),
                entry["created_at"],
                entry["updated_at"],
            ),
        )
        entry_id = cursor.lastrowid
    return entry_id


def update_manual_entry(
    entry_id: int, tags: list[str], status: str, memo: str, channel: str = DEFAULT_CHANNEL
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE manual_entries
            SET tags = ?, status = ?, memo = ?, channel = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                ",".join(tags),
                status,
                memo,
                channel or DEFAULT_CHANNEL,
                datetime.now(timezone.utc).isoformat(),
                entry_id,
            ),
        )


def delete_manual_entry(entry_id: int) -> str | None:
    """항목을 삭제하고, 함께 정리해야 할 image_paths(JSON 문자열)를 반환한다."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT image_paths FROM manual_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        conn.execute("DELETE FROM manual_entries WHERE id = ?", (entry_id,))
    return row["image_paths"] if row else None


def get_all_manual_entries() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM manual_entries ORDER BY created_at DESC", conn
        )
    return df


def get_manual_entry(entry_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM manual_entries WHERE id = ?", (entry_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_ai_analysis(entry_id: int, analysis: dict) -> None:
    """AI 분석 결과를 저장한다(항목당 1건, 재분석 시 덮어씀)."""
    scores = analysis["scores"]
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ai_analyses (
                entry_id, extracted_texts, hook_type, hook_type_reason, adaptation_ideas,
                score_hidden_story, score_title_twist, score_product_new_view,
                score_conflict_resolution, score_visual_material, score_purchasable_link,
                score_fact_and_fun, total_score, is_recommended, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                extracted_texts=excluded.extracted_texts,
                hook_type=excluded.hook_type,
                hook_type_reason=excluded.hook_type_reason,
                adaptation_ideas=excluded.adaptation_ideas,
                score_hidden_story=excluded.score_hidden_story,
                score_title_twist=excluded.score_title_twist,
                score_product_new_view=excluded.score_product_new_view,
                score_conflict_resolution=excluded.score_conflict_resolution,
                score_visual_material=excluded.score_visual_material,
                score_purchasable_link=excluded.score_purchasable_link,
                score_fact_and_fun=excluded.score_fact_and_fun,
                total_score=excluded.total_score,
                is_recommended=excluded.is_recommended,
                analyzed_at=excluded.analyzed_at
            """,
            (
                entry_id,
                analysis["extracted_texts"],
                analysis["hook_type"],
                analysis["hook_type_reason"],
                analysis["adaptation_ideas"],
                _int(scores["hidden_story"]),
                _int(scores["title_twist"]),
                _int(scores["product_new_view"]),
                _int(scores["conflict_resolution"]),
                _int(scores["visual_material"]),
                _int(scores["purchasable_link"]),
                _int(scores["fact_and_fun"]),
                _int(analysis["total_score"]),
                int(bool(analysis["is_recommended"])),
                now,
            ),
        )


def get_ai_analysis(entry_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ai_analyses WHERE entry_id = ?", (entry_id,)
        ).fetchone()
    return dict(row) if row else None


def get_top_keywords(days: int = 30, limit: int = 8) -> list[tuple[str, int]]:
    """최근 N일간 발견 이력에 반복 등장한 검색 키워드 상위 목록."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT keyword, COUNT(*) AS cnt FROM discoveries
            WHERE date(first_seen_at) >= date('now', ?) AND keyword != ''
            GROUP BY keyword
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (f"-{days} days", limit),
        ).fetchall()
    return [(r["keyword"], r["cnt"]) for r in rows]




def insert_youtube_api_key(api_key: str) -> int:
    """YouTube API 키를 등록한다. 이미 등록된 키면 sqlite3.IntegrityError가 발생한다."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO youtube_api_keys (api_key, added_at) VALUES (?, ?)",
            (api_key, now),
        )
        return cursor.lastrowid


def delete_youtube_api_key(key_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM youtube_api_keys WHERE id = ?", (key_id,))


def get_youtube_api_keys() -> list[dict]:
    """등록 순서(id 오름차순)대로 모든 키를 반환한다."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM youtube_api_keys ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def touch_youtube_api_key_used(key_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE youtube_api_keys SET last_used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), key_id),
        )


def set_youtube_api_key_exhausted(key_id: int, on_date: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE youtube_api_keys SET exhausted_date = ? WHERE id = ?",
            (on_date, key_id),
        )


def replace_viral_videos(rows: list[dict]) -> None:
    """캐시된 바이럴 영상을 통째로 교체한다(새로고침마다 스냅샷을 다시 채움)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM viral_videos")
        conn.executemany(
            """
            INSERT INTO viral_videos (
                video_id, keyword, title, channel_id, channel_title,
                thumbnail_url, published_at, view_count, like_count, duration_seconds,
                is_short, subscriber_count, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["video_id"],
                    r.get("keyword"),
                    r.get("title"),
                    r.get("channel_id"),
                    r.get("channel_title"),
                    r.get("thumbnail_url"),
                    r.get("published_at"),
                    _int(r.get("view_count", 0)),
                    _int(r.get("like_count", 0)),
                    _int(r.get("duration_seconds", 0)),
                    int(bool(r.get("is_short", False))),
                    _int(r.get("subscriber_count", 0)),
                    now,
                )
                for r in rows
            ],
        )


def get_viral_videos() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM viral_videos", conn)
    return df


def get_viral_last_refreshed_at() -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_refreshed_at FROM viral_refresh_state WHERE id = 1"
        ).fetchone()
    return row["last_refreshed_at"] if row else None


def set_viral_last_refreshed_at(iso_str: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO viral_refresh_state (id, last_refreshed_at) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET last_refreshed_at = excluded.last_refreshed_at
            """,
            (iso_str,),
        )


def save_channel(growth: dict) -> None:
    """급성장 채널 판정 결과를 스냅샷으로 저장한다(채널당 최신 1건, 재저장 시 덮어씀)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO saved_channels (
                channel_id, channel_title, thumbnail_url, published_at, subscriber_count,
                video_count, months_active, label, outlier_ratio, shorts_ratio,
                avg_views_recent, median_views_recent, upload_count_recent, saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                channel_title=excluded.channel_title,
                thumbnail_url=excluded.thumbnail_url,
                published_at=excluded.published_at,
                subscriber_count=excluded.subscriber_count,
                video_count=excluded.video_count,
                months_active=excluded.months_active,
                label=excluded.label,
                outlier_ratio=excluded.outlier_ratio,
                shorts_ratio=excluded.shorts_ratio,
                avg_views_recent=excluded.avg_views_recent,
                median_views_recent=excluded.median_views_recent,
                upload_count_recent=excluded.upload_count_recent,
                saved_at=excluded.saved_at
            """,
            (
                growth["channel_id"],
                growth.get("channel_title"),
                growth.get("thumbnail_url"),
                growth.get("published_at"),
                _int(growth.get("subscriber_count", 0)),
                _int(growth.get("video_count", 0)),
                _float(growth.get("months_active")),
                growth.get("label"),
                _float(growth.get("outlier_ratio")),
                _float(growth.get("shorts_ratio")),
                _float(growth.get("avg_views_recent")),
                _float(growth.get("median_views_recent")),
                _int(growth.get("upload_count_recent", 0)),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def remove_saved_channel(channel_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM saved_channels WHERE channel_id = ?", (channel_id,))


def get_saved_channels() -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM saved_channels ORDER BY saved_at DESC", conn)
    return df


def is_channel_saved(channel_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM saved_channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row is not None


def insert_manual_entry_category(category: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO manual_entry_categories (category, created_at) VALUES (?, ?)",
            (category, datetime.now(timezone.utc).isoformat()),
        )


def get_manual_entry_categories() -> list[str]:
    """사용자가 직접 추가한 커스텀 카테고리만 등록 순서대로 반환한다(기본 20종 제외)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category FROM manual_entry_categories ORDER BY created_at ASC"
        ).fetchall()
    return [r["category"] for r in rows]
