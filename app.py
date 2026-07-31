from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from googleapiclient.errors import HttpError

from google.genai import errors as genai_errors

from src import ai_analysis
from src import database as db
from src import manual_entries
from src import viral_videos
from src import youtube_api_keys as api_keys
from src.channel_growth import fetch_channel_by_direct_query, fetch_growing_channels
from src.config import (
    APP_SUBTITLE,
    APP_TITLE,
    AVAILABLE_TAGS,
    DEFAULT_CHANNEL,
    GIFT_SHOP_CRITERIA,
    GIFT_SHOP_MAX_SCORE,
    GROWTH_MAX_CANDIDATES,
    KEYWORD_TREND_DAYS,
    KEYWORD_TREND_LIMIT,
    MANUAL_ENTRY_IMAGE_TYPES,
    PERIOD_OPTIONS,
    PLATFORM_OPTIONS,
    PRODUCTION_STATUS_OPTIONS,
    RISING_VIDEOS_LIMIT,
    RISING_WINDOW_DAYS,
    SORT_OPTIONS,
    TAB_HELP,
    TAB_LABELS,
    VIDEO_FORMAT_OPTIONS,
    VIRAL_DEFAULT_MIN_VIEWS,
    VIRAL_DEFAULT_PERIOD_LABEL,
    VIRAL_DEFAULT_SORT_LABEL,
    VIRAL_DEFAULT_SUB_MAX,
    VIRAL_DEFAULT_SUB_MIN,
    VIRAL_REFRESH_INTERVAL_HOURS,
    VIRAL_REFRESH_QUOTA_ESTIMATE,
    VIRAL_SHOPPING_KEYWORDS,
)
from src.metrics import compute_metrics
from src.youtube_client import fetch_search_results

st.set_page_config(page_title="요잇템 레이더", page_icon="🛍️", layout="wide")
db.init_db()
api_keys.ensure_default_key_migrated()

KST = timezone(timedelta(hours=9))


def mark_data_updated() -> None:
    """검색/데이터 갱신 작업이 실행될 때마다 현재 KST 시각을 마지막 업데이트로 기록한다."""
    db.set_last_update_at(datetime.now(KST).strftime("%Y-%m-%d %H:%M"))


st.title(APP_TITLE)
st.caption(APP_SUBTITLE)
_last_update_at = db.get_last_update_at()
st.caption(f"🕒 마지막 업데이트: {_last_update_at}" if _last_update_at else "🕒 업데이트 기록 없음")
st.divider()


def format_number(value) -> str:
    try:
        return f"{int(round(value)):,}"
    except (TypeError, ValueError):
        return "-"


def format_date(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# 사이드바: 메뉴 목차 + 검색 필터
# ---------------------------------------------------------------------------
with st.sidebar:
    with st.expander("🔑 YouTube API 키 관리", expanded=False):
        new_key_input = st.text_input(
            "새 API 키", type="password", key="new_youtube_key_input"
        )
        if st.button("➕ 키 추가", key="add_youtube_key_btn", use_container_width=True):
            try:
                api_keys.add_key(new_key_input)
                st.toast("키를 추가했습니다.", icon="✅")
                st.rerun()
            except ValueError as e:
                st.warning(str(e))

        key_rows = api_keys.list_keys_with_status()
        if not key_rows:
            st.caption("등록된 키가 없습니다.")
        else:
            status_icons = {"정상": "🟢", "오늘 쿼터 소진": "🔴", "미확인": "⚪"}
            for row in key_rows:
                kc1, kc2 = st.columns([5, 1])
                icon = status_icons.get(row["status"], "⚪")
                kc1.markdown(f"{icon} 키 {row['position']} `{row['masked']}`  \n{row['status']}")
                if kc2.button("🗑", key=f"del_youtube_key_{row['id']}", help="이 키 삭제"):
                    api_keys.remove_key(row["id"])
                    st.toast("키를 삭제했습니다.", icon="🗑")
                    st.rerun()

    current_key = api_keys.get_current_key_position()
    if current_key:
        pos, total = current_key
        st.caption(f"🔑 현재 {pos}번째 키 사용 중 (전체 {total}개)")
    elif api_keys.has_any_key():
        st.caption(f"🔑 등록된 키 {len(api_keys.list_keys_with_status())}개 (아직 사용 안 함)")
    else:
        st.error("등록된 YouTube API 키가 없습니다. 위에서 키를 추가하세요.")

    st.divider()

    st.header("🔍 검색")

    keyword = st.text_input("검색 키워드", placeholder="예: 여행 브이로그")

    with st.expander("상세 필터", expanded=False):
        period_label = st.selectbox("업로드 기간", list(PERIOD_OPTIONS.keys()), index=2)
        min_views = st.number_input("최소 조회수", min_value=0, value=0, step=1000)

        st.caption("구독자 범위 (0 = 제한 없음)")
        col_sub1, col_sub2 = st.columns(2)
        sub_min = col_sub1.number_input("최소", min_value=0, value=0, step=1000, key="sub_min")
        sub_max = col_sub2.number_input("최대", min_value=0, value=0, step=1000, key="sub_max")

        video_format = st.radio("영상 형식", VIDEO_FORMAT_OPTIONS, horizontal=True)
        sort_label = st.selectbox("정렬", list(SORT_OPTIONS.keys()))

    search_clicked = st.button("🔍 검색", type="primary", use_container_width=True)

if search_clicked:
    if not keyword.strip():
        st.sidebar.warning("검색 키워드를 입력하세요.")
    else:
        with st.spinner("YouTube에서 검색 중..."):
            try:
                videos = fetch_search_results(
                    keyword=keyword.strip(),
                    period_hours=PERIOD_OPTIONS[period_label],
                    video_format=video_format,
                    min_views=min_views,
                    sub_min=sub_min,
                    sub_max=sub_max,
                )
                df = compute_metrics(videos, PERIOD_OPTIONS[period_label])
                st.session_state["results_df"] = df
                st.session_state["last_keyword"] = keyword.strip()
                db.log_discoveries(df, keyword.strip())
                mark_data_updated()
            except RuntimeError as e:
                st.error(str(e))
            except HttpError as e:
                st.error(f"YouTube API 오류가 발생했습니다: {e}")


# ---------------------------------------------------------------------------
# 결과 카드 렌더링 (검색 탭에서 사용)
# ---------------------------------------------------------------------------
def render_result_row(row: pd.Series, key_prefix: str = "search") -> None:
    """검색 결과 카드. key_prefix로 검색 탭/즐겨찾기 탭에서 재사용해도 위젯 key가 겹치지 않게 한다."""
    video_id = row["video_id"]
    existing = db.get_favorite(video_id)
    is_fav = existing is not None
    default_tags = existing["tags"].split(",") if is_fav and existing["tags"] else []
    default_memo = existing["memo"] if is_fav else ""

    with st.container(border=True):
        col_thumb, col_info, col_metrics = st.columns([1.1, 2.3, 2.2])

        with col_thumb:
            if row.get("thumbnail_url"):
                st.image(row["thumbnail_url"], use_container_width=True)
            st.link_button(
                "▶ YouTube에서 보기",
                f"https://www.youtube.com/watch?v={video_id}",
                use_container_width=True,
                key=f"{key_prefix}_watch_{video_id}",
            )

        with col_info:
            star = "⭐ " if is_fav else ""
            st.markdown(f"**{star}{row['title']}**")
            st.caption(row["channel_title"])
            st.write(f"구독자 {format_number(row['subscriber_count'])}명")
            st.write(f"조회수 {format_number(row['view_count'])}회")
            st.write(f"업로드일 {format_date(row['published_at'])}")
            st.write("형식: " + ("쇼츠" if row.get("is_short") else "일반"))

        with col_metrics:
            st.metric("종합 바이럴 점수", f"{row['viral_score']:.1f}")
            st.write(f"채널평균 대비 배수: **{row['channel_avg_multiple']:.2f}배**")
            st.write(f"일평균 조회속도: **{format_number(row['views_per_day'])}회/일**")
            st.write(f"구독자대비 조회수: **{row['views_per_sub']:.2f}**")
            st.write(f"좋아요율: **{row['like_rate']:.2f}%**")

        with st.form(key=f"{key_prefix}_fav_form_{video_id}"):
            tags = st.multiselect(
                "태그", AVAILABLE_TAGS, default=default_tags, key=f"{key_prefix}_tags_{video_id}"
            )
            memo = st.text_area(
                "메모", value=default_memo, height=68, key=f"{key_prefix}_memo_{video_id}"
            )
            c1, c2 = st.columns(2)
            save_clicked = c1.form_submit_button(
                "⭐ 즐겨찾기 저장", use_container_width=True
            )
            remove_clicked = c2.form_submit_button(
                "🗑 즐겨찾기 해제", use_container_width=True, disabled=not is_fav
            )

        if save_clicked:
            db.save_favorite(row.to_dict(), tags, memo, DEFAULT_CHANNEL)
            st.toast("즐겨찾기에 저장했습니다.", icon="⭐")
            st.rerun()
        if remove_clicked:
            db.remove_favorite(video_id)
            st.toast("즐겨찾기에서 제거했습니다.", icon="🗑")
            st.rerun()


# ---------------------------------------------------------------------------
# 급성장 채널 카드 렌더링 (급성장 채널 탭에서 사용)
# ---------------------------------------------------------------------------
def render_growth_channel_row(g: dict) -> None:
    with st.container(border=True):
        col_thumb, col_info, col_metrics = st.columns([1.1, 2.3, 2.2])

        with col_thumb:
            if g.get("thumbnail_url"):
                st.image(g["thumbnail_url"], use_container_width=True)
            st.link_button(
                "▶ 채널 보기",
                f"https://www.youtube.com/channel/{g['channel_id']}",
                use_container_width=True,
            )

        with col_info:
            st.markdown(f"**{g['channel_title']}**")
            st.write(f"구독자 {format_number(g['subscriber_count'])}명")
            st.write(f"운영 {g['months_active']:.1f}개월")
            st.write(f"총 영상 {format_number(g['video_count'])}개")
            st.write(f"최근 30일 업로드 {g['upload_count_recent']}개")

        with col_metrics:
            st.markdown(f"##### 🏷️ {g['label']}")
            st.write(f"운영개월 대비 구독자: **{format_number(g['subs_per_month'])}명/월**")
            st.write(f"최근 10개 중 아웃라이어: **{g['outlier_count']}개**")
            st.write(f"쇼츠 비율: **{g['shorts_ratio'] * 100:.0f}%**")
            st.write(
                f"최근 10개 평균/중앙값 조회수: "
                f"**{format_number(g['avg_views_recent'])} / {format_number(g['median_views_recent'])}**"
            )


# ---------------------------------------------------------------------------
# 채널 직접 검색 결과 카드 (급성장 채널 탭의 "채널 직접 검색" 섹션에서 사용)
# render_growth_channel_row와 비슷하지만 개설일 표시와 채널 저장 버튼이 추가로 있어 별도 함수로 둔다.
# ---------------------------------------------------------------------------
def render_direct_channel_result(g: dict) -> None:
    with st.container(border=True):
        col_thumb, col_info, col_metrics = st.columns([1.1, 2.3, 2.2])

        with col_thumb:
            if g.get("thumbnail_url"):
                st.image(g["thumbnail_url"], use_container_width=True)
            st.link_button(
                "▶ 채널 보기",
                f"https://www.youtube.com/channel/{g['channel_id']}",
                use_container_width=True,
            )

        with col_info:
            st.markdown(f"**{g['channel_title']}**")
            st.write(f"구독자 {format_number(g['subscriber_count'])}명")
            st.write(f"총 영상 {format_number(g['video_count'])}개")
            st.write(f"개설일 {format_date(g.get('published_at'))}")
            st.write(f"운영 {g['months_active']:.1f}개월")

        with col_metrics:
            st.markdown(f"##### 🏷️ {g['label']}")
            st.write(
                f"최근 10개 평균/중앙값 조회수: "
                f"**{format_number(g['avg_views_recent'])} / {format_number(g['median_views_recent'])}**"
            )
            st.write(f"최근 10개 중 아웃라이어: **{g['outlier_count']}개**")
            st.write(f"최근 30일 업로드: **{g['upload_count_recent']}개**")
            st.write(f"쇼츠 비율: **{g['shorts_ratio'] * 100:.0f}%**")

        already_saved = db.is_channel_saved(g["channel_id"])
        save_label = "✅ 저장됨 (다시 눌러 갱신)" if already_saved else "⭐ 채널 저장"
        if st.button(save_label, key=f"save_channel_{g['channel_id']}", use_container_width=True):
            db.save_channel(g)
            st.toast("채널을 저장했습니다.", icon="⭐")
            st.rerun()


# ---------------------------------------------------------------------------
# 채널 비교 표 (급성장 채널 탭의 "채널 비교" 섹션에서 사용)
# ---------------------------------------------------------------------------
def render_channel_comparison(results: list[dict]) -> None:
    """채널 2~3개를 지표별로 나란히 비교하고, 지표마다 가장 높은 채널에 ⭐ 표시한다."""
    # 채널 제목이 우연히 겹치는 경우 비교표 컬럼이 뒤섞이지 않도록 고유하게 만든다.
    seen_titles: dict[str, int] = {}
    column_labels = []
    for g in results:
        title = g.get("channel_title") or "(제목 없음)"
        seen_titles[title] = seen_titles.get(title, 0) + 1
        column_labels.append(title if seen_titles[title] == 1 else f"{title} #{seen_titles[title]}")

    # (표시 라벨, growth dict 키, 포맷 함수, 이 지표에서 최고값을 ⭐로 강조할지 여부)
    metrics = [
        ("구독자수", "subscriber_count", format_number, True),
        ("총 영상수", "video_count", format_number, True),
        ("운영 개월수", "months_active", lambda v: f"{v:.1f}개월", True),
        ("최근 10개 평균 조회수", "avg_views_recent", format_number, True),
        ("최근 10개 중앙값 조회수", "median_views_recent", format_number, True),
        ("아웃라이어 영상 수", "outlier_count", format_number, True),
        ("최근 30일 업로드", "upload_count_recent", format_number, True),
        ("쇼츠 비율", "shorts_ratio", lambda v: f"{v * 100:.0f}%", True),
        ("운영개월 대비 구독자(성장속도)", "subs_per_month", format_number, True),
    ]

    rows: dict[str, list[str]] = {"지표": ["개설일"] + [label for label, *_ in metrics]}
    for label, g in zip(column_labels, results):
        cells = [format_date(g.get("published_at"))]
        for _, key, fmt, _highlight in metrics:
            cells.append(fmt(g.get(key) or 0))
        rows[label] = cells

    for row_idx, (_, key, _fmt, highlight) in enumerate(metrics, start=1):
        if not highlight:
            continue
        raw_values = [g.get(key) or 0 for g in results]
        max_value = max(raw_values)
        if max_value <= 0:
            continue
        for col_label, raw in zip(column_labels, raw_values):
            if raw == max_value:
                rows[col_label][row_idx] = f"⭐ {rows[col_label][row_idx]}"

    compare_df = pd.DataFrame(rows).set_index("지표")
    st.dataframe(compare_df, use_container_width=True)


# ---------------------------------------------------------------------------
# 수동 등록 카드 렌더링 (해외 카드뉴스 수동 등록 탭에서 사용)
# ---------------------------------------------------------------------------
def render_manual_entry_row(entry: dict) -> None:
    with st.container(border=True):
        # 순서: 썸네일 → 제목/핵심지표 → (아래) 편집 폼 → 액션 버튼
        col_thumb, col_info = st.columns([1.1, 4.4])

        image_paths = manual_entries.get_image_full_paths(entry.get("image_paths"))

        with col_thumb:
            if image_paths:
                st.image(str(image_paths[0]), use_container_width=True)
                if len(image_paths) > 1:
                    st.caption(f"+{len(image_paths) - 1}장 더")
            else:
                st.caption("🖼️ 이미지 없음")
            if entry.get("source_url"):
                st.link_button("🔗 원본 보기", entry["source_url"], use_container_width=True)

        with col_info:
            st.markdown(f"**{entry.get('account_name') or '(계정명 없음)'}**")
            st.write(f"플랫폼: {entry.get('platform') or '-'}")
            st.write(f"국가·언어: {entry.get('country') or '-'} / {entry.get('language') or '-'}")
            st.write(f"게시일: {entry.get('posted_at') or '-'}")
            st.write(f"팔로워 {format_number(entry.get('follower_count'))}명")
            metric_parts = []
            if pd.notna(entry.get("view_count")):
                metric_parts.append(f"조회 {format_number(entry['view_count'])}")
            if pd.notna(entry.get("like_count")):
                metric_parts.append(f"좋아요 {format_number(entry['like_count'])}")
            if pd.notna(entry.get("comment_count")):
                metric_parts.append(f"댓글 {format_number(entry['comment_count'])}")
            if pd.notna(entry.get("share_count")):
                metric_parts.append(f"공유 {format_number(entry['share_count'])}")
            if metric_parts:
                st.write(" · ".join(metric_parts))
            saved_at = entry.get("created_at") or ""
            st.caption(f"저장일 {saved_at[:10] if saved_at else '-'}")

        entry_id = entry["id"]
        current_tags = entry["tags"].split(",") if entry.get("tags") else []
        current_status = entry.get("status") or PRODUCTION_STATUS_OPTIONS[0]
        status_index = (
            PRODUCTION_STATUS_OPTIONS.index(current_status)
            if current_status in PRODUCTION_STATUS_OPTIONS
            else 0
        )

        with st.form(key=f"manual_edit_{entry_id}"):
            tags = st.multiselect(
                "카테고리",
                manual_entries.get_all_manual_entry_categories(),
                default=current_tags,
                key=f"manual_tags_{entry_id}",
            )
            status = st.selectbox(
                "제작 상태",
                PRODUCTION_STATUS_OPTIONS,
                index=status_index,
                key=f"manual_status_{entry_id}",
            )
            memo = st.text_area(
                "메모", value=entry.get("memo") or "", height=68, key=f"manual_memo_{entry_id}"
            )
            c3, c4 = st.columns(2)
            save_clicked = c3.form_submit_button("💾 저장", use_container_width=True)
            delete_clicked = c4.form_submit_button("🗑 삭제", use_container_width=True)

        if save_clicked:
            db.update_manual_entry(entry_id, tags, status, memo, DEFAULT_CHANNEL)
            st.toast("저장했습니다.", icon="✅")
            st.rerun()
        if delete_clicked:
            manual_entries.delete_manual_entry(entry_id)
            st.toast("삭제했습니다.", icon="🗑")
            st.rerun()

        st.divider()
        analyze_clicked = st.button(
            "🤖 AI 분석 실행", key=f"analyze_{entry_id}", use_container_width=True
        )
        if analyze_clicked:
            with st.spinner("Gemini로 이미지를 분석하는 중입니다..."):
                try:
                    ai_analysis.analyze_entry(entry_id)
                    st.toast("AI 분석을 완료했습니다.", icon="🤖")
                    st.rerun()
                except (RuntimeError, ValueError) as e:
                    st.error(str(e))
                except genai_errors.APIError as e:
                    st.error(f"Gemini API 오류가 발생했습니다: {e}")

        analysis = ai_analysis.get_analysis_for_display(entry_id)
        if analysis:
            render_ai_analysis(analysis)


def render_ai_analysis(analysis: dict) -> None:
    with st.expander("🤖 AI 분석 결과", expanded=True):
        total_score = analysis.get("total_score") or 0
        if analysis.get("is_recommended"):
            st.success(f"✅ 숨은 이야기형 추천 — 총점 {total_score}/{GIFT_SHOP_MAX_SCORE}")
        else:
            st.info(f"총점 {total_score}/{GIFT_SHOP_MAX_SCORE} — 추천 기준 미달")

        st.markdown(f"**첫 장 후킹 유형**: {analysis.get('hook_type') or '-'}")
        if analysis.get("hook_type_reason"):
            st.caption(analysis["hook_type_reason"])

        ideas = analysis.get("adaptation_ideas") or []
        if ideas:
            st.markdown("**국내 적용 아이디어**")
            for idea in ideas:
                st.write(f"- {idea}")

        st.markdown("**선물가게형 평가**")
        score_cols = st.columns(len(GIFT_SHOP_CRITERIA))
        for col, (key, label) in zip(score_cols, GIFT_SHOP_CRITERIA):
            col.metric(label, f"{analysis.get(f'score_{key}') or 0}점")

        texts = analysis.get("extracted_texts") or []
        shown_any = False
        for i, t in enumerate(texts, start=1):
            if not t.get("original_text") and not t.get("translated_text"):
                continue
            if not shown_any:
                st.markdown("**이미지 텍스트 추출 / 번역**")
                shown_any = True
            st.write(f"**{i}장** 원문: {t.get('original_text') or '-'}")
            st.write(f"　　　번역: {t.get('translated_text') or '-'}")

        analyzed_at = analysis.get("analyzed_at") or ""
        st.caption(f"분석 시각: {analyzed_at[:19] if analyzed_at else '-'}")


# ---------------------------------------------------------------------------
# 탭: 대시보드 / 전체 바이럴 영상 / 검색 결과 / 급성장 채널 / 즐겨찾기 / 해외 카드뉴스 수동 등록
# 탭 이름은 src/config.py의 TAB_DEFINITIONS 한 곳에서만 관리한다(중복 정의 방지).
# ---------------------------------------------------------------------------
(
    tab_dashboard,
    tab_viral,
    tab_search,
    tab_growth,
    tab_favorites,
    tab_manual,
) = st.tabs(TAB_LABELS)

with tab_dashboard:
    with st.popover("❓ 사용법"):
        st.markdown(TAB_HELP["dashboard"])

    today = datetime.now(timezone.utc).date().isoformat()

    col1, col2 = st.columns(2)
    col1.metric("오늘 새로 발견한 콘텐츠", f"{db.get_new_discoveries_count(today)}건")
    col2.metric(
        "저장했지만 미검토",
        f"{db.get_unreviewed_favorites_count()}건",
        help="즐겨찾기에 저장했지만 태그·메모를 아직 입력하지 않은 항목 수입니다.",
    )
    st.caption("'오늘 새로 발견한 콘텐츠'는 검색 탭에서 검색을 실행할 때마다 누적됩니다.")

    st.divider()
    st.markdown(f"**🔥 최근 {RISING_WINDOW_DAYS}일 급상승 영상**")
    rising_df = db.get_rising_videos(days=RISING_WINDOW_DAYS, limit=RISING_VIDEOS_LIMIT)
    if rising_df.empty:
        st.caption("아직 데이터가 없습니다. 검색 탭에서 검색을 실행하면 여기 채워집니다.")
    else:
        st.dataframe(
            rising_df[["title", "channel_title", "view_count", "viral_score", "published_at"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "title": "제목",
                "channel_title": "채널명",
                "view_count": st.column_config.NumberColumn("조회수", format="%d"),
                "viral_score": st.column_config.NumberColumn("바이럴점수", format="%.1f"),
                "published_at": "업로드일",
            },
        )

    st.divider()
    st.markdown(f"**🔁 최근 {KEYWORD_TREND_DAYS}일 반복 등장 키워드**")
    keyword_counts = db.get_top_keywords(days=KEYWORD_TREND_DAYS, limit=KEYWORD_TREND_LIMIT)
    if not keyword_counts:
        st.caption("아직 데이터가 없습니다. 검색 탭에서 검색을 실행하면 여기 채워집니다.")
    else:
        keyword_df = (
            pd.DataFrame(keyword_counts, columns=["키워드", "등장 횟수"])
            .set_index("키워드")
            .sort_values("등장 횟수", ascending=True)
        )
        st.bar_chart(keyword_df, horizontal=True, color="#FF4B4B")

with tab_viral:
    with st.popover("❓ 사용법"):
        st.markdown(TAB_HELP["viral"])

    st.subheader("전체 바이럴 영상")
    st.caption(
        "쇼핑·생활용품 키워드 세트로 search.list를 돌려 쇼츠만 모읍니다(뉴스/정치는 자동 제외)."
    )

    try:
        with st.spinner("바이럴 영상을 확인하는 중..."):
            if viral_videos.refresh_viral_videos():
                mark_data_updated()
    except RuntimeError as e:
        st.error(str(e))
    except HttpError as e:
        st.error(f"YouTube API 오류가 발생했습니다: {e}")

    refresh_col1, refresh_col2 = st.columns([3, 1])
    refresh_col1.caption(
        f"🕒 마지막 업데이트: {viral_videos.format_relative_time(db.get_viral_last_refreshed_at())}"
        f" (자동 갱신은 {VIRAL_REFRESH_INTERVAL_HOURS}시간마다 최대 1회)"
    )
    if refresh_col2.button("🔄 지금 새로고침", key="viral_manual_refresh", use_container_width=True):
        with st.spinner("최신 인기 영상을 가져오는 중..."):
            try:
                viral_videos.refresh_viral_videos(force=True)
                mark_data_updated()
                st.toast("바이럴 영상을 갱신했습니다.", icon="🔄")
                st.rerun()
            except RuntimeError as e:
                st.error(str(e))
            except HttpError as e:
                st.error(f"YouTube API 오류가 발생했습니다: {e}")
    st.caption(
        f"⚠️ 새로고침 시 API 쿼터를 약 {VIRAL_REFRESH_QUOTA_ESTIMATE} units 소모합니다 "
        f"(키워드 {len(VIRAL_SHOPPING_KEYWORDS)}개 × search.list 100 units가 대부분이며, "
        "일일 기본 쿼터 10,000 units 기준으로 하루 2회 자동 갱신해도 여유가 있습니다)."
    )

    vc1, vc2 = st.columns([2, 1])
    viral_keyword = vc1.text_input(
        "검색어(선택)",
        placeholder="입력하면 검색 결과 탭 이용법을 안내합니다",
        key="viral_keyword",
    )
    viral_shopping_keyword = vc2.selectbox(
        "키워드", ["전체"] + VIRAL_SHOPPING_KEYWORDS, key="viral_shopping_keyword"
    )

    with st.expander("상세 필터", expanded=False):
        vf1, vf2 = st.columns(2)
        viral_period_label = vf1.selectbox(
            "업로드 기간",
            list(PERIOD_OPTIONS.keys()),
            index=list(PERIOD_OPTIONS.keys()).index(VIRAL_DEFAULT_PERIOD_LABEL),
            key="viral_period",
        )
        viral_min_views = vf2.number_input(
            "최소 조회수",
            min_value=0,
            value=VIRAL_DEFAULT_MIN_VIEWS,
            step=1000,
            key="viral_min_views",
        )

        st.caption("구독자 범위 (0 = 제한 없음)")
        vf3, vf4 = st.columns(2)
        viral_sub_min = vf3.number_input(
            "최소", min_value=0, value=VIRAL_DEFAULT_SUB_MIN, step=1000, key="viral_sub_min"
        )
        viral_sub_max = vf4.number_input(
            "최대", min_value=0, value=VIRAL_DEFAULT_SUB_MAX, step=1000, key="viral_sub_max"
        )

        viral_sort_label = st.selectbox(
            "정렬",
            list(SORT_OPTIONS.keys()),
            index=list(SORT_OPTIONS.keys()).index(VIRAL_DEFAULT_SORT_LABEL),
            key="viral_sort",
        )

    if viral_keyword.strip():
        st.info(
            f"'{viral_keyword.strip()}' 검색어가 입력되었습니다. 이 탭은 쇼핑 키워드 쇼츠 전용이라, "
            "왼쪽 사이드바의 '🔍 검색' 필드에 같은 키워드를 입력하고 [🔍 검색] 버튼을 누른 뒤 "
            "'🔍 검색 결과' 탭에서 확인해주세요."
        )
    else:
        viral_df = viral_videos.load_viral_videos_df()
        if viral_df.empty:
            st.info("아직 데이터가 없습니다. 위 [🔄 지금 새로고침] 버튼을 눌러 최초 데이터를 가져오세요.")
        else:
            filtered = viral_df.copy()
            if viral_shopping_keyword != "전체":
                filtered = filtered[filtered["keyword"] == viral_shopping_keyword]

            period_cutoff = datetime.now(timezone.utc) - pd.Timedelta(
                hours=PERIOD_OPTIONS[viral_period_label]
            )
            filtered = filtered[
                pd.to_datetime(filtered["published_at"], utc=True, errors="coerce") >= period_cutoff
            ]
            if viral_min_views:
                filtered = filtered[filtered["view_count"] >= viral_min_views]
            if viral_sub_min:
                filtered = filtered[filtered["subscriber_count"] >= viral_sub_min]
            if viral_sub_max:
                filtered = filtered[filtered["subscriber_count"] <= viral_sub_max]

            if filtered.empty:
                st.warning("조건에 맞는 영상이 없습니다. 상세 필터를 완화해보세요.")
            else:
                st.subheader(f"바이럴 영상 ({len(filtered)}건)")
                sorted_df = filtered.sort_values(SORT_OPTIONS[viral_sort_label], ascending=False)
                for _, row in sorted_df.iterrows():
                    render_result_row(row, key_prefix="viral")

with tab_search:
    with st.popover("❓ 사용법"):
        st.markdown(TAB_HELP["search"])

    results_df = st.session_state.get("results_df")
    if results_df is None:
        st.info("👈 왼쪽 사이드바에서 검색 키워드를 입력하고 [🔍 검색] 버튼을 눌러주세요.")
    elif results_df.empty:
        st.warning("조건에 맞는 영상이 없습니다. 사이드바의 상세 필터를 완화해보세요.")
    else:
        st.subheader(f"'{st.session_state.get('last_keyword', '')}' 검색 결과 ({len(results_df)}건)")
        sorted_df = results_df.sort_values(SORT_OPTIONS[sort_label], ascending=False)
        for _, row in sorted_df.iterrows():
            render_result_row(row, key_prefix="search")

with tab_growth:
    with st.popover("❓ 사용법"):
        st.markdown(TAB_HELP["growth"])

    st.subheader("급성장 채널 찾기")
    st.caption(
        "최근 업로드 10개의 조회수를 채널 자체 평균/중앙값과 비교해 아웃라이어 비율이 높은 "
        "채널을 찾습니다. 채널 1개를 분석할 때마다 API 쿼터를 약 100단위 소모하므로 "
        "후보 채널 수가 많으면 시간이 걸릴 수 있습니다."
    )

    channel_keyword = st.text_input(
        "채널 검색 키워드", placeholder="예: 여행 브이로그", key="channel_keyword"
    )

    with st.expander("상세 필터", expanded=False):
        c1, c2 = st.columns(2)
        months_min = c1.number_input(
            "운영 개월(최소)", min_value=0, value=0, step=1, key="months_min"
        )
        months_max = c2.number_input(
            "운영 개월(최대, 0=제한없음)", min_value=0, value=0, step=1, key="months_max"
        )

        st.caption("구독자 범위 (0 = 제한 없음)")
        c3, c4 = st.columns(2)
        ch_sub_min = c3.number_input(
            "최소", min_value=0, value=0, step=1000, key="ch_sub_min"
        )
        ch_sub_max = c4.number_input(
            "최대", min_value=0, value=0, step=1000, key="ch_sub_max"
        )

        min_uploads_recent = st.number_input(
            "최근 30일 업로드 수(최소)", min_value=0, value=0, step=1, key="min_uploads_recent"
        )
        min_shorts_ratio = st.slider(
            "쇼츠 비율(최근 10개 중, 최소 %)", 0, 100, 0, key="min_shorts_ratio"
        )

    growth_search_clicked = st.button("🚀 채널 검색", type="primary", key="growth_search_btn")

    if growth_search_clicked:
        if not channel_keyword.strip():
            st.warning("채널 검색 키워드를 입력하세요.")
        else:
            with st.spinner("채널을 분석하는 중입니다... (채널 수에 따라 다소 걸릴 수 있습니다)"):
                try:
                    channels = fetch_growing_channels(
                        keyword=channel_keyword.strip(),
                        months_min=months_min,
                        months_max=months_max,
                        sub_min=ch_sub_min,
                        sub_max=ch_sub_max,
                        min_uploads_recent=min_uploads_recent,
                        min_shorts_ratio=min_shorts_ratio,
                        max_candidates=GROWTH_MAX_CANDIDATES,
                    )
                    st.session_state["growth_results"] = channels
                    st.session_state["last_channel_keyword"] = channel_keyword.strip()
                    mark_data_updated()
                except RuntimeError as e:
                    st.error(str(e))
                except HttpError as e:
                    st.error(f"YouTube API 오류가 발생했습니다: {e}")

    growth_results = st.session_state.get("growth_results")
    if growth_results is None:
        st.info("👆 채널 검색 키워드를 입력하고 [🚀 채널 검색] 버튼을 눌러주세요.")
    elif not growth_results:
        st.warning("조건에 맞는 채널이 없습니다. 상세 필터를 완화해보세요.")
    else:
        st.subheader(
            f"'{st.session_state.get('last_channel_keyword', '')}' 채널 분석 결과 "
            f"({len(growth_results)}건)"
        )
        growth_sorted = sorted(growth_results, key=lambda g: g["outlier_ratio"], reverse=True)
        for g in growth_sorted:
            render_growth_channel_row(g)

    st.divider()
    st.subheader("채널 직접 검색")
    st.caption(
        "채널명(핸들)·채널 URL·채널 ID를 입력하면 그 채널 하나만 바로 조회합니다. "
        "search.list 없이 channels.list + playlistItems.list만 사용해 위 카테고리 탐색보다 훨씬 적은 "
        "쿼터로 동작합니다."
    )

    direct_channel_query = st.text_input(
        "채널명(핸들) / 채널 URL / 채널 ID",
        placeholder="예: @마크에디션, https://www.youtube.com/@마크에디션, UCxxxxxxxxxxxxxxxxxxxxxx",
        key="direct_channel_query",
    )
    direct_search_clicked = st.button("🔎 채널 조회", key="direct_channel_search_btn")

    if direct_search_clicked:
        if not direct_channel_query.strip():
            st.warning("채널명(핸들), URL, 또는 채널 ID를 입력하세요.")
        else:
            with st.spinner("채널을 조회하는 중입니다..."):
                try:
                    st.session_state["direct_channel_result"] = fetch_channel_by_direct_query(
                        direct_channel_query.strip()
                    )
                    st.session_state["direct_channel_searched"] = True
                    mark_data_updated()
                except RuntimeError as e:
                    st.error(str(e))
                except HttpError as e:
                    st.error(f"YouTube API 오류가 발생했습니다: {e}")

    direct_channel_result = st.session_state.get("direct_channel_result")
    if not st.session_state.get("direct_channel_searched"):
        st.info("👆 채널명(핸들)·URL·채널 ID를 입력하고 [🔎 채널 조회] 버튼을 눌러주세요.")
    elif direct_channel_result is None:
        st.warning(
            "채널을 찾을 수 없습니다. 채널 ID(UC로 시작하는 24자), @핸들, 또는 채널 URL을 정확히 "
            "입력했는지 확인해주세요. (일반 채널명 검색은 search.list가 필요해 이 기능에서는 지원하지 "
            "않습니다.)"
        )
    else:
        render_direct_channel_result(direct_channel_result)

    saved_channels_df = db.get_saved_channels()
    if not saved_channels_df.empty:
        st.markdown("**📌 저장된 채널**")
        for _, srow in saved_channels_df.iterrows():
            sc1, sc2, sc3 = st.columns([3, 2, 1])
            sc1.write(f"**{srow['channel_title']}**")
            sc2.caption(
                f"🏷️ {srow.get('label') or '-'} · 구독자 {format_number(srow.get('subscriber_count'))}명"
            )
            if sc3.button("🗑", key=f"remove_saved_channel_{srow['channel_id']}", help="저장 해제"):
                db.remove_saved_channel(srow["channel_id"])
                st.toast("저장을 해제했습니다.", icon="🗑")
                st.rerun()

    st.divider()
    st.subheader("채널 비교")
    st.caption(
        "채널 2~3개를 입력해 지표를 나란히 비교합니다. 채널 직접 검색과 동일하게 "
        "channels.list + playlistItems.list만 사용해 채널당 3 units만 소모합니다(search.list 미사용)."
    )

    comp_col1, comp_col2, comp_col3 = st.columns(3)
    compare_query_1 = comp_col1.text_input(
        "채널 1", key="compare_query_1", placeholder="@핸들 또는 URL/ID"
    )
    compare_query_2 = comp_col2.text_input(
        "채널 2", key="compare_query_2", placeholder="@핸들 또는 URL/ID"
    )
    compare_query_3 = comp_col3.text_input(
        "채널 3 (선택)", key="compare_query_3", placeholder="@핸들 또는 URL/ID"
    )

    compare_clicked = st.button("🆚 채널 비교", key="channel_compare_btn")

    if compare_clicked:
        compare_queries = [
            q.strip() for q in (compare_query_1, compare_query_2, compare_query_3) if q.strip()
        ]
        if len(compare_queries) < 2:
            st.warning("비교하려면 채널을 2개 이상 입력하세요.")
        else:
            with st.spinner("채널들을 조회하는 중입니다..."):
                try:
                    found, not_found = [], []
                    for q in compare_queries:
                        result = fetch_channel_by_direct_query(q)
                        (found if result is not None else not_found).append(result or q)
                    st.session_state["compare_results"] = found
                    st.session_state["compare_not_found"] = not_found
                    mark_data_updated()
                except RuntimeError as e:
                    st.error(str(e))
                except HttpError as e:
                    st.error(f"YouTube API 오류가 발생했습니다: {e}")

    compare_results = st.session_state.get("compare_results")
    compare_not_found = st.session_state.get("compare_not_found") or []

    if compare_results is None:
        st.info("👆 채널을 2~3개 입력하고 [🆚 채널 비교] 버튼을 눌러주세요.")
    else:
        if compare_not_found:
            st.warning("다음 채널을 찾지 못했습니다: " + ", ".join(compare_not_found))
        if len(compare_results) >= 2:
            render_channel_comparison(compare_results)
        else:
            st.warning("비교할 채널을 2개 이상 찾지 못했습니다. 입력을 확인해주세요.")

with tab_favorites:
    with st.popover("❓ 사용법"):
        st.markdown(TAB_HELP["favorites"])

    fav_df = db.get_all_favorites()
    if fav_df.empty:
        st.info("아직 즐겨찾기한 영상이 없습니다. 🔍 검색 탭에서 영상을 찾아 ⭐ 버튼으로 저장해보세요.")
    else:
        with st.expander("필터", expanded=False):
            tag_filter = st.multiselect("태그로 필터링", AVAILABLE_TAGS, key="fav_tag_filter")

        display_df = fav_df.copy()
        if tag_filter:
            display_df = display_df[
                display_df["tags"].apply(
                    lambda t: any(tag in (t.split(",") if t else []) for tag in tag_filter)
                )
            ]

        csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 CSV로 내보내기",
            data=csv_bytes,
            file_name=f"content_radar_favorites_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )

        st.caption(f"{len(display_df)}건 표시 중 (전체 {len(fav_df)}건)")
        for _, row in display_df.iterrows():
            render_result_row(row, key_prefix="fav")

with tab_manual:
    with st.popover("❓ 사용법"):
        st.markdown(TAB_HELP["manual"])

    st.subheader("해외 카드뉴스 수동 등록")

    with st.expander("➕ 새 카테고리 추가", expanded=False):
        new_category_input = st.text_input("새 카테고리 이름", key="new_manual_category_input")
        if st.button("추가", key="add_manual_category_btn"):
            try:
                manual_entries.add_manual_entry_category(new_category_input)
                st.toast("카테고리를 추가했습니다.", icon="✅")
                st.rerun()
            except ValueError as e:
                st.warning(str(e))

    with st.form(key="manual_entry_form", clear_on_submit=True):
        source_url = st.text_input("원본 URL")

        c1, c2 = st.columns(2)
        account_name = c1.text_input("계정명")
        platform = c2.selectbox("플랫폼", PLATFORM_OPTIONS)

        c3, c4 = st.columns(2)
        country = c3.text_input("국가")
        language = c4.text_input("언어")

        posted_at = st.date_input("게시 날짜", value=datetime.now(timezone.utc).date())

        st.caption("성과 지표 (선택 입력 — 모르면 0으로 두면 미입력으로 저장됩니다)")
        c5, c6, c7, c8 = st.columns(4)
        view_count = c5.number_input("조회수", min_value=0, value=0, step=100)
        like_count = c6.number_input("좋아요", min_value=0, value=0, step=10)
        comment_count = c7.number_input("댓글", min_value=0, value=0, step=1)
        share_count = c8.number_input("공유", min_value=0, value=0, step=1)

        follower_count = st.number_input("계정 팔로워 수", min_value=0, value=0, step=100)

        images = st.file_uploader(
            "이미지 업로드 (여러 장 가능, 드래그앤드롭 지원)",
            type=MANUAL_ENTRY_IMAGE_TYPES,
            accept_multiple_files=True,
        )

        memo = st.text_area("개인 메모", height=80)
        tags = st.multiselect("카테고리", manual_entries.get_all_manual_entry_categories())
        status = st.selectbox("제작 상태", PRODUCTION_STATUS_OPTIONS)

        register_clicked = st.form_submit_button(
            "➕ 등록", type="primary", use_container_width=True
        )

    if register_clicked:
        if not source_url.strip() or not account_name.strip():
            st.warning("원본 URL과 계정명은 필수입니다.")
        else:
            manual_entries.create_manual_entry(
                source_url=source_url.strip(),
                account_name=account_name.strip(),
                platform=platform,
                country=country.strip(),
                language=language.strip(),
                posted_at=posted_at.isoformat(),
                view_count=view_count or None,
                like_count=like_count or None,
                comment_count=comment_count or None,
                share_count=share_count or None,
                follower_count=follower_count,
                images=images,
                memo=memo,
                tags=tags,
                status=status,
                channel=DEFAULT_CHANNEL,
            )
            st.toast("등록했습니다.", icon="✅")
            st.rerun()

    st.divider()
    st.subheader("등록된 항목")

    manual_df = db.get_all_manual_entries()
    if manual_df.empty:
        st.info("아직 등록된 항목이 없습니다. 위 폼에 정보를 입력하고 [➕ 등록] 버튼을 눌러 첫 항목을 추가해보세요.")
    else:
        with st.expander("필터", expanded=False):
            fc1, fc2, fc3 = st.columns(3)
            manual_status_filter = fc1.multiselect(
                "제작 상태 필터", PRODUCTION_STATUS_OPTIONS, key="manual_status_filter"
            )
            manual_tag_filter = fc2.multiselect(
                "카테고리 필터", manual_entries.get_all_manual_entry_categories(), key="manual_tag_filter"
            )
            manual_platform_filter = fc3.multiselect(
                "플랫폼 필터", PLATFORM_OPTIONS, key="manual_platform_filter"
            )

        manual_filtered = manual_df.copy()
        if manual_status_filter:
            manual_filtered = manual_filtered[manual_filtered["status"].isin(manual_status_filter)]
        if manual_tag_filter:
            manual_filtered = manual_filtered[
                manual_filtered["tags"].apply(
                    lambda t: any(tag in (t.split(",") if t else []) for tag in manual_tag_filter)
                )
            ]
        if manual_platform_filter:
            manual_filtered = manual_filtered[manual_filtered["platform"].isin(manual_platform_filter)]

        st.caption(f"{len(manual_filtered)}건 표시 중 (전체 {len(manual_df)}건)")
        for _, row in manual_filtered.iterrows():
            render_manual_entry_row(row.to_dict())

