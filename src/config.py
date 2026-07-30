import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# 기본값은 기존과 동일(프로젝트 루트의 data/)하되, DATA_DIR/DB_PATH/UPLOADS_DIR 환경변수를
# 설정하면 그 경로를 그대로 쓴다. Railway 등에서 영구 Volume을 마운트할 때 이 환경변수만
# Volume 경로로 지정하면 코드 변경 없이 데이터가 그 위치에 저장된다.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "content_radar.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 업로드 기간 필터: 표시 라벨 -> 시간(hours)
PERIOD_OPTIONS = {
    "24시간": 24,
    "3일": 24 * 3,
    "7일": 24 * 7,
    "14일": 24 * 14,
    "30일": 24 * 30,
    "90일": 24 * 90,
}

# 영상 형식 필터
VIDEO_FORMAT_OPTIONS = ["전체", "쇼츠"]
SHORTS_MAX_SECONDS = 60  # 쇼츠 판정 기준(초)

# 정렬 옵션: 표시 라벨 -> 정렬 기준 컬럼
SORT_OPTIONS = {
    "종합점수순": "viral_score",
    "조회수순": "view_count",
    "배수순": "channel_avg_multiple",
    "좋아요율순": "like_rate",
}

# 즐겨찾기 태그 고정 목록
AVAILABLE_TAGS = ["요잇템", "숨은이야기형", "해외바이럴", "이음나무", "상징을띄우다"]

# 바이럴 점수 가중치
VIRAL_SCORE_WEIGHTS = {
    "channel_avg_multiple": 0.35,
    "views_per_day": 0.25,
    "views_per_sub": 0.20,
    "like_rate": 0.15,
    "freshness": 0.05,
}

# search.list 1회 최대 결과 수(YouTube API 상한)
MAX_SEARCH_RESULTS = 50

# --- 전체 바이럴 영상 탭 (쇼핑/생활용품 키워드 기반 search.list 수집) -------------
# 아래 키워드 세트로 search.list(쇼츠 필터, 1회 = 100 units)를 돌려 결과를 모은다.
# 필요하면 이 리스트에 키워드를 추가/수정하기만 하면 다음 새로고침부터 반영된다.
VIRAL_SHOPPING_KEYWORDS = [
    "생활꿀템",
    "다이소 꿀템",
    "가성비 아이템",
    "화장품 추천",
    "뷰티템 리뷰",
    "주방용품 추천",
    "신박한 아이템",
    "잇템 추천",
]

# 뉴스/정치(유튜브 공식 videoCategoryId=25)는 쇼핑 키워드 검색 결과에 섞여 들어와도 무조건 제외한다.
VIRAL_EXCLUDED_CATEGORY_IDS = {"25"}

# 새로고침 1회당 대략적인 쿼터 소모 추정치. 지배적인 비용은 키워드당 search.list 1회(100 units)이고,
# videos.list/channels.list 배치 조회는 몇 units 수준이라 무시할 만하다.
# 예: 키워드 8개 = 약 800 units. 일일 기본 쿼터(10,000 units) 기준 하루 2회 갱신해도 여유가 있다.
VIRAL_REFRESH_QUOTA_ESTIMATE = len(VIRAL_SHOPPING_KEYWORDS) * 100

# 이 시간 이내 재방문하면 저장된 결과를 그대로 보여주고 API를 다시 부르지 않는다(하루 최대 2회 갱신).
VIRAL_REFRESH_INTERVAL_HOURS = 12
# search.list 수집 시 publishedAfter 기준 창이자, 바이럴 점수 "최신성" 지표의 정규화 기준 창이기도 하다.
VIRAL_FRESHNESS_WINDOW_HOURS = 24 * 30

# 탭 최초 진입 시 자동 세팅되는 기본 필터값. 전부 이미 가져온 데이터 안에서만 걸러내므로
# 필터를 바꿔도 API 재호출 없이 즉시 반영된다. 수집 자체가 쇼츠로 고정돼 있어 영상 형식 필터는 없다.
VIRAL_DEFAULT_PERIOD_LABEL = "7일"
VIRAL_DEFAULT_MIN_VIEWS = 10000
VIRAL_DEFAULT_SUB_MIN = 100
VIRAL_DEFAULT_SUB_MAX = 100000
VIRAL_DEFAULT_SORT_LABEL = "종합점수순"

# --- 급성장 채널 탭 ---------------------------------------------------------
# 채널 검색 시 후보로 가져올 채널 수. 채널 1개당 API 쿼터를 약 100단위 소모하므로
# (search.list order=date 1회 = 100 units) 과도하게 늘리지 않는다.
# 기본 일일 쿼터(10,000 units)로 하루에 여러 번 검색하려면 8 정도가 안전하다.
GROWTH_MAX_CANDIDATES = 8
GROWTH_RECENT_SAMPLE_SIZE = 10  # "최근 N개 영상" 기준
GROWTH_RECENT_UPLOAD_WINDOW_DAYS = 30  # "최근 30일 업로드 수" 기준

# 아웃라이어 판정: 최근 N개 영상의 중앙값 대비 이 배수 이상이면 아웃라이어로 카운트
GROWTH_OUTLIER_MULTIPLIER = 2.0

# 추천 라벨 분류 임계값 (아래에서부터 순서대로 검사해 첫 매치를 채택)
GROWTH_NEW_CHANNEL_MONTHS = 6            # 개설 후 이 개월수 이하면 "신규"로 간주
GROWTH_NEW_CHANNEL_MIN_OUTLIER_RATIO = 0.3   # 그리고 최근 영상 중 30% 이상이 아웃라이어면 "신규 급성장"
GROWTH_CONSECUTIVE_VIRAL_RATIO = 0.7         # 최근 영상의 70% 이상이 아웃라이어면 "연속 바이럴"
GROWTH_FEW_VIDEOS_MAX_COUNT = 20             # 전체 업로드 수가 이 이하면서
GROWTH_FEW_VIDEOS_MIN_SUBS = 10000           # 구독자가 이 이상이면 "소수 영상 고효율"
GROWTH_SHORTS_DOMINANT_RATIO = 0.7           # 최근 영상의 70% 이상이 쇼츠면 "쇼츠 강세"
GROWTH_FALLBACK_LABEL = "관찰 필요"

# --- 대시보드 탭 -------------------------------------------------------------
RISING_WINDOW_DAYS = 7       # "최근 N일 급상승 영상"의 기준 기간(업로드일 기준)
RISING_VIDEOS_LIMIT = 8
KEYWORD_TREND_DAYS = 30      # "최근 반복 등장 키워드" 집계 기간(최초 발견일 기준)
KEYWORD_TREND_LIMIT = 8

# --- 해외 카드뉴스 수동 등록 탭 ----------------------------------------------
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", str(DATA_DIR / "uploads")))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

PLATFORM_OPTIONS = ["Instagram", "TikTok", "Pinterest", "기타"]

MANUAL_ENTRY_IMAGE_TYPES = ["png", "jpg", "jpeg", "webp", "gif"]

# 즐겨찾기 태그(AVAILABLE_TAGS)와 별개로, 수동 등록 항목만을 위한 주제중심 카테고리.
# "기타"는 항상 마지막에 오는 고정 예비 카테고리로 취급한다(구 태그 마이그레이션의 매핑 대상이기도 함).
MANUAL_ENTRY_TAGS = [
    "뉴스·이슈", "과학", "AI·기술", "동물", "자연·환경", "건강", "심리", "인간관계",
    "패션", "뷰티", "음식", "여행", "역사", "문화", "유명인", "감동", "미스터리",
    "유머", "생활정보", "기타",
]
MANUAL_ENTRY_CATEGORY_FALLBACK = "기타"

# 제작 파이프라인 단계(왼쪽일수록 이전 단계)
PRODUCTION_STATUS_OPTIONS = [
    "미검토", "검토중", "제작후보", "원고작성", "이미지준비",
    "편집중", "업로드완료", "성과확인", "재활용후보", "제외",
]
MANUAL_ENTRY_DEFAULT_STATUS = "미검토"

# --- AI 분석 (Gemini) --------------------------------------------------------
# "gemini-flash-latest"는 현재 권장되는 flash 모델을 가리키는 별칭이라
# 특정 버전이 단종돼도(예: 이번에 확인된 gemini-2.5-flash 신규유저 제한) 코드 수정 없이 유지된다.
GEMINI_MODEL = "gemini-flash-latest"

# 첫 장 후킹 유형 16종 (표준 제안 — 필요에 맞게 수정 가능)
HOOK_TYPES = [
    "숫자형", "질문형", "반전형", "모순형", "비교형", "경고형", "공감형",
    "비밀공개형", "스토리텔링형", "챌린지형", "리스트형", "궁금증유발형",
    "충격통계형", "오해불식형", "꿀팁형", "권위형",
]

# 선물가게형 후보 평가 7개 항목: (내부 키, 화면 표시 라벨)
GIFT_SHOP_CRITERIA = [
    ("hidden_story", "숨은 이야기 여부"),
    ("title_twist", "제목 반전 가능성"),
    ("product_new_view", "제품이 다르게 보이는지"),
    ("conflict_resolution", "20~40초 내 갈등/해결 가능성"),
    ("visual_material", "시각자료 유무"),
    ("purchasable_link", "구매가능 상품 연결성"),
    ("fact_and_fun", "사실+재미 확보"),
]
GIFT_SHOP_MAX_SCORE = 5 * len(GIFT_SHOP_CRITERIA)  # 35
GIFT_SHOP_RECOMMEND_THRESHOLD = 25  # 이 총점 이상이면 "숨은 이야기형 추천" (조정 가능)

# --- 콘텐츠 채널 필드(화면 UI에서는 제거됨) -----------------------------------
# 예전엔 멜로바이브/소리정원 등으로 채널을 구분했지만, 범용 개인 도구로 방향이 바뀌며
# 그 구분 UI는 제거했다. DB의 channel 컬럼은 남아있고(마이그레이션 부담 회피), 저장 시
# 이 기본값이 항상 채워진다.
DEFAULT_CHANNEL = "미정"

# --- 화면 텍스트(UI 문구) -----------------------------------------------------
# 여기 문구만 바꾸면 화면에 바로 반영된다(로직과 분리).
APP_TITLE = "🛍️ 요잇템 레이더 (YoItem Radar)"
APP_SUBTITLE = "생활·상품 소재 발굴 & 유튜브 아웃라이어 탐지 도구"

# 탭 정의 — 화면 상단 탭(st.tabs)이 이 표 하나로 관리된다(탭 이름을 여러 곳에 중복 하드코딩하지 않도록).
TAB_DEFINITIONS = [
    {"icon": "📊", "tab_name": "대시보드"},
    {"icon": "🔥", "tab_name": "전체 바이럴 영상"},
    {"icon": "🔍", "tab_name": "검색 결과"},
    {"icon": "🚀", "tab_name": "급성장 채널"},
    {"icon": "⭐", "tab_name": "즐겨찾기"},
    {"icon": "🌍", "tab_name": "해외 카드뉴스 수동 등록"},
]

# st.tabs()에 그대로 넘길 라벨 목록: ["📊 대시보드", "🔍 검색 결과", ...]
TAB_LABELS = [f"{t['icon']} {t['tab_name']}" for t in TAB_DEFINITIONS]

# 탭별 "❓ 사용법" 팝오버에 표시할 안내 문구(3~5줄, Markdown)
TAB_HELP = {
    "dashboard": (
        "오늘 새로 발견한 콘텐츠, 미검토 즐겨찾기, 최근 급상승 영상, 반복 키워드를 "
        "한눈에 모아 보여줍니다.\n\n"
        "검색 탭에서 검색을 실행할수록 데이터가 쌓입니다."
    ),
    "viral": (
        "쇼핑·생활용품·꿀템 중심 쇼츠 발굴 탭입니다. 정해진 쇼핑 키워드 세트로 search.list를 돌려 "
        "쇼츠만 모으고, 뉴스/정치 카테고리는 결과에 섞여도 자동으로 제외합니다.\n\n"
        "① 키워드를 고르면(또는 '전체') 이미 가져온 결과 중 해당 키워드로 모인 영상만 보여줍니다.\n\n"
        "② 검색어를 입력하면 이 탭 대신 왼쪽 사이드바 검색 → '🔍 검색 결과' 탭을 이용하도록 안내합니다.\n\n"
        "③ 자동 갱신은 하루 최대 2회(약 12시간 간격)만 이뤄지고, 상세 필터는 이미 가져온 데이터 안에서 "
        "즉시 걸러줄 뿐 API를 다시 부르지 않습니다. 더 최신 데이터가 필요하면 **🔄 지금 새로고침**을 누르세요."
    ),
    "search": (
        "① 왼쪽 사이드바에서 키워드를 입력하고, 필요하면 상세 필터(기간·조회수·구독자·형식)를 펼쳐 설정하세요.\n\n"
        "② **검색** 버튼을 누르면 결과가 이 탭에 표시됩니다.\n\n"
        "③ 마음에 드는 영상은 태그를 지정해 **⭐ 즐겨찾기 저장** 버튼으로 저장하세요."
    ),
    "growth": (
        "① 채널 관련 키워드를 입력하고, 운영 개월·구독자·업로드 수·쇼츠 비율 필터를 필요하면 펼쳐 설정하세요.\n\n"
        "② **채널 검색**을 누르면 최근 업로드 대비 아웃라이어 비율이 높은 채널이 표시됩니다.\n\n"
        "③ 채널 1개당 API 쿼터 소모가 크니(약 100단위) 후보 수를 적절히 조절하세요."
    ),
    "favorites": (
        "검색 탭에서 저장한 영상이 모이는 곳입니다.\n\n"
        "태그로 필터링해 원하는 항목만 볼 수 있고, CSV로 내보낼 수 있습니다.\n\n"
        "각 카드에서 태그·메모를 수정할 수 있습니다."
    ),
    "manual": (
        "① 위 등록 폼에 해외 카드뉴스 정보(원본 URL, 계정, 지표, 이미지)를 입력하고 **등록**하세요.\n\n"
        "② 등록된 항목에서 **🤖 AI 분석 실행**으로 후킹 유형·국내 적용 아이디어·선물가게형 점수를 얻을 수 있습니다."
    ),
}
