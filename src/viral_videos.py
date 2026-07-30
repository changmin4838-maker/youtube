from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from src import database as db
from src.config import (
    SHORTS_MAX_SECONDS,
    VIRAL_EXCLUDED_CATEGORY_IDS,
    VIRAL_FRESHNESS_WINDOW_HOURS,
    VIRAL_REFRESH_INTERVAL_HOURS,
    VIRAL_SHOPPING_KEYWORDS,
)
from src.metrics import compute_metrics
from src.youtube_client import execute_with_rotation, get_channels_stats, parse_duration_seconds, search_video_ids


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_keyword_videos_raw(keyword: str) -> list[dict]:
    """쇼핑 키워드 1개로 search.list(쇼츠 필터, 1회 100 units) + videos.list(상세조회)를 실행한다.

    기존 검색 탭이 쓰는 search_video_ids를 그대로 재사용해 검색 단계를 만들고, 상세조회는
    이 탭 전용으로 직접 호출한다(뉴스/정치 categoryId 제외 판정에 필요한 snippet.categoryId를
    기존 검색 탭의 get_videos_details가 반환하지 않아, 그 함수를 건드리지 않기 위해 분리했다).
    """
    video_ids = search_video_ids(keyword, VIRAL_FRESHNESS_WINDOW_HOURS, "쇼츠")
    if not video_ids:
        return []

    videos: list[dict] = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        response = execute_with_rotation(
            lambda youtube: youtube.videos()
            .list(part="snippet,statistics,contentDetails", id=",".join(chunk))
            .execute()
        )
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            if snippet.get("categoryId") in VIRAL_EXCLUDED_CATEGORY_IDS:
                continue  # 뉴스/정치는 쇼핑 키워드 검색 결과에 섞여 들어와도 완전히 제외한다.

            content = item.get("contentDetails", {})
            duration_seconds = parse_duration_seconds(content.get("duration", ""))
            if not (0 < duration_seconds <= SHORTS_MAX_SECONDS):
                # search.list의 videoDuration=short는 4분 미만까지 허용하므로 60초 기준으로 다시 정확히 거른다.
                continue

            stats = item.get("statistics", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail_url = (
                thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
            ).get("url", "")
            videos.append(
                {
                    "video_id": item["id"],
                    "title": snippet.get("title", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "channel_title": snippet.get("channelTitle", ""),
                    "thumbnail_url": thumbnail_url,
                    "published_at": snippet.get("publishedAt", ""),
                    "view_count": int(stats.get("viewCount", 0)),
                    "like_count": int(stats.get("likeCount", 0)),
                    "duration_seconds": duration_seconds,
                    "is_short": True,
                }
            )
    return videos


def _gather_all_keywords() -> list[dict]:
    """등록된 모든 쇼핑 키워드를 순회해 쇼츠 후보를 모으고, 구독자수를 배치 조회해 붙인다."""
    by_video_id: dict[str, dict] = {}
    for keyword in VIRAL_SHOPPING_KEYWORDS:
        for video in _fetch_keyword_videos_raw(keyword):
            if video["video_id"] in by_video_id:
                continue  # 여러 키워드에 동시에 걸리는 영상은 먼저 만난 키워드로 귀속시킨다.
            video["keyword"] = keyword
            by_video_id[video["video_id"]] = video

    videos = list(by_video_id.values())
    channel_ids = tuple({v["channel_id"] for v in videos if v["channel_id"]})
    sub_counts = get_channels_stats(channel_ids)
    for v in videos:
        v["subscriber_count"] = sub_counts.get(v["channel_id"], 0)
    return videos


def needs_refresh(interval_hours: float = VIRAL_REFRESH_INTERVAL_HOURS) -> bool:
    last = db.get_viral_last_refreshed_at()
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=interval_hours)


def refresh_viral_videos(force: bool = False) -> bool:
    """조건을 만족하면(기본: 마지막 갱신 후 12시간 경과) 새로 가져와 캐시를 통째로 교체한다.

    반환값은 실제로 API를 호출했는지 여부다(호출하지 않았으면 False).
    """
    if not force and not needs_refresh():
        return False
    videos = _gather_all_keywords()
    db.replace_viral_videos(videos)
    db.set_viral_last_refreshed_at(datetime.now(timezone.utc).isoformat())
    return True


def load_viral_videos_df() -> pd.DataFrame:
    """캐시된 원본 영상에 바이럴 점수 등 지표를 계산해 붙여 반환한다(API 호출 없음).

    채널별 최근 평균 조회수(channel_avg_views)는 채널당 추가 호출이 필요해 비용이 커서
    이 탭에서는 수집하지 않는다. compute_metrics는 해당 값이 없으면 채널평균배수 지표를
    전 종목 동일값(중립)으로 처리하므로, 나머지 4개 지표만으로 상대 순위가 매겨진다.
    """
    raw_df = db.get_viral_videos()
    if raw_df.empty:
        return raw_df
    return compute_metrics(raw_df.to_dict("records"), VIRAL_FRESHNESS_WINDOW_HOURS)


def format_relative_time(iso_str: str | None) -> str:
    """마지막 갱신 시각을 "N시간 전" 같은 상대 표현으로 변환한다."""
    if not iso_str:
        return "아직 갱신 안 됨"
    dt = datetime.fromisoformat(iso_str)
    minutes = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if minutes < 1:
        return "방금 전"
    if minutes < 60:
        return f"{minutes}분 전"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 전"
    return f"{hours // 24}일 전"
