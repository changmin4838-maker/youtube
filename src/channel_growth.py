from __future__ import annotations

from datetime import datetime, timezone
from statistics import median

from src.config import (
    GROWTH_CONSECUTIVE_VIRAL_RATIO,
    GROWTH_FALLBACK_LABEL,
    GROWTH_FEW_VIDEOS_MAX_COUNT,
    GROWTH_FEW_VIDEOS_MIN_SUBS,
    GROWTH_MAX_CANDIDATES,
    GROWTH_NEW_CHANNEL_MIN_OUTLIER_RATIO,
    GROWTH_NEW_CHANNEL_MONTHS,
    GROWTH_OUTLIER_MULTIPLIER,
    GROWTH_SHORTS_DOMINANT_RATIO,
)
from src.youtube_client import (
    fetch_channel_by_query,
    get_channel_recent_video_stats_via_playlist,
    get_channels_metadata,
    search_channel_ids,
)

MIN_MONTHS_ACTIVE = 1 / 30  # 개설 당일에도 나눗셈이 폭주하지 않도록 최소값 고정


def _months_active(published_at: str) -> float:
    created = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    delta_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    return max(delta_days / 30.44, MIN_MONTHS_ACTIVE)


def _classify_label(
    months_active: float,
    outlier_ratio: float,
    video_count: int,
    subscriber_count: int,
    shorts_ratio: float,
) -> str:
    """아래 순서대로 조건을 검사해 첫 매치를 라벨로 채택한다."""
    if (
        months_active <= GROWTH_NEW_CHANNEL_MONTHS
        and outlier_ratio >= GROWTH_NEW_CHANNEL_MIN_OUTLIER_RATIO
    ):
        return "신규 급성장"
    if outlier_ratio >= GROWTH_CONSECUTIVE_VIRAL_RATIO:
        return "연속 바이럴"
    if video_count <= GROWTH_FEW_VIDEOS_MAX_COUNT and subscriber_count >= GROWTH_FEW_VIDEOS_MIN_SUBS:
        return "소수 영상 고효율"
    if shorts_ratio >= GROWTH_SHORTS_DOMINANT_RATIO:
        return "쇼츠 강세"
    return GROWTH_FALLBACK_LABEL


def compute_channel_growth(meta: dict, activity: dict) -> dict | None:
    """채널 메타데이터 + 최근 업로드 활동으로 급성장 지표와 추천 라벨을 계산한다."""
    videos = activity["videos"]
    if not videos:
        return None

    views = [v["view_count"] for v in videos]
    avg_views = sum(views) / len(views)
    median_views = median(views)
    threshold = median_views * GROWTH_OUTLIER_MULTIPLIER
    outlier_count = sum(1 for v in views if threshold > 0 and v >= threshold)
    outlier_ratio = outlier_count / len(videos)

    shorts_count = sum(1 for v in videos if v["is_short"])
    shorts_ratio = shorts_count / len(videos)

    months_active = _months_active(meta["published_at"])
    subs_per_month = meta["subscriber_count"] / months_active

    label = _classify_label(
        months_active=months_active,
        outlier_ratio=outlier_ratio,
        video_count=meta["video_count"],
        subscriber_count=meta["subscriber_count"],
        shorts_ratio=shorts_ratio,
    )

    return {
        **meta,
        "months_active": months_active,
        "subs_per_month": subs_per_month,
        "avg_views_recent": avg_views,
        "median_views_recent": median_views,
        "outlier_count": outlier_count,
        "outlier_ratio": outlier_ratio,
        "shorts_ratio": shorts_ratio,
        "upload_count_recent": activity["upload_count_recent"],
        "label": label,
    }


def fetch_growing_channels(
    keyword: str,
    months_min: int,
    months_max: int,
    sub_min: int,
    sub_max: int,
    min_uploads_recent: int,
    min_shorts_ratio: float,
    max_candidates: int = GROWTH_MAX_CANDIDATES,
) -> list[dict]:
    """채널 검색 -> 메타데이터 필터 -> 최근 업로드 분석 -> 추가 필터 순으로 급성장 채널을 찾는다.

    최근 업로드 조회는 channels.list에서 이미 받아온 업로드 재생목록 ID로
    playlistItems.list(채널당 약 2~3 units)를 쓰므로, 여전히 저비용인 메타데이터
    (운영 개월/구독자) 필터를 먼저 적용해 불필요한 후보를 줄인다.
    """
    channel_ids = search_channel_ids(keyword, max_results=max_candidates)
    metadata = get_channels_metadata(tuple(channel_ids))

    candidates = []
    for meta in metadata.values():
        months_active = _months_active(meta["published_at"])
        if months_min and months_active < months_min:
            continue
        if months_max and months_active > months_max:
            continue
        if sub_min and meta["subscriber_count"] < sub_min:
            continue
        if sub_max and meta["subscriber_count"] > sub_max:
            continue
        candidates.append(meta)

    results = []
    for meta in candidates:
        activity = get_channel_recent_video_stats_via_playlist(meta.get("uploads_playlist_id", ""))
        growth = compute_channel_growth(meta, activity)
        if growth is None:
            continue
        if min_uploads_recent and growth["upload_count_recent"] < min_uploads_recent:
            continue
        if min_shorts_ratio and growth["shorts_ratio"] * 100 < min_shorts_ratio:
            continue
        results.append(growth)

    return results


def fetch_channel_by_direct_query(query: str) -> dict | None:
    """채널명(핸들)/URL/채널 ID로 채널 1개를 직접 조회해 급성장 지표+라벨을 계산한다.

    카테고리 키워드 탐색(fetch_growing_channels)과 달리 search.list를 전혀 쓰지 않고
    channels.list + playlistItems.list + videos.list만으로 동작해 훨씬 저렴하다.
    """
    meta = fetch_channel_by_query(query)
    if meta is None:
        return None
    activity = get_channel_recent_video_stats_via_playlist(meta.get("uploads_playlist_id", ""))
    return compute_channel_growth(meta, activity)
