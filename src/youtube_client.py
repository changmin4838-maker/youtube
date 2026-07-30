from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import streamlit as st
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src import youtube_api_keys as api_keys
from src.config import (
    GROWTH_MAX_CANDIDATES,
    GROWTH_RECENT_SAMPLE_SIZE,
    GROWTH_RECENT_UPLOAD_WINDOW_DAYS,
    MAX_SEARCH_RESULTS,
    SHORTS_MAX_SECONDS,
)

_DURATION_RE = re.compile(
    r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def parse_duration_seconds(duration: str) -> int:
    """ISO 8601 duration(예: PT4M13S)을 초 단위 정수로 변환한다."""
    match = _DURATION_RE.match(duration or "")
    if not match:
        return 0
    parts = match.groupdict()
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    return hours * 3600 + minutes * 60 + seconds


_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_CHANNEL_URL_ID_RE = re.compile(r"/channel/(UC[A-Za-z0-9_-]{22})")
_CHANNEL_USER_URL_RE = re.compile(r"/user/([A-Za-z0-9_-]+)")
_CHANNEL_HANDLE_URL_RE = re.compile(r"/@([A-Za-z0-9_.-]+)")
_CHANNEL_CUSTOM_URL_RE = re.compile(r"/c/([A-Za-z0-9_-]+)")


def _parse_channel_query(query: str) -> tuple[str, str]:
    """채널 ID/URL/핸들/사용자명을 구분해 (종류, 값)으로 반환한다.

    search.list 없이 channels.list만으로 채널을 찾아야 하므로, 정확한 채널 ID나
    핸들(@handle)이 아닌 애매한 표시 이름은 핸들 후보로 최선을 다해 시도만 한다.
    """
    q = query.strip()
    if _CHANNEL_ID_RE.match(q):
        return "id", q
    m = _CHANNEL_URL_ID_RE.search(q)
    if m:
        return "id", m.group(1)
    m = _CHANNEL_USER_URL_RE.search(q)
    if m:
        return "username", m.group(1)
    m = _CHANNEL_HANDLE_URL_RE.search(q)
    if m:
        return "handle", m.group(1)
    m = _CHANNEL_CUSTOM_URL_RE.search(q)
    if m:
        return "handle", m.group(1)
    if q.startswith("@"):
        return "handle", q[1:]
    return "handle", q.replace(" ", "")


@st.cache_resource(show_spinner=False)
def _build_client(api_key: str):
    return build("youtube", "v3", developerKey=api_key)


def _is_quota_error(e: HttpError) -> bool:
    status = getattr(e.resp, "status", None)
    if status == 429:
        return True
    if status == 403:
        content = (e.content or b"").decode("utf-8", errors="ignore").lower()
        return any(kw in content for kw in ("quotaexceeded", "dailylimitexceeded", "ratelimitexceeded"))
    return False


def execute_with_rotation(request_fn):
    """request_fn(youtube_client) -> dict 형태의 API 호출을 키 자동 전환과 함께 실행한다.

    429(쿼터 초과) 발생 시 해당 키를 오늘 날짜로 소진 처리하고, 등록된 다음 키로 즉시
    재시도한다. 등록된 키가 없거나 오늘 모든 키가 소진됐으면 RuntimeError를 발생시킨다.
    """
    if not api_keys.has_any_key():
        raise RuntimeError(
            "YouTube API 키가 등록되지 않았습니다. 사이드바의 'YouTube API 키 관리'에서 키를 추가하세요."
        )

    tried_ids: set[int] = set()
    while True:
        key_row = api_keys.get_next_available_key(exclude_ids=tried_ids)
        if key_row is None:
            raise RuntimeError(
                "오늘 등록된 모든 키의 쿼터가 소진되었습니다. 새 키를 추가하거나 내일 다시 시도해주세요."
            )
        try:
            client = _build_client(key_row["api_key"])
            result = request_fn(client)
            api_keys.mark_key_used(key_row["id"])
            return result
        except HttpError as e:
            if _is_quota_error(e):
                api_keys.mark_key_exhausted(key_row["id"])
                tried_ids.add(key_row["id"])
                continue
            raise


@st.cache_data(ttl=1800, show_spinner=False)
def search_video_ids(keyword: str, period_hours: int, video_format: str) -> list[str]:
    """키워드 + 업로드 기간 + 형식 조건으로 영상 ID 목록을 검색한다."""
    published_after = (
        datetime.now(timezone.utc) - timedelta(hours=period_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = dict(
        q=keyword,
        part="id",
        type="video",
        order="viewCount",
        publishedAfter=published_after,
        maxResults=MAX_SEARCH_RESULTS,
    )
    if video_format == "쇼츠":
        # API 자체엔 "쇼츠" 필터가 없어 4분 미만으로 1차 필터링 후,
        # 상세 조회 단계에서 60초 이하 여부로 정확히 재판정한다.
        params["videoDuration"] = "short"

    response = execute_with_rotation(lambda youtube: youtube.search().list(**params).execute())
    return [item["id"]["videoId"] for item in response.get("items", [])]


@st.cache_data(ttl=1800, show_spinner=False)
def get_videos_details(video_ids: tuple[str, ...]) -> dict[str, dict]:
    """영상 ID들의 제목/채널/조회수/좋아요수/길이 등 상세 정보를 조회한다."""
    if not video_ids:
        return {}
    details: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        response = execute_with_rotation(
            lambda youtube: youtube.videos()
            .list(part="snippet,statistics,contentDetails", id=",".join(chunk))
            .execute()
        )
        for item in response.get("items", []):
            snippet = item["snippet"]
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            duration_seconds = parse_duration_seconds(content.get("duration", ""))
            thumbnails = snippet.get("thumbnails", {})
            thumbnail_url = (
                thumbnails.get("high")
                or thumbnails.get("medium")
                or thumbnails.get("default")
                or {}
            ).get("url", "")
            details[item["id"]] = {
                "video_id": item["id"],
                "title": snippet.get("title", ""),
                "channel_id": snippet.get("channelId", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "thumbnail_url": thumbnail_url,
                "published_at": snippet.get("publishedAt", ""),
                "view_count": int(stats.get("viewCount", 0)),
                "like_count": int(stats.get("likeCount", 0)),
                "duration_seconds": duration_seconds,
                "is_short": duration_seconds > 0 and duration_seconds <= SHORTS_MAX_SECONDS,
            }
    return details


@st.cache_data(ttl=1800, show_spinner=False)
def get_channels_stats(channel_ids: tuple[str, ...]) -> dict[str, int]:
    """채널 ID들의 구독자 수를 조회한다. 비공개 구독자수는 0으로 처리한다."""
    if not channel_ids:
        return {}
    stats: dict[str, int] = {}
    unique_ids = list(dict.fromkeys(channel_ids))
    for i in range(0, len(unique_ids), 50):
        chunk = unique_ids[i : i + 50]
        response = execute_with_rotation(
            lambda youtube: youtube.channels().list(part="statistics", id=",".join(chunk)).execute()
        )
        for item in response.get("items", []):
            sub_count = item.get("statistics", {}).get("subscriberCount")
            stats[item["id"]] = int(sub_count) if sub_count is not None else 0
    return stats


@st.cache_data(ttl=3600, show_spinner=False)
def get_channel_recent_avg_views(channel_id: str, sample_size: int = 10) -> float:
    """해당 채널의 최근 업로드 영상 N개 평균 조회수를 계산한다."""
    if not channel_id:
        return 0.0
    search_response = execute_with_rotation(
        lambda youtube: youtube.search()
        .list(
            channelId=channel_id,
            part="id",
            type="video",
            order="date",
            maxResults=sample_size,
        )
        .execute()
    )
    ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
    if not ids:
        return 0.0
    stats_response = execute_with_rotation(
        lambda youtube: youtube.videos().list(part="statistics", id=",".join(ids)).execute()
    )
    views = [
        int(item.get("statistics", {}).get("viewCount", 0))
        for item in stats_response.get("items", [])
    ]
    return sum(views) / len(views) if views else 0.0


def fetch_search_results(
    keyword: str,
    period_hours: int,
    video_format: str,
    min_views: int,
    sub_min: int,
    sub_max: int,
) -> list[dict]:
    """검색 -> 상세조회 -> 필터(형식/조회수/구독자) -> 채널 평균조회수 순으로 결과를 만든다."""
    video_ids = search_video_ids(keyword, period_hours, video_format)
    details = get_videos_details(tuple(video_ids))
    videos = list(details.values())

    if video_format == "쇼츠":
        videos = [v for v in videos if v["is_short"]]

    if min_views:
        videos = [v for v in videos if v["view_count"] >= min_views]

    channel_ids = tuple({v["channel_id"] for v in videos if v["channel_id"]})
    sub_counts = get_channels_stats(channel_ids)
    for v in videos:
        v["subscriber_count"] = sub_counts.get(v["channel_id"], 0)

    if sub_min:
        videos = [v for v in videos if v["subscriber_count"] >= sub_min]
    if sub_max:
        videos = [v for v in videos if v["subscriber_count"] <= sub_max]

    for v in videos:
        v["channel_avg_views"] = get_channel_recent_avg_views(v["channel_id"])

    return videos


# ---------------------------------------------------------------------------
# 급성장 채널 탭에서 쓰는 채널 검색/분석 API
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner=False)
def search_channel_ids(keyword: str, max_results: int = GROWTH_MAX_CANDIDATES) -> list[str]:
    """키워드로 채널을 검색해 채널 ID 목록을 반환한다."""
    response = execute_with_rotation(
        lambda youtube: youtube.search()
        .list(q=keyword, part="id", type="channel", maxResults=max_results)
        .execute()
    )
    return [item["id"]["channelId"] for item in response.get("items", [])]


@st.cache_data(ttl=1800, show_spinner=False)
def get_channels_metadata(channel_ids: tuple[str, ...]) -> dict[str, dict]:
    """채널 ID들의 개설일/구독자수/총 영상수 등 메타데이터를 조회한다."""
    if not channel_ids:
        return {}
    metadata: dict[str, dict] = {}
    unique_ids = list(dict.fromkeys(channel_ids))
    for i in range(0, len(unique_ids), 50):
        chunk = unique_ids[i : i + 50]
        response = execute_with_rotation(
            lambda youtube: youtube.channels()
            .list(part="snippet,statistics", id=",".join(chunk))
            .execute()
        )
        for item in response.get("items", []):
            snippet = item["snippet"]
            stats = item.get("statistics", {})
            thumbnails = snippet.get("thumbnails", {})
            thumbnail_url = (
                thumbnails.get("high")
                or thumbnails.get("medium")
                or thumbnails.get("default")
                or {}
            ).get("url", "")
            sub_count = stats.get("subscriberCount")
            metadata[item["id"]] = {
                "channel_id": item["id"],
                "channel_title": snippet.get("title", ""),
                "thumbnail_url": thumbnail_url,
                "published_at": snippet.get("publishedAt", ""),
                "subscriber_count": int(sub_count) if sub_count is not None else 0,
                "video_count": int(stats.get("videoCount", 0)),
            }
    return metadata


@st.cache_data(ttl=1800, show_spinner=False)
def get_channel_recent_video_stats(
    channel_id: str,
    sample_size: int = GROWTH_RECENT_SAMPLE_SIZE,
    recent_window_days: int = GROWTH_RECENT_UPLOAD_WINDOW_DAYS,
) -> dict:
    """채널의 최근 업로드(최대 50개)를 조회해 최근 N개 상세와 최근 기간 업로드 수를 반환한다.

    최근 기간 업로드 수는 최근 50개 중에서 집계하므로, 한 채널이 그 기간에
    50개 넘게 올린 경우에는 과소 집계될 수 있다(개인용 도구 수준에서는 드문 케이스).
    """
    search_response = execute_with_rotation(
        lambda youtube: youtube.search()
        .list(channelId=channel_id, part="id", type="video", order="date", maxResults=50)
        .execute()
    )
    ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
    if not ids:
        return {"videos": [], "upload_count_recent": 0}

    videos: list[dict] = []
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        response = execute_with_rotation(
            lambda youtube: youtube.videos()
            .list(part="snippet,statistics,contentDetails", id=",".join(chunk))
            .execute()
        )
        for item in response.get("items", []):
            snippet = item["snippet"]
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            duration_seconds = parse_duration_seconds(content.get("duration", ""))
            videos.append(
                {
                    "video_id": item["id"],
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "view_count": int(stats.get("viewCount", 0)),
                    "duration_seconds": duration_seconds,
                    "is_short": duration_seconds > 0 and duration_seconds <= SHORTS_MAX_SECONDS,
                }
            )

    videos.sort(key=lambda v: v["published_at"], reverse=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_window_days)
    upload_count_recent = sum(
        1
        for v in videos
        if datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) >= cutoff
    )

    return {"videos": videos[:sample_size], "upload_count_recent": upload_count_recent}


# ---------------------------------------------------------------------------
# 급성장 채널 탭 "채널 직접 검색" 섹션 — search.list를 쓰지 않고
# channels.list + playlistItems.list + videos.list만으로 채널 1개를 조회한다.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_channel_by_query(query: str) -> dict | None:
    """채널 ID/URL/핸들/사용자명으로 채널 하나를 직접 조회한다(search.list 미사용).

    channels.list 1회 호출(1 unit)로 끝나는 게 보통이고, 입력이 정확한 핸들/ID/URL이
    아니면 최선을 다해 핸들로도 한 번 더 시도한다(최대 2회 = 2 units).
    """
    kind, value = _parse_channel_query(query)

    param_variants: list[dict[str, str]] = []
    if kind == "id":
        param_variants.append({"id": value})
    elif kind == "username":
        param_variants.append({"forUsername": value})
        param_variants.append({"forHandle": value if value.startswith("@") else f"@{value}"})
    else:
        param_variants.append({"forHandle": value if value.startswith("@") else f"@{value}"})

    for params in param_variants:
        response = execute_with_rotation(
            lambda youtube: youtube.channels()
            .list(part="snippet,statistics,contentDetails", **params)
            .execute()
        )
        items = response.get("items", [])
        if not items:
            continue
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        content = item.get("contentDetails", {})
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (
            thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}
        ).get("url", "")
        sub_count = stats.get("subscriberCount")
        return {
            "channel_id": item["id"],
            "channel_title": snippet.get("title", ""),
            "thumbnail_url": thumbnail_url,
            "published_at": snippet.get("publishedAt", ""),
            "subscriber_count": int(sub_count) if sub_count is not None else 0,
            "video_count": int(stats.get("videoCount", 0)),
            "uploads_playlist_id": content.get("relatedPlaylists", {}).get("uploads", ""),
        }
    return None


@st.cache_data(ttl=1800, show_spinner=False)
def get_channel_recent_video_stats_via_playlist(
    uploads_playlist_id: str,
    sample_size: int = GROWTH_RECENT_SAMPLE_SIZE,
    recent_window_days: int = GROWTH_RECENT_UPLOAD_WINDOW_DAYS,
) -> dict:
    """업로드 재생목록(playlistItems.list)에서 최근 업로드를 가져온다.

    채널당 channels.list(1) + playlistItems.list(1) + videos.list(1) = 총 3 units로,
    기존 급성장 채널 탐색(get_channel_recent_video_stats, search.list 기반 채널당 약 101 units)보다
    훨씬 저렴하다. 반환 형태는 get_channel_recent_video_stats와 동일하게 맞춰 판정 로직을 그대로 재사용한다.
    """
    if not uploads_playlist_id:
        return {"videos": [], "upload_count_recent": 0}

    playlist_response = execute_with_rotation(
        lambda youtube: youtube.playlistItems()
        .list(part="contentDetails", playlistId=uploads_playlist_id, maxResults=50)
        .execute()
    )
    ids = [
        it["contentDetails"]["videoId"]
        for it in playlist_response.get("items", [])
        if it.get("contentDetails", {}).get("videoId")
    ]
    if not ids:
        return {"videos": [], "upload_count_recent": 0}

    videos: list[dict] = []
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        response = execute_with_rotation(
            lambda youtube: youtube.videos()
            .list(part="snippet,statistics,contentDetails", id=",".join(chunk))
            .execute()
        )
        for item in response.get("items", []):
            snippet = item["snippet"]
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            duration_seconds = parse_duration_seconds(content.get("duration", ""))
            videos.append(
                {
                    "video_id": item["id"],
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "view_count": int(stats.get("viewCount", 0)),
                    "duration_seconds": duration_seconds,
                    "is_short": duration_seconds > 0 and duration_seconds <= SHORTS_MAX_SECONDS,
                }
            )

    videos.sort(key=lambda v: v["published_at"], reverse=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_window_days)
    upload_count_recent = sum(
        1
        for v in videos
        if datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) >= cutoff
    )

    return {"videos": videos[:sample_size], "upload_count_recent": upload_count_recent}
