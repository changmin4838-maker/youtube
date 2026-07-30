from __future__ import annotations

import json

from google import genai
from google.genai import types

from src import database as db
from src import manual_entries
from src.config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GIFT_SHOP_CRITERIA,
    GIFT_SHOP_RECOMMEND_THRESHOLD,
    HOOK_TYPES,
)

_SCORE_KEYS = [key for key, _ in GIFT_SHOP_CRITERIA]

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _get_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return genai.Client(api_key=GEMINI_API_KEY)


def _build_schema() -> types.Schema:
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "extracted_texts": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "original_text": types.Schema(type=types.Type.STRING),
                        "translated_text": types.Schema(type=types.Type.STRING),
                    },
                    required=["original_text", "translated_text"],
                ),
            ),
            "hook_type": types.Schema(type=types.Type.STRING, enum=HOOK_TYPES),
            "hook_type_reason": types.Schema(type=types.Type.STRING),
            "adaptation_ideas": types.Schema(
                type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
            ),
            "scores": types.Schema(
                type=types.Type.OBJECT,
                properties={key: types.Schema(type=types.Type.INTEGER) for key in _SCORE_KEYS},
                required=_SCORE_KEYS,
            ),
        },
        required=[
            "extracted_texts", "hook_type", "hook_type_reason", "adaptation_ideas", "scores",
        ],
    )


def _build_prompt(entry: dict) -> str:
    criteria_lines = "\n".join(f"- {key}: {label}" for key, label in GIFT_SHOP_CRITERIA)
    memo = entry.get("memo") or "(없음)"
    return f"""당신은 해외 소셜미디어 카드뉴스를 분석해 국내 콘텐츠 제작에 활용하는 전문 에디터입니다.
다음은 {entry.get('platform', '')} 계정 "{entry.get('account_name', '')}"이 올린 카드뉴스 이미지들입니다.
이미지는 게시 순서대로 주어지며, 첫 번째 이미지가 "첫 장"입니다.

작업:
1. 각 이미지에 포함된 외국어 텍스트를 추출하고 한국어로 번역하세요. 텍스트가 없으면 두 값 모두 빈 문자열로 두세요.
2. 첫 장(첫 번째 이미지)의 후킹 방식을 아래 16종 중 정확히 하나로 분류하고, 이유를 1문장으로 설명하세요.
   유형: {", ".join(HOOK_TYPES)}
3. 이 카드뉴스를 한국 시청자 대상으로 각색할 아이디어를 정확히 5개 제안하세요. 각각 한 문장으로.
4. "선물가게형" 콘텐츠 후보로서 아래 7개 항목을 1~5점으로 평가하세요(5점 = 매우 그렇다, 1점 = 전혀 아니다):
{criteria_lines}

참고 메모(있다면): {memo}
"""


def _load_image_parts(entry: dict) -> list[types.Part]:
    image_paths = manual_entries.get_image_full_paths(entry.get("image_paths"))
    parts = []
    for path in image_paths:
        if not path.exists():
            continue
        mime_type = _MIME_TYPES.get(path.suffix.lower(), "image/jpeg")
        parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type))
    return parts


def analyze_entry(entry_id: int) -> dict:
    """등록 항목의 이미지를 Gemini로 분석해 결과를 저장하고 반환한다."""
    entry = db.get_manual_entry(entry_id)
    if entry is None:
        raise ValueError("등록된 항목을 찾을 수 없습니다.")

    image_parts = _load_image_parts(entry)
    if not image_parts:
        raise ValueError("분석할 이미지가 없습니다. 이미지를 먼저 업로드하세요.")

    client = _get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[_build_prompt(entry), *image_parts],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_build_schema(),
        ),
    )
    result = json.loads(response.text)

    scores = {key: int(result.get("scores", {}).get(key, 0)) for key in _SCORE_KEYS}
    total_score = sum(scores.values())
    is_recommended = total_score >= GIFT_SHOP_RECOMMEND_THRESHOLD

    analysis = {
        "extracted_texts": json.dumps(result.get("extracted_texts", []), ensure_ascii=False),
        "hook_type": result.get("hook_type", ""),
        "hook_type_reason": result.get("hook_type_reason", ""),
        "adaptation_ideas": json.dumps(result.get("adaptation_ideas", []), ensure_ascii=False),
        "scores": scores,
        "total_score": total_score,
        "is_recommended": is_recommended,
    }
    db.upsert_ai_analysis(entry_id, analysis)
    return get_analysis_for_display(entry_id)


def get_analysis_for_display(entry_id: int) -> dict | None:
    """저장된 AI 분석 결과를 화면 표시용으로 역직렬화해 반환한다."""
    row = db.get_ai_analysis(entry_id)
    if row is None:
        return None
    row = dict(row)
    row["extracted_texts"] = json.loads(row.get("extracted_texts") or "[]")
    row["adaptation_ideas"] = json.loads(row.get("adaptation_ideas") or "[]")
    return row
