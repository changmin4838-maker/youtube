from datetime import datetime, timezone

import pandas as pd

from src.config import VIRAL_SCORE_WEIGHTS

MIN_ELAPSED_DAYS = 1 / 24  # 업로드 직후(1시간 미만)에도 나눗셈이 폭주하지 않도록 최소값 고정


def _elapsed_days(published_at: str) -> float:
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - published
    return max(delta.total_seconds() / 86400, MIN_ELAPSED_DAYS)


def _minmax_normalize(series: pd.Series) -> pd.Series:
    """검색 결과 내 상대 순위를 0~100 스케일로 맞춘다(단일/동일값이면 100)."""
    min_v, max_v = series.min(), series.max()
    if pd.isna(min_v) or max_v - min_v < 1e-9:
        return pd.Series([100.0] * len(series), index=series.index)
    return (series - min_v) / (max_v - min_v) * 100


def compute_metrics(videos: list[dict], period_hours: int) -> pd.DataFrame:
    """원시 영상 데이터에 5가지 지표 + 종합 바이럴 점수를 계산해 붙인다."""
    if not videos:
        return pd.DataFrame()

    df = pd.DataFrame(videos)
    period_days = max(period_hours / 24, MIN_ELAPSED_DAYS)

    df["elapsed_days"] = df["published_at"].apply(_elapsed_days)

    # 1) 조회수 ÷ 구독자수
    df["views_per_sub"] = df.apply(
        lambda r: r["view_count"] / r["subscriber_count"]
        if r["subscriber_count"] > 0
        else 0.0,
        axis=1,
    )

    # 2) 채널 평균(최근 10개) 대비 배수
    df["channel_avg_multiple"] = df.apply(
        lambda r: r["view_count"] / r["channel_avg_views"]
        if r.get("channel_avg_views", 0) > 0
        else 0.0,
        axis=1,
    )

    # 3) 조회수 ÷ 게시후경과일 (일평균 조회속도)
    df["views_per_day"] = df["view_count"] / df["elapsed_days"]

    # 4) 좋아요율
    df["like_rate"] = df.apply(
        lambda r: (r["like_count"] / r["view_count"] * 100) if r["view_count"] > 0 else 0.0,
        axis=1,
    )

    # 5) 최신성: 선택한 기간 창(period) 안에서 최근일수록 1에 가까움
    df["freshness_raw"] = (1 - df["elapsed_days"] / period_days).clip(lower=0, upper=1)

    # 종합 바이럴 점수 = 각 지표를 결과 내 상대값(0~100)으로 정규화 후 가중합
    df["viral_score"] = (
        _minmax_normalize(df["channel_avg_multiple"]) * VIRAL_SCORE_WEIGHTS["channel_avg_multiple"]
        + _minmax_normalize(df["views_per_day"]) * VIRAL_SCORE_WEIGHTS["views_per_day"]
        + _minmax_normalize(df["views_per_sub"]) * VIRAL_SCORE_WEIGHTS["views_per_sub"]
        + _minmax_normalize(df["like_rate"]) * VIRAL_SCORE_WEIGHTS["like_rate"]
        + (df["freshness_raw"] * 100) * VIRAL_SCORE_WEIGHTS["freshness"]
    )

    return df
