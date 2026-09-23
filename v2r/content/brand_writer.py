"""브랜드(제휴) 바이럴 원고 생성기.

Make 시나리오 지침(`warehouse/guides/★NEW 카페 바이럴★/`)을 브랜드별 규칙 표로
증류해, 키워드 하나로 **제목 + 본문 + 댓글 12개**를 만든다.

- 본문은 `brand_body` 용도(모델), 댓글은 `brand_comments` 용도로 나눠 부른다.
- 결과 `Manuscript`의 댓글 구조는 `parse_affiliate_rows`/`parse_article`이 만드는
  것과 같아서(라벨·깊이·역할) 기존 발행 사슬이 그대로 받아 쓴다.
- 발행도 시트 쓰기도 하지 않는다. 검토용 JSON/MD만 남긴다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from v2r.content.manuscript import CommentNode, Manuscript, content_hash, tags_from_keyword
from v2r.content.guide_compress import compress_guide_text
from v2r.llm.prompts import (
    BRAND_BODY_TASK,
    BRAND_CLICHE_RULE,
    BRAND_COMBINED_TASK,
    BRAND_COMMENTS_TASK,
    BRAND_FIRST_MENTION_RULE,
    BRAND_LINE_BREAK_RULE,
    BRAND_PARTIAL_RETRY_TASK,
    BRAND_REVIEW_THREAD_FLOW,
    BRAND_SHARED_SYSTEM,
    HUMAN_TONE_RULES,
    NO_INTERNAL_TERMS_RULE,
)

log = logging.getLogger(__name__)

#: 댓글 12개 라벨 (읽는 순서 = live-comment-order.md §1)
COMMENT_LABELS: tuple[str, ...] = (
    "댓글1",
    "대댓글1",
    "댓글2",
    "대댓글2",
    "대대댓글2",
    "대대대댓글2",
    "댓글3",
    "대댓글3",
    "댓글4",
    "대댓글4",
    "댓글5",
    "대댓글5",
)

#: 라벨 → 작성 계정 역할 (live-comment-order.md §1·§2)
ACCOUNT_ROLES_COMMON: dict[str, str] = {
    "댓글1": "댓글풀 A1",
    "대댓글1": "본문 작성자",
    "댓글2": "댓글풀 A2",
    "대댓글2": "본문 작성자",
    "댓글3": "댓글풀 A3",
    "대댓글3": "본문 작성자",
    "댓글4": "댓글풀 A4",
    "대댓글4": "본문 작성자",
    "댓글5": "댓글풀 A5",
    "대댓글5": "본문 작성자",
}
#: 원고유형별로 갈리는 두 자리
ACCOUNT_ROLES_BY_TYPE: dict[str, dict[str, str]] = {
    "질문형": {"대대댓글2": "댓글2 계정(A2)", "대대대댓글2": "여분 댓글풀 계정(A6)"},
    "후기형": {"대대댓글2": "여분 댓글풀 계정(A6)", "대대대댓글2": "본문 작성자"},
}

#: 원고 한 건당 최대 시도 횟수 (본문·댓글 각각). 사용자 지시 2026-09-19,
#: 2026-09-21에 6 → 10으로 올렸다 (그래도 못 지키면 `ok: False`로 보고한다)
MAX_ATTEMPTS = 10

#: 기본 생성 방식. `single` = 본문 호출 + 댓글 호출(2번), `combined` = 한 번에(1번).
#: 명령에서 `한번에` 라고 적으면 combined 로 바뀐다.
DEFAULT_MODE = "single"

#: 글자 수 허용 배수 — 본문·댓글 **모두** 상한의 200%까지는 통과다.
#: 사용자 지시(2026-09-21): "글자 수 제한은 상한의 200%까지 허용하고 글 완성도를
#: 먼저 높이자." 그 이하는 경고조차 내지 않는다 (경고가 재시도를 부르지 않게).
LENGTH_TOLERANCE = 2.0
SOFT_LIMIT_FACTOR = 2.0

#: 결과물에 새어 나오면 안 되는 작업용 내부 용어 (평가 2026-09-19 공통문제 7)
#: "규칙"은 뺐다 — "근무 불규칙한" 같은 평범한 말에 걸려 멀쩡한 원고를 떨어뜨렸다
#: (사용자 지시 2026-09-21).
INTERNAL_TERMS: tuple[str, ...] = (
    "프레임",
    "페르소나",
    "키워드",
    "본문",
    "원고",
    "댓글1",
    "대댓글",
)

#: 브랜드를 꺼낸 뒤 스스로 힘을 빼는 말 (평가 2026-09-19 공통문제 2)
RETREAT_PHRASES: tuple[str, ...] = (
    "제품보다",
    "제품얘기랑은별개로",
    "제품얘기가아니라",
    "제품이라기보단",
    "관리법개념",
    "결국은습관",
    "개인차",
    "참고만",
    "헷갈리지마시고",
    "따로검색",
)

#: 대대댓글2(질문형에서 제품을 처음 꺼내는 자리)에 나오면 **바로 실패**인 말
#: (설계서 C. 기존 논리·흐름이 무너지는 표현들)
REPLY2_BANNED_PHRASES: tuple[str, ...] = (
    "제품보다",
    "제품얘기랑은별개로",
    "관리법개념",
    "프레임",
    "헷갈리지마시고",
    "방법이중요",
)

#: 대대댓글2 길이 (사용자 지시 2026-09-21: 60자 미만만 탈락, 상한은 200%까지 허용)
REPLY2_MIN_LEN = 60
REPLY2_MAX_LEN = 110

#: 대대댓글2 근거 문장에 들어가야 하는 낱말 (권위 / 검증 / 원리)
REPLY2_EVIDENCE_WORDS: tuple[str, ...] = (
    "농촌진흥청",
    "국가기관",
    "정부기관",
    "기관",
    "검증",
    "인증",
    "논문",
    "임상",
    "실험",
    "연구",
    "의사",
    "원장",
    "산부인과",
    "항문외과",
    "이비인후과",
    "피부과",
    "병원",
    "클리닉",
    "성분",
    "원리",
    "방식",
    "작용",
    "흡수",
    "강화",
    "정돈",
    "막아",
    "막아주",
    "보호막",
    "근육",
    "배합",
    "멜라닌",
    "각질",
    "기도",
    "비강",
    "새싹",
    "추출",
    "지방",
    "체지방",
    "배출",
    "붙잡",
    "잡아주",
)

#: 대대댓글2 "대안의 한계" 문장에 들어가야 하는 낱말
REPLY2_LIMIT_WORDS: tuple[str, ...] = (
    "그냥",
    "만으로는",
    "만있",
    "한계",
    "잠깐",
    "도루묵",
    "제자리",
    "소용없",
    "겉돌",
    "안돼",
    "안되",
    "재발",
    "그때뿐",
    "금방다시",
    "다시올라",
    "또막",
    "참는",
    "참기",
    "아무거나",
    "단순",
    "있어야",
    "없으면",
    "없는건",
    "없는게",
    "의미없",
    "결국",
    "억지로",
    "그대로",
    "떼면",
    "둘다",
    "아니라",
    "달라",
    "다르",
    "굶",
    "덜",
    "똑같",
)

#: 대대댓글2 검색 유도에 인정하는 낱말
REPLY2_SEARCH_WORDS: tuple[str, ...] = ("검색", "찾아보")

#: 팥순이 후기형 대대댓글2의 최소 감량 수치 (사용자 지시 2026-09-21)
REVIEW_MIN_KG = 8

#: 감량 수치를 찾는 표현 (`8kg` `8키로` `8 킬로`)
_KG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kg|KG|Kg|키로|킬로|킬로그램)")

#: 후기형 **댓글**의 감량 수치 범위 (정리본 `팥순이 후기형.md` 절대 규칙 4번:
#: "숫자는 매번 다르게 (3~10kg, 3~8주 범위)"). 본문 마무리(10~12kg)와는 다른 값이다.
REVIEW_COMMENT_MIN_KG = 3.0
REVIEW_COMMENT_MAX_KG = 10.0

#: 후기형 댓글5의 역할 = **요요 고민 토로** (정리본 `팥순이 후기형.md`:
#: "요요 때문에 진짜 몇 번을 실패했는지 모르겠어요" / "'요요 때문에 실패' 기본 유지").
REVIEW_C5_YOYO_WORDS: tuple[str, ...] = ("요요",)
REVIEW_C5_FAIL_WORDS: tuple[str, ...] = (
    "실패",
    "몇번",
    "여러번",
    "번이나",
    "다시쪄",
    "또쪄",
    "되돌아",
    "도루묵",
)

#: 본문 문단 끝에 붙이는 한글 이모티콘 (정리본 `팥순이 후기형.md`:
#: "문단 끝에 한글 이모티콘 추가 (ㅋㅋ, ㅎㅎ, ㅠㅠ, !! 등)")
PARAGRAPH_TAIL_MARKS: tuple[str, ...] = ("ㅠ", "ㅜ", "ㅋ", "ㅎ", "!!", "!")

#: 요요를 말하면 기간과 kg 을 함께 적는다 (정리본 11번: "[기간 명시 + 명확한 kg]",
#: "요요가 와서 쪘다고 하는 부분에 모두 적용할 것")
_PERIOD_RE = re.compile(
    r"(\d+\s*(?:주|달|개월|년|일))|((?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:주|달|개월|년))"
)

#: 댓글3(바이럴 4종)이 반드시 담아야 하는 **해결 경험 / 도움이 된 노력**
#: (정리본 `코숨핏.md` 등: "해결된 경험을 공유한다 ... 어떤 노력이 필요했는지 알려준다")
COMMENT3_SOLUTION_WORDS: tuple[str, ...] = (
    "해결",
    "괜찮아졌",
    "나아졌",
    "좋아졌",
    "줄었",
    "덜해졌",
    "효과",
    "도움",
    "덕분",
    "해보니",
    "하고나서",
    "하니까",
    "바꾸니",
    "챙기니",
    "지키니",
    "버릇",
    "습관",
    "루틴",
)

#: 근거 출처로 쓰이는 **기관** 낱말 (두 개를 겹쳐 붙이면 출처가 불분명해진다)
AUTHORITY_PLACE_WORDS: tuple[str, ...] = (
    "수면클리닉",
    "이비인후과",
    "산부인과",
    "항문외과",
    "피부과",
    "내과",
    "한의원",
    "대학병원",
    "종합병원",
    "보건소",
    "농촌진흥청",
    "식약처",
    "국가기관",
    "정부기관",
)
#: 근거 출처로 쓰이는 **문헌·사람** 낱말
AUTHORITY_OTHER_WORDS: tuple[str, ...] = (
    "논문",
    "연구",
    "학회",
    "저널",
    "특허",
    "임상",
    "실험",
    "의사",
    "전문의",
    "원장",
    "교수",
    "박사",
)

#: 고민을 말하는 문장 (여기에 ㅋㅋ ㅎㅎ 가 붙으면 정서가 어긋난다, 2026-09-22)
WORRY_WORDS: tuple[str, ...] = (
    "걱정",
    "불안",
    "무섭",
    "허무",
    "속상",
    "막막",
    "답답",
    "우울",
    "지쳐",
    "지치",
    "힘들",
    "고민",
    "안빠져",
    "안빠지",
    "그대로예요",
    "그대로에요",
    "재발",
    "실패",
    "멘붕",
    "눈물",
    "포기",
)
#: 고민 문장에 붙으면 안 되는 웃음 표기 (ㅠㅠ 는 **있으면 좋을 뿐 필수가 아니다**,
#: 사용자 결정 2026-09-22 — 검증기는 ㅋㅋ ㅎㅎ 가 붙었는지만 본다)
WORRY_LAUGH_RE = re.compile(r"[ㅋㅎ]+")

# --- 의도적 오탈자·띄어쓰기 (정리본 [AI 티 제거] 규칙, 사용자 결정 2026-09-22) -------
#: 정리본이 못 박은 규칙: "본문에 띄어쓰기 2~3개, 맞춤법 1~2개를 의도적으로 틀린다".
#: 하나도 없으면 글이 너무 반듯해서 AI 티가 난다 → **경고**로만 알린다 (원고는 버리지 않는다).
#: 온라인에서 다들 틀리는 맞춤법 (정리본의 보기 그대로: 되/돼, 웬/왠, 에요/예요 …)
COMMON_TYPO_WORDS: tuple[str, ...] = (
    # 되/돼 (정리본 보기: 안돼요 → 안되요)
    "안되요",
    "안되서",
    "되요",
    "됬",
    "됀",
    # 웬/왠 (정리본 보기)
    "왠만",
    "웬지",
    # 에요/예요 혼용 (정리본 보기)
    "거에요",
    "네요에요",
    "이예요",
    # 않/안 뒤바뀜
    "않하",
    "않되",
    # 구어체 표기 흔들림 (`~더라구요` 는 이미 모든 원고가 쓰는 말이라 빼고 센다)
    "네여",
    "구여",
    "맞아욬",
    "그쵸",
    "어떻해",
    "금새",
    "몆",
)
#: 온라인에서 다들 **붙여 쓰는** 자리 (원래는 띄어야 맞다 — 정리본 보기: 할수있어)
COMMON_SPACING_WORDS: tuple[str, ...] = (
    "할수",
    "볼수",
    "갈수",
    "될수",
    "먹을수",
    "그때뿐",
    "한번더",
    "몇번이나",
    "안빠져",
    "안빠지",
    "잘안",
    "못참",
    "다시안",
    "제일많",
    "진짜많",
)
#: 본문에서 이 개수 미만이면 경고 (0개 = 하나도 안 틀렸다)
TYPO_MIN_COUNT = 1

#: 검증 표에는 남기되 **다시 시키지는 않는** 항목의 구간 이름.
#: 본문·댓글 재시도 고리는 `violations(scope="본문")` `violations(scope="댓글")` 만
#: 보므로, 이 구간에 둔 항목은 모델을 다시 부르지 않는다 (토큰 절약).
INFO_SCOPE = "참고"

#: 프롬프트에 박는 규칙 한 줄 — 일부러 틀리기
INTENTIONAL_TYPO_RULE = (
    "본문은 **일부러 조금 틀리게** 쓴다 (정리본 [AI 티 제거] 규칙)."
    " 온라인에서 다들 안 지키는 맞춤법을 1~2개"
    " (되/돼 → `안되요`, 웬/왠 → `왠지`, 에요/예요 혼용),"
    " 띄어쓰기를 2~3개 (`할 수 있어` → `할수있어`, `그 때뿐` → `그때뿐`)"
    " 자연스럽게 틀린다. 글이 너무 반듯하면 AI가 쓴 티가 난다"
)

# --- 마이너스 카피 근거 (사용자 결정 2026-09-22) -------------------------------
#: 근거 문장을 **"이게 없으면 효과가 없다"** 꼴로 쓰는 브랜드.
#: 플러스 카피(`배합이랑 원물 함량을 따져 만들어서 흡수가 잘된대요`)는 광고문으로 읽히고,
#: 마이너스 카피(`배합이랑 원물 함량을 안 따지면 흡수가 안 돼서 소용없대요`)는
#: 안 사면 손해라는 쪽으로 읽힌다.
MINUS_COPY_BRANDS: tuple[str, ...] = ("장으뜸", "뉴더미스")
#: 앞쪽 부정형 (`없으면` / `안 …` / `못 …`)
MINUS_COPY_NEGATIVE_WORDS: tuple[str, ...] = (
    "없으면",
    "없이",
    "없는건",
    "없는게",
    "없다면",
    "안따지",
    "안보고",
    "안맞추",
    "안챙기",
    "안들어",
    "못따지",
    "못챙기",
    "못맞추",
)
#: 뒤쪽 "그래서 소용없다"
MINUS_COPY_USELESS_WORDS: tuple[str, ...] = (
    "소용없",
    "소용이없",
    "효과없",
    "효과가없",
    "안돼",
    "안되",
    "의미없",
)
#: 프롬프트에 박는 규칙 한 줄 — 마이너스 카피
MINUS_COPY_RULE = (
    "대대댓글2의 **근거 문장은 마이너스 카피**로 쓴다 —"
    " `이게 있어서 좋다`가 아니라 **`이게 없으면 소용없다`** 로 적는다."
    " 나쁜 보기) 배합이랑 원물 함량을 따져 만들어서 흡수가 잘된대요."
    " 좋은 보기) 배합이랑 원물 함량을 안 따지면 흡수가 안 돼서 소용없대요"
)

#: 프롬프트에 박는 규칙 한 줄 — 근거 출처는 하나만
ONE_SOURCE_RULE = (
    "근거의 출처는 **하나만** 쓴다"
    " (나쁜 보기: `수면클리닉 이비인후과 의사 논문에` — 기관·사람·문헌을 겹쳐 붙이면"
    " 출처가 불분명해지고 꾸며 낸 티가 난다. 좋은 보기: `이비인후과에서 들었는데`)"
)
#: 프롬프트에 박는 규칙 한 줄 — 권위 근거는 원고 전체에서 한 번만
ONE_AUTHORITY_PER_MANUSCRIPT_RULE = (
    "의사·논문·기관·클리닉 같은 **권위 근거는 원고 전체에서 한 번만** 쓴다."
    " 댓글2에서 이미 썼으면 대대댓글2에서는 성분·원리·직접 겪은 체감으로 설득한다"
    " (같은 권위를 두 번 꺼내면 짜고 친 티가 난다)"
)
#: 프롬프트에 박는 규칙 한 줄 — 검색 유도 마무리를 습관처럼 붙이지 않는다
SEARCH_VARIETY_RULE = (
    "검색 유도 마무리는 **문장 형태를 매번 바꾼다**."
    " `후기 많아요` 를 기본으로 붙이지 말고 `한번 검색해보세요` `찾아보시면 나와요`"
    " 처럼 그때그때 다르게 끝낸다"
)
#: 프롬프트에 박는 규칙 한 줄 — 카페 구어체(-더라구요)
DEORAGUYO_RULE = (
    "겪은 일을 말할 때는 카페 구어체 `~더라구요` 를 살린다"
    " (`허무했어요` 보다 `허무하더라구요`, `효과 없었어요` 보다 `효과가 없더라구요`)"
)

#: 프롬프트에 박는 규칙 한 줄 — 댓글3은 해결 경험까지 쓴다.
#: (2026-09-22 재시도 원인 집계 1위 항목. 예전 규칙은 "한 마디 덧붙인다" 라고만 해서
#: 모델이 고충만 쓰고 끝내는 일이 잦았다 → 어떤 낱말이 들어가야 하는지까지 못 박는다)
COMMENT3_SOLUTION_RULE = (
    "댓글3은 고충만 말하고 끝내면 **다시 쓴다**."
    " `~해보니 ~나아졌어요` 처럼 해결된 경험이나 도움이 된 노력을 반드시 한 마디 붙인다"
    " (나아졌 / 좋아졌 / 줄었 / 괜찮아졌 / 도움 / 덕분 / 해보니 / 챙기니 / 바꾸니 중"
    " 하나는 들어가야 한다). 나쁜 보기) 저도 똑같아서 속상해요ㅠㅠ"
    " — 좋은 보기) 저도 그랬는데 각질부터 챙기니 훨 나아졌어요"
)
#: 프롬프트에 박는 규칙 한 줄 — 권위 근거 중복의 **판정 기준**까지 못 박는다
#: (2026-09-22 재시도 원인 집계 2위 항목)
AUTHORITY_WORD_RULE = (
    "권위 낱말(의사 / 전문의 / 원장 / 교수 / 논문 / 연구 / 임상 / 실험 / 학회 /"
    " 피부과 · 이비인후과 · 산부인과 · 항문외과 같은 진료과 / 수면클리닉 / 대학병원 /"
    " 농촌진흥청 · 식약처 · 국가기관 · 정부기관)은 **댓글2와 대대댓글2 중 한 곳에만** 쓴다."
    " 댓글2에서 `피부과` 를 썼으면 대대댓글2에는 이 목록의 낱말을 하나도 쓰지 말고"
    " 성분·원리·직접 겪은 체감으로만 쓴다"
)

#: 프롬프트에 박는 규칙 한 줄 — 고민 문장에는 ㅠㅠ
WORRY_LAUGH_RULE = (
    "고민·걱정을 말하는 문장(걱정돼요 / 안 빠져요 / 재발했어요 / 허무해요)에는"
    " ㅋㅋ ㅎㅎ 를 붙이지 않고 ㅠㅠ 를 쓴다"
    " (웃으면서 걱정하는 말은 사람이 쓴 말로 읽히지 않는다)."
    " ㅋㅋ ㅎㅎ 는 후기·마무리처럼 밝은 대목에서만 쓴다"
)
#: 특정 브랜드만 쓸 수 있는 근거 (다른 브랜드가 가져다 쓰면 거짓말이 된다).
#: 모델이 팥순이 근거(농촌진흥청 체지방 25%)를 장으뜸 댓글에 가져다 쓴 일이 있었다.
BRAND_ONLY_EVIDENCE: dict[str, tuple[str, ...]] = {
    "팥순이": ("농촌진흥청", "체지방25%", "체지방 25%", "25%", "25퍼", "25프로")
}

#: 웃음·감탄 표기 (ㅋㅋ ㅎㅎ ㅠㅠ). 문장에 붙은 꼬리로 본다
LAUGH_RE = re.compile(r"[ㅋㅎㅠㅜ]+")

#: 그 중 **근거 문장 옆에 붙으면 신뢰가 깎이는** 것 (사용자 지시 2026-09-22).
#: ㅠㅠ 는 웃는 것이 아니라 안타까움이라 근거 문장에서도 허용한다.
EVIDENCE_LAUGH_RE = re.compile(r"[ㅋㅎ]+")

#: 근거를 말하는 자리라 웃음 표기를 막는 댓글
EVIDENCE_COMMENT_LABELS: tuple[str, ...] = ("댓글2", "대대댓글2")

#: 문장 끝(서술 어미)으로 인정하는 마지막 글자
SENTENCE_ENDINGS: tuple[str, ...] = ("요", "죠", "용", "욬")

#: 조사·어미가 붙은 어절로 보는 마지막 글자 (이게 아니면 "맨 명사"로 본다)
_PARTICLE_TAIL = set(
    "은는이가을를에서의도만로과와랑고며면데라나니야해져진된인한할겨봐줘써들"
    "건걸것게거뿐때중째쯤씩큼퍼졌쳐혀줄"
)

#: 명사만 이만큼 줄줄이 붙으면 문장이 아니라 **낱말 나열**로 본다
BARE_NOUN_RUN_MAX = 3

#: 본문 한 줄 글자 수 상한 (지침의 "1줄 20자 안팎")
LINE_MAX = 28
#: 상한을 넘는 줄이 이 비율을 넘으면 **실패** (설계서 F-a)
LINE_OVER_RATIO = 0.20

#: 댓글5에서 수치로 인정하는 표현 (아라비아 숫자 또는 한글 수사)
_NUMBER_RE = re.compile(
    r"[0-9]|(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|스무)\s*(?:달|주|개월|번|일|kg|킬로|년)"
)

#: 후기형에서 작성자가 자기 제품을 두고 물으면 안 되는 말 (평가 2026-09-19 공통문제 11)
AUTHOR_QUESTION_TERMS: tuple[str, ...] = (
    "어디서사",
    "어디서파",
    "어디서구입",
    "어디서구매",
    "어떤제품",
    "제품이름",
    "어떤성분",
    "성분이어떻게",
    "어떤방법",
    "무슨성분",
    "효과있나",
    "효과어때",
    "효과가어떤",
    "얼마나빠지",
)

#: 본문 작성자가 쓰는 라벨 (원고유형별)
def author_labels(manuscript_type: str) -> tuple[str, ...]:
    """해당 원고유형에서 **본문 작성자**가 쓰는 댓글 라벨."""
    roles = dict(ACCOUNT_ROLES_COMMON)
    roles.update(ACCOUNT_ROLES_BY_TYPE.get(manuscript_type or "질문형", {}))
    return tuple(label for label in COMMENT_LABELS if roles.get(label) == "본문 작성자")


#: 본문에서 **상황 연출**로 보는 말 (사용자 지시 2026-09-21).
#: "글 쓰는 장소·시간·상태"를 적는 순간 실제 회원 글이 아니게 된다.
#: 공백을 지운 본문에서 찾으므로 띄어쓰기가 달라도 걸린다.
SCENE_BANNED_PHRASES: tuple[str, ...] = (
    "글남겨",
    "글남기",
    "글써요",
    "글써봐",
    "글씁니다",
    "글써봅니다",
    "글쓰는중",
    "글쓰다가",
    "글올려요",
    "글올려봐요",
    "올려봐요",
    "남겨봐요",
    "남겨봅니다",
    "한산해",
    "한산한",
    "잠깐앉아",
    "앉아서써",
    "쓰는중이에요",
    "쓰는중입니다",
    "끄적",
    "적어봐요",
    "적어보는",
    "적어봅니다",
    "적어놓고가",
    "써보는중",
)

#: 본문 프롬프트에 박아 두는 **절대 규칙** (페르소나·상황 연출 폐지, 2026-09-21)
NO_SCENE_RULE = (
    "<절대 규칙 — 글 쓰는 상황을 연출하지 않는다>\n"
    "- 기존 완성 원고처럼 **문제 상황(증상·고민)부터 바로** 시작한다\n"
    "- 글을 쓰는 장소·시간·상태를 묘사하지 않는다\n"
    "- 다음 같은 말은 쓰는 순간 그 글은 버린다:"
    " ~하다가 글 남겨요 / 가게가 한산해서 / 잠깐 앉아서 / 쓰는 중이에요 /"
    " 끄적여봐요 / 적어봐요 / 적어보는 중 / 올려봐요\n"
    "- 나이 직업 가족 구성 같은 인물 설정을 굳이 꺼내지 않는다"
    " (고민 자체를 말하다 자연스럽게 드러나는 것만 남긴다)"
)


@dataclass(frozen=True)
class ReplyLogic:
    """대대댓글2가 **반드시 담아야 하는 논리** (브랜드마다 다르다).

    사용자 지시 2026-09-21: 장으뜸·뉴더미스는 기존 원고의 논리로 통일한다.
    - `words` 중 `min_hits` 개 이상이 들어가야 하고
    - `also` 가 있으면 그 중 하나는 반드시 들어가야 한다
    - `note` 는 프롬프트에 그대로 적는 한국어 설명이다
    """

    words: tuple[str, ...]
    min_hits: int = 2
    also: tuple[str, ...] = ()
    note: str = ""


#: 골든 문장(사용자가 고른 본보기)을 담아 둔 설정 파일 이름
GOLDEN_CONFIG = "reply2_golden"


def load_golden_reply2(
    brand: str, manuscript_type: str = "질문형", data: Any = None
) -> list[str]:
    """`config/reply2_golden.yaml` 에 적어 둔 그 브랜드의 골든 대대댓글2.

    파일이 없거나 그 브랜드 자리가 비어 있으면 빈 목록이라 예전처럼 돈다.
    """
    if data is None:
        try:
            from v2r.config import load_yaml

            data = load_yaml(GOLDEN_CONFIG)
        except Exception:  # pragma: no cover - 설정을 못 읽어도 생성은 계속한다
            return []
    table = (data or {}).get(brand)
    if not isinstance(table, dict):
        return []
    items = table.get((manuscript_type or "").strip() or "질문형")
    if isinstance(items, str):
        items = [items]
    return [str(t).strip() for t in (items or []) if str(t).strip()]


class BrandWriteError(RuntimeError):
    """브랜드 원고 생성/검증 실패."""


@dataclass(frozen=True)
class BrandRule:
    """브랜드(+원고유형)별 작성 규칙. 지침에서 뽑은 값만 담는다."""

    brand: str
    manuscript_type: str = "질문형"
    #: 댓글에서 최초로 꺼내는 제품명
    product: str = ""
    #: 댓글에서 제품명을 이렇게 표기한다 (팥순이는 `팥순ㅇㅣ`)
    product_in_comment: str = ""
    #: 본문에 절대 들어가면 안 되는 낱말 (브랜드명·제품명 등)
    banned_in_body: tuple[str, ...] = ()
    target: str = ""
    one_thing: str = ""
    authority: str = ""
    body_max: int = 250
    keyword_count: int = 3
    #: 댓글2·대대댓글2를 뺀 나머지 댓글의 글자 수 상한 (설계서 E)
    root_max: int = 40
    #: 댓글2 글자 수 상한 (설계서 E)
    comment2_max: int = 90
    #: 대대댓글2 글자 수 상한 (제품을 꺼내는 자리라 조금 길게 준다, 설계서 E)
    reply2_max: int = REPLY2_MAX_LEN
    #: True면 상한 초과는 경고, 상한의 1.5배를 넘겨야 실패로 본다
    soft_limits: bool = True
    #: `{키워드}`를 몇 번째 문단 뒤에 둘지
    placeholder_after_paragraph: int = 2
    #: 추가 자리표시자 (팥순이 후기형의 `{B/A}`)
    extra_placeholder: str = ""
    #: 제품명이 처음 등장해도 되는 라벨
    first_mention_label: str = "대대댓글2"
    #: 대대댓글2가 반드시 담아야 하는 브랜드 고유 논리 (없으면 검사하지 않는다)
    reply2_logic: ReplyLogic | None = None
    #: 지침이 **위치까지 정해 둔** 필수 멘트. `(라벨, (인정되는 표현들,))`
    #: 같은 라벨에 여러 줄을 둘 수 있고, 한 줄 안의 표현 중 하나만 있으면 통과다.
    required_phrases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: 본문 한 줄 글자 수 상한 (모바일 줄 나눔)
    line_max: int = LINE_MAX
    #: 댓글3이 "해결된 경험·도움이 된 노력"을 담아야 하는가 (바이럴 4종 지침).
    #: 팥순이는 댓글3이 **키워드 효과 질문**이라 검사하지 않는다.
    comment3_needs_solution: bool = True
    body_structure: tuple[str, ...] = ()
    body_notes: tuple[str, ...] = ()
    comment_notes: tuple[str, ...] = ()

    @property
    def extra_placeholder_after_paragraph(self) -> int:
        """추가 자리표시자를 몇 번째 문단 뒤에 둘지.

        `body_structure`에 그 토큰이 적혀 있으면(예: 팥순이 후기형의
        "병행 효과 (여기에 {B/A} 표시)") 그 항목 번호를 쓰고, 없으면 맨 뒤다.
        """
        token = self.extra_placeholder
        if not token:
            return 0
        for i, item in enumerate(self.body_structure, start=1):
            if token in item:
                return i
        return len(self.body_structure) or 99

    @property
    def comment_max(self) -> int:
        """예전 이름(`comment_max`)으로도 읽히게 둔 별칭."""
        return self.root_max

    @property
    def key(self) -> str:
        return f"{self.brand}/{self.manuscript_type}"

    @property
    def comment_label_of_product(self) -> str:
        return self.first_mention_label


_VIRAL_STRUCTURE = (
    "오프닝: 지금 겪고 있는 문제(증상·고민)를 첫 줄부터 바로 꺼낸다"
    " (글 쓰는 상황이나 인물 소개로 시작하지 않는다)",
    "솔루션: 문제가 생긴 과정 → 예전에 해봤지만 효과 없던 노력 → 아직 남은 고민",
    "클로징: 고민을 한 번 더 짚고 해결 방법을 알려달라는 조언 요청으로 끝낸다",
)
_VIRAL_BODY_NOTES = (
    "제목은 작성 키워드로 시작하고 공백 포함 30자 이내 한 문장 물음형으로 쓴다",
    "제목에 공감을 부르는 우는 이모티콘(ㅠㅠ)을 섞는다",
    "모바일 기준으로 한 줄 20자 이내 한 문단 4줄 이내로 문단을 자주 나눈다",
    "제품명이나 브랜드명을 본문에서 절대 말하지 않는다 (댓글에서 나오게 남겨 둔다)",
    WORRY_LAUGH_RULE,
    DEORAGUYO_RULE,
    # 정리본 [AI 티 제거] 규칙 (사용자 결정 2026-09-22)
    INTENTIONAL_TYPO_RULE,
)
_VIRAL_COMMENT_NOTES = (
    "댓글1은 제품과 무관하게 작성자의 질문에 답하는 중립적 정보다 (광고 아님을 증명)",
    "대댓글1은 작성자가 처음 알게 된 듯한 반응만 쓴다 (정보를 아는 척 공유하면 안 된다)",
    "댓글1과 댓글2 안에는 작성 키워드가 한 글자도 빠짐없이 들어가야 한다",
    "댓글2는 권위재를 근거로 원씽 이동을 제시한다 제품명은 절대 쓰지 않는다",
    "대댓글2는 작성자가 제품이 아니라 성분이나 방법을 되묻는 질문이다",
    "대대댓글2에서 제품명을 처음 꺼내고 쓰게 된 계기와 느낀 변화 한 가지를 적은 뒤"
    " 다른 방법은 왜 안 되는지 한 마디 덧붙이고 검색해보라고 유도한다",
    "대대대댓글2는 제3자가 222 저도 효과 봤어요 식으로 여론을 만든다",
    "댓글3은 비슷한 고민을 말한 뒤 **해결된 경험이나 도움이 된 노력**을 한 마디 덧붙인다"
    " (고충만 늘어놓고 끝내지 않는다) 대댓글3은 맞장구다",
    "댓글4는 작성자가 시도한 잘못된 방법을 가볍게 짚어 준다 (두 문장 내외)",
    "댓글5는 제3자 여론 보강이다 수치와 기간을 구체적으로 쓰고 다른 제품을 언급하지 않는다",
    "대댓글5는 효과에 호기심을 보이는 반응이다",
    # --- 2026-09-22 GPT 교차 검증 반영 (docs/reports/codex-crosscheck-2026-09-22.md)
    ONE_SOURCE_RULE,
    ONE_AUTHORITY_PER_MANUSCRIPT_RULE,
    SEARCH_VARIETY_RULE,
    WORRY_LAUGH_RULE,
    DEORAGUYO_RULE,
    # --- 2026-09-22 재시도 원인 집계 1·2위 항목을 한 줄로 못 박는다 (토큰 절약)
    COMMENT3_SOLUTION_RULE,
    AUTHORITY_WORD_RULE,
)


#: 후기형은 댓글2 스레드 역할이 질문형과 반대라 그 세 줄을 빼고 쓴다
_REVIEW_COMMENT_BASE = tuple(
    n
    for n in _VIRAL_COMMENT_NOTES
    if not n.startswith(("대댓글2", "대대댓글2", "대대대댓글2"))
)


def _viral_rule(**kwargs: Any) -> BrandRule:
    """바이럴 4종(우아덤·장으뜸·코숨핏·뉴더미스) 공통값으로 규칙을 만든다."""
    base: dict[str, Any] = {
        "body_structure": _VIRAL_STRUCTURE,
        "body_notes": _VIRAL_BODY_NOTES,
        "comment_notes": _VIRAL_COMMENT_NOTES,
    }
    base.update(kwargs)
    notes = base.pop("extra_comment_notes", ())
    if notes:
        base["comment_notes"] = tuple(base["comment_notes"]) + tuple(notes)
    extra_body = base.pop("extra_body_notes", ())
    if extra_body:
        base["body_notes"] = tuple(base["body_notes"]) + tuple(extra_body)
    return BrandRule(**base)


#: 브랜드(+원고유형)별 규칙표
BRAND_RULES: dict[str, BrandRule] = {}


def _register(rule: BrandRule) -> None:
    BRAND_RULES[rule.key] = rule


_register(
    _viral_rule(
        brand="우아덤",
        product="그린커피 아하바하",
        banned_in_body=("그린커피 아하바하", "아하바하", "그린커피"),
        target="착색 미백 색소침착으로 고민하는 타겟",
        one_thing=(
            "착색된 부위는 각질이 두꺼워 미백 성분이 흡수되지 않는다 "
            "각질 정돈(AHA·BHA)과 멜라닌 억제(그린커피 추출물)가 같이 되어야 한다"
        ),
        authority="피부과 의사 레이저 시술 후 관리 논문 카페 추천",
    )
)
_register(
    _viral_rule(
        brand="장으뜸",
        product="장으뜸 장어즙",
        banned_in_body=("장으뜸", "장어즙"),
        target="난임 유산 자연임신을 바라는 임신 준비 타겟",
        one_thing="기력이 좋아야 착상이 잘 된다 내 몸에 실제로 작용하는 것이 중요하다",
        authority="산부인과 원장님 난임 카페 추천 가족 권유",
        # 사용자 지시 2026-09-21: 기존 원고 논리로 통일한다
        reply2_logic=ReplyLogic(
            words=("흡수", "작용", "배합", "함량"),
            min_hits=2,
            note="아무거나 먹으면 흡수나 작용이 안 돼서 소용없고"
            " 배합과 원물 함량을 보고 골라야 한다는 논리로만 쓴다",
        ),
        extra_comment_notes=(
            "대댓글2에는 제품 이라는 낱말을 절대 쓰지 않는다 성분이나 방법으로 묻는다",
            "대대대댓글2에는 장으뜸 이라는 브랜드명을 쓰지 않는다 (효과 여론만 남긴다)",
            "댓글2는 방법을 알게 된 뒤 임신에 성공했다는 확실한 효과로 마무리한다",
        ),
    )
)
_register(
    _viral_rule(
        brand="코숨핏",
        product="코숨핏",
        banned_in_body=("코숨핏", "광고"),
        target="코골이 비염으로 고민하는 타겟",
        one_thing="기도 근육을 강화해야 코골이와 비염이 잡힌다",
        authority="수면클리닉 이비인후과 의사 논문",
        extra_body_notes=(
            "코골이 소재라면 글쓴이는 반드시 코를 고는 본인이 아니라 배우자나 여자친구 시점이다",
            "본문에 광고 라는 낱말을 절대 쓰지 않는다 (카페 필터에 걸린다)",
        ),
    )
)
_register(
    _viral_rule(
        brand="뉴더미스",
        product="자연방패 항문세정제",
        banned_in_body=("자연방패", "뉴더미스", "항문세정제"),
        target="치질 항문 가려움 통증 사타구니 습진으로 고민하는 타겟",
        one_thing="항문 보호막을 지켜 주는 관리가 근본 해결책이다",
        authority="항문외과 의사 치질 카페 추천",
        # 사용자 지시 2026-09-21: 기존 원고 논리로 통일한다
        reply2_logic=ReplyLogic(
            words=("보호막", "강화", "성분"),
            min_hits=2,
            also=("소용없", "의미없", "한계", "그때뿐", "재발", "금방다시"),
            note="보호막 강화 성분이 없으면 그냥 씻기만 하는 거라 소용없다는 논리로만 쓴다",
        ),
        extra_comment_notes=(
            "항문외과 의사가 알려줬다 는 표현을 반복해서 쓰지 않는다 권위재를 매번 다르게 고른다",
        ),
    )
)

#: 팥순이 질문형 — 다이어트 4단계 구조, 본문 200자, 댓글 전부 50자
_register(
    BrandRule(
        brand="팥순이",
        manuscript_type="질문형",
        product="팥순추출물 다이어트",
        product_in_comment="팥순ㅇㅣ",
        banned_in_body=("팥순이",),
        target="체중 감량 다이어트를 하는 타겟",
        one_thing="팥순추출물이 붓기가 아니라 체중 감량에 작용한다",
        authority="다이어트 카페 추천 후기",
        body_max=200,
        keyword_count=3,
        comment3_needs_solution=False,
        root_max=50,
        comment2_max=90,
        reply2_max=REPLY2_MAX_LEN,
        placeholder_after_paragraph=2,
        # 질문형 지침: 대대댓글2에 "농촌진흥청 / 체지방 25% 감소 검증"을 반드시 넣는다
        required_phrases=(
            ("대대댓글2", ("농촌진흥청", "국가기관")),
            ("대대댓글2", ("25%", "25퍼", "25프로")),
        ),
        body_structure=(
            "1단계 다이어트 중 고민 상황 (50자)",
            "2단계 키워드를 접하게 된 계기 (50자)",
            "3단계 핵심 궁금증 (50~60자)",
            "4단계 후기 요청 멘트 (40~50자)",
        ),
        body_notes=_VIRAL_BODY_NOTES
        + (
            "붓기와 직접 이어지는 키워드가 아니면 붓기보다 체중 감량 다이어트 내용으로 쓴다",
            "본문에서는 팥순이 라고 절대 쓰지 않는다",
            "반드시 다이어트와 관련된 질문을 본문에 적는다",
        ),
        comment_notes=_VIRAL_COMMENT_NOTES
        + (
            "모든 댓글은 50자를 넘지 않는다",
            "제품은 대대댓글2에서만 팥순ㅇㅣ 로 표기해 처음 꺼낸다 (팥순이 라고 붙여 쓰지 않는다)",
            "팥순추출물 이라고 반드시 언급한다 다른 방법 새로운 방법 같은 간접 표현은 쓰지 않는다",
            "네이버 를 언급하지 않는다",
            "팥순이 제품인데요 팥순이 검색하시면 같은 고정 표현을 쓰지 않는다",
        ),
    )
)

#: 팥순이 후기형 — 작성자가 이미 써 본 사람. 첫 문단 뒤 {키워드}, 병행 효과에 {B/A}
_register(
    BrandRule(
        brand="팥순이",
        manuscript_type="후기형",
        product="팥순추출물 다이어트",
        product_in_comment="팥순ㅇㅣ",
        banned_in_body=("팥순이",),
        target="체중 감량 다이어트를 하는 타겟",
        one_thing="키워드 단독으로는 한계가 있고 팥순추출물 다이어트를 병행해야 한다",
        authority="다이어트 카페 추천 후기",
        body_max=300,
        keyword_count=4,
        comment3_needs_solution=False,
        root_max=50,
        comment2_max=90,
        reply2_max=REPLY2_MAX_LEN,
        placeholder_after_paragraph=1,
        extra_placeholder="{B/A}",
        first_mention_label="대댓글2",
        # 후기형 지침이 자리까지 정해 둔 멘트
        # 대대댓글2 = 체지방 25% 검증 근거 / 대대대댓글2 = 국가기관 인증 / 댓글4 = 바로 구매
        required_phrases=(
            ("대대댓글2", ("25%", "25퍼", "25프로")),
            ("대대대댓글2", ("국가기관", "농촌진흥청")),
            ("댓글4", ("구매했", "주문했", "강추")),
        ),
        body_structure=(
            "키워드 단독 사용 후기로 시작",
            "느낀 점과 아쉬운 점",
            "팥순추출물 다이어트를 발견한 계기",
            "병행 효과 (여기에 {B/A} 표시)",
            "같은 고민을 하는 사람에게 추천하며 마무리",
        ),
        body_notes=(
            "제목에 팥순이 를 쓰지 않고 이거 이것만 같은 대명사로 바꾼다",
            "제목은 공백 포함 30자 이내이고 물음형 후기형을 섞어 궁금증을 만든다",
            "이것만 추가했더니 두 달째 같은 특정 문구를 반복하지 않는다",
            "본문에서는 팥순추출물 또는 팥순추출물 다이어트 로만 쓰고 팥순이 는 절대 쓰지 않는다",
            "키워드에 대한 객관적 설명을 길게 늘어놓지 않는다",
            "모바일 기준으로 한 줄 20자 이내 한 문단 4줄 이내로 문단을 자주 나눈다",
            "마무리의 감량 수치는 -10.0kg~-12.0kg 사이에서 소수 첫째 자리까지 매번 다르게 쓴다",
            "문단은 반드시 ㅠㅠ ㅋㅋ ㅎㅎ !! 중 하나로 끝낸다 (문단 끝 한글 이모티콘)",
            "요요가 와서 다시 쪘다고 쓸 때는 기간과 명확한 kg 수치를 같이 적는다",
        ),
        comment_notes=_REVIEW_COMMENT_BASE
        + (
            "모든 댓글은 50자를 넘지 않는다",
            f"댓글의 감량 수치는 {REVIEW_COMMENT_MIN_KG:g}~{REVIEW_COMMENT_MAX_KG:g}kg,"
            " 기간은 3~8주 범위에서 매번 다르게 쓴다 (본문 마무리 수치와는 다른 범위다)",
            "댓글5는 요요 때문에 여러 번 실패했다는 토로다"
            " (정체기 경험만 쓰면 안 된다) 대댓글5는 꾸준한 루틴·체질 개선으로 받아 준다",
            "작성자는 이미 써 본 사람이라 대댓글2에서 작성자가 직접 팥순ㅇㅣ 를 꺼낸다",
            "작성자는 자기가 이미 쓴 제품을 두고 되묻지 않는다 (어디서 사요 어떤 성분이에요 금지)",
            "대대댓글2는 여분 댓글 계정이 저도 이거 먹는 중이라고 거든다"
            " 질문형과 똑같이 네 마디를 갖춘다:"
            " 저도 이거 먹는 중 → 기간·감량 수치와 국가기관 체지방 25% 검증 근거 →"
            " 식단만으로는 왜 안 되는지 한 문장 → 검색해보시면 후기 많아요 류 마무리",
            "대대대댓글2는 작성자가 맞장구치며 마무리한다",
            "네이버 를 언급하지 않는다",
        ),
    )
)


def rule_for(brand: str, manuscript_type: str = "") -> BrandRule:
    """브랜드(+원고유형) 규칙. 없으면 BrandWriteError."""
    mtype = (manuscript_type or "").strip() or "질문형"
    rule = BRAND_RULES.get(f"{brand}/{mtype}") or BRAND_RULES.get(f"{brand}/질문형")
    if rule is None:
        raise BrandWriteError(
            f"브랜드 규칙이 없습니다: {brand} (등록된 브랜드: "
            + ", ".join(sorted({r.brand for r in BRAND_RULES.values()}))
            + ")"
        )
    return rule


# ------------------------------------------------------------------ 세기
def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def strip_placeholders(text: str) -> str:
    """`{...}` 자리표시자를 지운 텍스트."""
    return re.sub(r"\{[^{}\r\n]*\}", "", text or "")


def body_length(body: str) -> int:
    """본문 글자 수 (공백 제외, 자리표시자 제외)."""
    return len(_squash(strip_placeholders(body)))


def keyword_hits(body: str, keyword: str) -> int:
    """본문에 키워드가 몇 번 들어갔는지 (공백 무시)."""
    needle = _squash(keyword)
    if not needle:
        return 0
    return _squash(strip_placeholders(body)).count(needle)


def paragraphs(body: str) -> list[str]:
    """빈 줄로 나눈 문단 목록."""
    return [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]


# --------------------------------------------------- 대대댓글2 문장 뜯어보기
def _bare_tail(word: str) -> str:
    """어절에서 꼬리표(!?~)를 뗀 몸통."""
    return (word or "").strip().strip("!?~.…,\"'()[]" + "ㅋㅎㅠㅜㅡㄷㄴㅇ")


def is_sentence_end(word: str) -> bool:
    """이 어절이 서술 어미로 끝나는가 (`~해요` `~거든요` `~더라구요`)."""
    body = _bare_tail(word)
    return bool(body) and body.endswith(SENTENCE_ENDINGS)


def reply2_sentences(text: str) -> list[str]:
    """댓글 한 줄을 **문장 단위**로 자른다.

    카페 댓글은 마침표를 안 쓰기 때문에 서술 어미(`~요` `~죠`)로 끊는다.
    마지막 조각이 어미로 끝나지 않으면 그 조각도 그대로 돌려준다
    (`reply2_structure_problems` 가 "끝을 못 맺었다"고 잡는다).
    """
    words = (text or "").split()
    out: list[str] = []
    buf: list[str] = []
    for word in words:
        # 홀로 떨어진 웃음 표기(ㅎㅎ ㅋㅋ ㅠㅠ)는 **앞 문장에 붙은 것**으로 본다
        if out and not buf and LAUGH_RE.fullmatch(word.strip()):
            out[-1] = out[-1] + " " + word
            continue
        buf.append(word)
        if is_sentence_end(word):
            out.append(" ".join(buf))
            buf = []
    if buf:
        out.append(" ".join(buf))
    return out


def max_bare_noun_run(text: str) -> int:
    """조사도 어미도 없는 맨 명사가 몇 개나 줄줄이 붙어 있는지.

    `참기만 하면 제자리 농촌진흥청 25% 검증 검색해봐요` 처럼 근거를 문장이
    아니라 **낱말 나열**로 적은 조각을 잡아내려고 센다.
    """
    run = 0
    best = 0
    for word in (text or "").split():
        body = _bare_tail(word)
        if not body:
            continue
        bare = body[-1] not in _PARTICLE_TAIL and not is_sentence_end(body)
        run = run + 1 if bare else 0
        best = max(best, run)
    return best


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    squashed = _squash(text)
    return any(_squash(w) in squashed for w in words)


def reply2_sentence_problems(
    text: str, product: str = "", min_sentences: int = 3, max_sentences: int = 4
) -> list[str]:
    """대대댓글2가 **문장으로 이어지는가**만 본다 (구조·낱말은 따로 본다).

    `product`를 주면 제품 이름은 낱말 나열 세기에서 뺀다 (`자연방패 항문세정제`
    처럼 이름 자체가 명사 두세 개인 브랜드가 억울하게 걸리지 않게).
    """
    problems: list[str] = []
    sentences = reply2_sentences(text)
    if not sentences:
        return ["문장 없음 — 빈 줄이다"]
    if not is_sentence_end(sentences[-1].split()[-1] if sentences[-1].split() else ""):
        problems.append("끝맺음 — 마지막 문장이 서술 어미(~요/~에요/~더라구요)로 끝나지 않는다")
    if not min_sentences <= len(sentences) <= max_sentences:
        problems.append(
            f"문장 수 — 완전한 문장 {min_sentences}~{max_sentences}개여야 하는데"
            f" {len(sentences)}개다"
        )
    short = [s for s in sentences if len(s.split()) < 2]
    if short:
        problems.append(f"조각 문장 — 어절이 하나뿐인 문장이 있다 (`{short[0]}`)")
    counted = text or ""
    if product:
        counted = counted.replace(product, " ")
    run = max_bare_noun_run(counted)
    if run > BARE_NOUN_RUN_MAX:
        problems.append(
            f"낱말 나열 — 조사 없는 명사가 {run}개 연달아 붙어 문장이 아니라 나열이다"
        )
    return problems


def is_evidence_sentence(sentence: str) -> bool:
    """이 문장이 **근거를 말하는 문장**인가 (권위기관·검증·성분·원리)."""
    return _has_any(sentence, REPLY2_EVIDENCE_WORDS)


def evidence_laughter_problems(text: str) -> list[str]:
    """근거 문장 옆에 붙은 웃음 표기 (사용자 지시 2026-09-22).

    `의사분들이 추천하는 보호막 강화 성분이 들어있어요 ㅋㅋㅋㅋ` 처럼
    권위·근거를 말하면서 웃어 버리면 그 말의 신뢰가 깎인다.
    웃음 표기는 **대안의 한계 문장이나 마무리(검색 유도)** 쪽에서만 쓴다.
    """
    out: list[str] = []
    for sentence in reply2_sentences(text):
        if not is_evidence_sentence(sentence):
            continue
        marks = EVIDENCE_LAUGH_RE.findall(sentence)
        if marks:
            out.append(
                f"근거 문장 웃음 표기 — `{sentence.strip()}` 에 {''.join(marks)} 가 붙었다"
                " (ㅋㅋ ㅎㅎ 는 대안의 한계나 마무리 문장에서만 쓴다. ㅠㅠ 는 괜찮다)"
            )
    return out


def strip_evidence_laughter(text: str) -> str:
    """근거 문장에 붙은 웃음 표기만 지운다 (문장 자체는 그대로 둔다)."""
    out: list[str] = []
    for sentence in reply2_sentences(text):
        if is_evidence_sentence(sentence):
            sentence = re.sub(r"\s*" + EVIDENCE_LAUGH_RE.pattern, "", sentence)
        out.append(sentence.strip())
    return " ".join(s for s in out if s)


def is_minus_copy_sentence(sentence: str) -> bool:
    """이 문장이 **마이너스 카피**인가 — `…이 없으면 … 소용없다` 꼴.

    앞쪽 부정형(`없으면` / `안 따지면` / `못 챙기면`)과
    뒤쪽 `소용없 / 효과 없 / 안 돼` 가 **한 문장 안에 같이** 있어야 한다.
    """
    squashed = _squash(sentence)
    has_negative = any(_squash(w) in squashed for w in MINUS_COPY_NEGATIVE_WORDS)
    has_useless = any(_squash(w) in squashed for w in MINUS_COPY_USELESS_WORDS)
    return has_negative and has_useless


def minus_copy_problems(text: str, rule: "BrandRule") -> list[str]:
    """장으뜸·뉴더미스 대대댓글2의 근거 문장이 마이너스 카피인가 (사용자 결정 2026-09-22).

    `배합이랑 원물 함량을 따져 만들어서 흡수가 잘된대요` (플러스) 는 광고문으로 읽힌다.
    `배합이랑 원물 함량을 안 따지면 흡수가 안 돼서 소용없대요` (마이너스) 로 써야
    "이게 없으면 효과가 없다" 는 쪽으로 읽힌다. **필수 위반**이라 다시 시킨다.
    """
    if rule.brand not in MINUS_COPY_BRANDS:
        return []
    evidence = [s for s in reply2_sentences(text) if is_evidence_sentence(s)]
    if not evidence:
        return []  # 근거 문장이 아예 없는 것은 `reply2_structure_problems` 가 잡는다
    if any(is_minus_copy_sentence(s) for s in evidence):
        return []
    return [
        f"마이너스 카피 — {rule.brand} 근거 문장은 `이게 없으면 소용없다` 꼴로 쓴다"
        f" (지금 근거 문장: `{evidence[0].strip()}`)."
        " 좋은 보기: 배합이랑 원물 함량을 안 따지면 흡수가 안 돼서 소용없대요"
    ]


def intentional_typo_hits(text: str) -> list[str]:
    """본문에서 찾은 **의도적 오탈자·띄어쓰기 붙임** 목록 (정리본 [AI 티 제거] 규칙)."""
    squashed = _squash(text)
    hits = [w for w in COMMON_TYPO_WORDS if w in squashed]
    # 띄어쓰기는 "원래 띄어야 하는데 붙여 썼다"를 봐야 하므로 공백을 지우지 않고 본다
    raw = text or ""
    hits += [w for w in COMMON_SPACING_WORDS if w in raw]
    return sorted(set(hits))


def intentional_typo_problems(body: str) -> list[str]:
    """하나도 안 틀렸으면 경고 (사용자 결정 2026-09-22).

    정리본은 "본문에 띄어쓰기 2~3개, 맞춤법 1~2개를 의도적으로 틀린다"고 못 박았다.
    글이 너무 반듯하면 AI가 쓴 티가 난다. 다만 **경고**라 원고를 버리지는 않는다.
    """
    hits = intentional_typo_hits(body)
    if len(hits) >= TYPO_MIN_COUNT:
        return []
    return [
        "의도적 오탈자·띄어쓰기 — 본문이 하나도 안 틀렸다"
        " (정리본 [AI 티 제거] 규칙: 맞춤법 1~2개, 띄어쓰기 2~3개를 일부러 틀린다)"
    ]


def reply2_logic_problems(text: str, rule: "BrandRule") -> list[str]:
    """브랜드 고유 논리(`reply2_logic`)를 담았는지 (사용자 지시 2026-09-21)."""
    logic = rule.reply2_logic
    if logic is None:
        return []
    squashed = _squash(text)
    hits = [w for w in logic.words if _squash(w) in squashed]
    problems: list[str] = []
    if len(hits) < logic.min_hits:
        problems.append(
            f"브랜드 논리 — {logic.note or '정해진 논리'}"
            f" ({' / '.join(logic.words)} 중 {logic.min_hits}개 이상,"
            f" 지금은 {len(hits)}개)"
        )
    if logic.also and not any(_squash(w) in squashed for w in logic.also):
        problems.append("브랜드 논리 — " + " / ".join(logic.also) + " 중 하나가 없다")
    return problems


def reply2_structure_problems(text: str, rule: "BrandRule") -> list[str]:
    """질문형 대대댓글2의 4요소 + 문장 검사 (기존 원고 구조 그대로).

    구조: [제품명(이)라고 있어요] → [근거를 **완전한 문장**으로] →
    [대안의 한계 한 문장] → [검색해보시면 후기 많아요]
    """
    product = rule.product_in_comment or rule.product
    problems: list[str] = []
    if product and _squash(product) not in _squash(text):
        problems.append(f"제품 표기 — `{product}` 가 들어가지 않았다")
    if not _has_any(text, REPLY2_EVIDENCE_WORDS):
        problems.append(
            "근거 문장 — 권위기관·검증·원리 중 하나를 **완전한 문장**으로 적어야 한다"
        )
    if not _has_any(text, REPLY2_LIMIT_WORDS):
        problems.append("대안의 한계 — 다른 방법으로는 왜 안 되는지 한 문장이 없다")
    if not _has_any(text, REPLY2_SEARCH_WORDS):
        problems.append("검색 유도 — 검색해보시면 후기 많아요 류의 마무리가 없다")
    problems += reply2_logic_problems(text, rule)
    problems += minus_copy_problems(text, rule)
    problems += reply2_sentence_problems(text, product)
    problems += evidence_laughter_problems(text)
    for owner, words in BRAND_ONLY_EVIDENCE.items():
        if rule.brand == owner:
            continue
        stolen = [w for w in words if _squash(w) in _squash(text)]
        if stolen:
            problems.append(
                f"근거 도용 — {owner} 근거({', '.join(stolen)})를 가져다 썼다"
            )
            break
    banned = [w for w in REPLY2_BANNED_PHRASES if w in _squash(text)]
    if banned:
        problems.append("금칙어 — " + ", ".join(banned))
    return problems


def review_reply2_problems(text: str, rule: "BrandRule") -> list[str]:
    """후기형 대대댓글2 — 역할이 다르다 (여분 계정이 "저도 이거 먹는 중"이라 거든다).

    제품명을 다시 꺼내지 않고 `이거` 로 받으며, 기간·감량 수치와 기관 검증을 말한다.
    """
    problems: list[str] = []
    if not _has_any(text, ("저도", "저두", "나도")):
        problems.append("맞장구 — 저도 이거 쓰는 중이라는 동조가 없다")
    if not _has_any(text, ("이거", "이걸", "이것")):
        problems.append("지칭 — 제품명 대신 `이거` 로 받아야 한다")
    if not _NUMBER_RE.search(text or ""):
        problems.append("수치 — 기간과 감량 수치를 숫자로 적어야 한다")
    kilos = [float(m) for m in _KG_RE.findall(text or "")]
    if not kilos:
        problems.append(f"감량 수치 — 몇 kg 빠졌는지 적어야 한다 ({REVIEW_MIN_KG}kg 이상)")
    elif max(kilos) < REVIEW_MIN_KG:
        problems.append(
            f"감량 수치 — {max(kilos):g}kg 는 너무 적다 ({REVIEW_MIN_KG}kg 이상으로 쓴다)"
        )
    if not _has_any(text, ("25%", "25퍼", "25프로")):
        problems.append("검증 수치 — 체지방 25% 감소 결과가 빠졌다")
    if not _has_any(text, ("정부기관", "국가기관", "농촌진흥청", "기관")):
        problems.append("권위 — 정부기관 실험이라는 근거가 빠졌다")
    # 후기형도 질문형과 같은 4요소다 (2026-09-22 GPT 교차 검증 반영):
    # 제품(이거) → 근거 → **대안의 한계** → **검색 유도**
    if not _has_any(text, REPLY2_LIMIT_WORDS):
        problems.append("대안의 한계 — 식단만으로는 왜 안 되는지 한 문장이 없다")
    if not _has_any(text, REPLY2_SEARCH_WORDS):
        problems.append("검색 유도 — 검색해보시면 후기 많아요 류의 마무리가 없다")
    product = rule.product_in_comment or rule.product
    if product and _squash(product) in _squash(text):
        problems.append(f"제품 표기 — 후기형 대대댓글2는 `{product}` 를 다시 쓰지 않는다")
    # 4요소(제품→근거→대안 한계→검색 유도)에 맞장구 한 마디가 더 붙어 최대 5문장이다
    problems += reply2_sentence_problems(
        text, product, min_sentences=2, max_sentences=5
    )
    problems += evidence_laughter_problems(text)
    return problems


def authority_words_in(text: str) -> set[str]:
    """이 글에 들어 있는 권위 근거 **명사**의 집합 (정규화 후).

    긴 낱말 안에 들어앉은 짧은 낱말은 뺀다 (`국가기관` 이 있으면 `기관` 은 세지 않는다).
    그래야 `정부기관` 과 `농촌진흥청` 처럼 **서로 다른 말**이 같은 낱말로 묶이지 않는다
    (사용자 결정 2026-09-22).
    """
    words = tuple(AUTHORITY_PLACE_WORDS) + tuple(AUTHORITY_OTHER_WORDS)
    squashed = _squash(text)
    found = {w for w in words if _squash(w) in squashed}
    return {w for w in found if not any(o != w and w in o for o in found)}


def shared_authority_words(*texts: str) -> list[str]:
    """여러 글에 **함께** 나오는 권위 근거 낱말 (정규화 후 **같은 낱말**일 때만).

    `정부기관` 과 `농촌진흥청` 은 서로 다른 말이라 중복이 아니다 (사용자 결정 2026-09-22).
    """
    if not texts:
        return []
    order = tuple(AUTHORITY_PLACE_WORDS) + tuple(AUTHORITY_OTHER_WORDS)
    sets = [authority_words_in(t) for t in texts]
    common = set.intersection(*sets) if sets else set()
    return [w for w in order if w in common]


def duplicate_authority_problems(comment2: str, reply2: str) -> list[str]:
    """권위 근거는 원고 전체에서 한 번만 (사용자 원고 피드백 2026-09-22).

    댓글2에서 `항문외과 의사` 를 썼으면 대대댓글2에서는 성분·원리·체감으로 간다.
    보는 자리는 **댓글2 ↔ 대대댓글2 둘뿐**이다. 팥순이 후기형 대대대댓글2의
    "국가기관 인증" 은 정리본이 못 박은 **고정 멘트**라 여기서 보지 않는다
    (사용자 결정 2026-09-22).
    """
    shared = shared_authority_words(comment2, reply2)
    if not shared:
        return []
    return [
        "권위 근거 중복 — " + ", ".join(shared) + " 를 댓글2와 대대댓글2에서 함께 썼다"
        " (대대댓글2는 성분·원리·직접 겪은 체감으로 바꿔 쓴다)"
    ]


def closing_sentence(text: str) -> str:
    """대대댓글2의 **마지막 문장** (같은 마무리가 되풀이되는지 보려고 쓴다)."""
    sentences = reply2_sentences(text)
    return _squash(sentences[-1]) if sentences else ""


def repeated_closings(texts: list[str]) -> list[str]:
    """한 묶음(같은 브랜드 하루) 안에서 **똑같은 마무리 문장**이 되풀이되는가."""
    seen: dict[str, int] = {}
    for text in texts:
        key = closing_sentence(text)
        if key:
            seen[key] = seen.get(key, 0) + 1
    return [f"같은 마무리 문장이 {n}번 되풀이됨: `{k}`" for k, n in seen.items() if n > 1]


def comment3_role_problems(text: str) -> list[str]:
    """댓글3이 **해결 경험 / 도움이 된 노력**을 담았는가 (정리본 댓글3 세트 역할).

    고충만 늘어놓고 끝나면 "이 사람도 나랑 같은 고민이었는데 해결됐구나" 라는
    신뢰가 생기지 않는다 (코숨핏 지적).
    """
    if not (text or "").strip():
        return ["댓글3 없음"]
    if _has_any(text, COMMENT3_SOLUTION_WORDS):
        return []
    return [
        "댓글3 역할 — 비슷한 고충만 있고 해결된 경험이나 도움이 된 노력이 없다"
        " (무엇을 해보니 어떻게 나아졌는지 한 마디를 넣는다)"
    ]


def stacked_authority_problems(text: str) -> list[str]:
    """근거 출처를 **겹쳐 붙였는가** (경고, 2026-09-22).

    `수면클리닉 이비인후과 의사 논문에` 처럼 기관·사람·문헌을 나란히 붙이면
    출처가 하나도 분명하지 않고 꾸며 낸 티가 난다. 출처는 하나만 쓴다.
    """
    words = tuple(AUTHORITY_PLACE_WORDS) + tuple(AUTHORITY_OTHER_WORDS)
    pattern = re.compile("|".join(re.escape(w) for w in words))
    out: list[str] = []
    for sentence in reply2_sentences(text) or [text or ""]:
        found = [(m.start(), m.end(), m.group()) for m in pattern.finditer(sentence)]
        run: list[str] = []
        prev_end = -99
        runs: list[list[str]] = []
        for start, end, word in found:
            if start - prev_end <= 2:
                run.append(word)
            else:
                run = [word]
                runs.append(run)
            prev_end = end
        places = [w for _, _, w in found if w in AUTHORITY_PLACE_WORDS]
        worst = max(runs, key=len) if runs else []
        if len(worst) >= 3 or len(places) >= 2:
            out.append(
                "근거 출처 겹침 — `"
                + " ".join(worst or places)
                + "` 처럼 출처를 겹쳐 붙이지 말고 **하나만** 쓴다"
            )
    return out


def worry_laughter_problems(text: str) -> list[str]:
    """고민을 말하는 문장에 붙은 ㅋㅋ ㅎㅎ (경고, 2026-09-22).

    `엽산이랑 철분은 챙겨 먹었는데도 그대로예요ㅋㅋ` 처럼 걱정을 말하면서
    웃어 버리면 정서가 어긋난다. 그 자리는 ㅠㅠ 다.
    """
    out: list[str] = []
    for sentence in reply2_sentences(text) or [text or ""]:
        if not _has_any(sentence, WORRY_WORDS):
            continue
        marks = WORRY_LAUGH_RE.findall(sentence)
        if marks:
            out.append(
                f"고민 문장 웃음 표기 — `{sentence.strip()}` 에 {''.join(marks)} 가 붙었다"
                " (고민·걱정 문장에는 ㅠㅠ 를 쓴다)"
            )
    return out


def yoyo_number_problems(text: str) -> list[str]:
    """요요(다시 쪘다)를 말했으면 **기간과 kg** 을 같이 적었는가.

    정리본 `팥순이 후기형.md` 11번: 몸무게 변화는 [기간 명시 + 명확한 kg] 으로 쓰고
    "요요가 와서 쪘다고 하는 부분에 모두 적용할 것".
    """
    if "요요" not in _squash(text) and not _has_any(text, ("다시쪄", "다시쪘", "또쪘")):
        return []
    out: list[str] = []
    if not _KG_RE.search(text or ""):
        out.append("요요 수치 — 요요를 말했으면 몇 kg 다시 쪘는지 적어야 한다")
    if not _PERIOD_RE.search(text or ""):
        out.append("요요 기간 — 요요를 말했으면 몇 주(달) 만인지 적어야 한다")
    return out


def review_comment5_problems(text: str) -> list[str]:
    """후기형 댓글5 — 요요로 여러 번 실패했다는 토로 + 댓글 수치 범위(3~10kg).

    정리본 `팥순이 후기형.md`: 댓글5(요요 고민 토로) "요요 때문에 진짜 몇 번을
    실패했는지 모르겠어요" / 절대 규칙 4번 "숫자는 매번 다르게 (3~10kg, 3~8주 범위)".
    """
    if not (text or "").strip():
        return ["댓글5 없음"]
    out: list[str] = []
    if not _has_any(text, REVIEW_C5_YOYO_WORDS) or not _has_any(
        text, REVIEW_C5_FAIL_WORDS
    ):
        out.append(
            "댓글5 역할 — 요요로 여러 번 실패했다는 토로가 있어야 한다"
            " (정체기 경험만으로는 안 된다)"
        )
    kilos = [float(m) for m in _KG_RE.findall(text or "")]
    bad = [
        k for k in kilos if not REVIEW_COMMENT_MIN_KG <= k <= REVIEW_COMMENT_MAX_KG
    ]
    if bad:
        out.append(
            f"댓글5 감량 수치 — {bad[0]:g}kg 는 댓글 수치 범위"
            f"({REVIEW_COMMENT_MIN_KG:g}~{REVIEW_COMMENT_MAX_KG:g}kg) 밖이다"
        )
    return out


def review_body_problems(body: str) -> list[str]:
    """후기형 본문 — 문단 끝 한글 이모티콘과 요요 수치 (정리본 반영, 2026-09-22)."""
    out: list[str] = []
    naked = [
        p.strip().splitlines()[-1].strip()
        for p in paragraphs(body)
        if p.strip()
        and not re.fullmatch(r"\{[^{}]*\}", p.strip())
        and not any(p.rstrip().endswith(mark) for mark in PARAGRAPH_TAIL_MARKS)
    ]
    if naked:
        out.append(
            f"문단 끝 이모티콘 — {len(naked)}개 문단이 ㅠㅠ ㅋㅋ ㅎㅎ !! 없이 끝난다"
            f" (보기: `{naked[0][-16:]}`)"
        )
    out += yoyo_number_problems(body)
    return out


def reply2_problems(text: str, rule: "BrandRule") -> list[str]:
    """원고유형에 맞는 대대댓글2 검사."""
    if rule.manuscript_type == "후기형":
        return review_reply2_problems(text, rule)
    return reply2_structure_problems(text, rule)


def golden_block(rule: "BrandRule", golden: list[str] | None = None) -> list[str]:
    """사용자가 고른 골든 대대댓글2를 **반드시 따를 흐름**으로 붙인다.

    시트에서 모은 예시보다 **먼저** 보여 준다 (사용자 지시 2026-09-21).
    """
    items = golden if golden is not None else load_golden_reply2(
        rule.brand, rule.manuscript_type
    )
    items = [t for t in (items or []) if (t or "").strip()]
    if not items:
        return []
    lines = [
        "",
        "<대대댓글2 골든 문장 — 사용자가 고른 본보기다. 이 구조와 논리를 반드시 따른다>",
        "문장을 그대로 베끼지 말고 **같은 차례·같은 논리·같은 길이감**으로 새로 쓴다",
    ]
    lines += [f"{i}. {t}" for i, t in enumerate(items, start=1)]
    if rule.reply2_logic and rule.reply2_logic.note:
        lines.append(f"- 이 브랜드의 논리: {rule.reply2_logic.note}")
    if rule.manuscript_type == "후기형":
        lines.append(
            f"- 감량 수치는 {REVIEW_MIN_KG}kg 이상으로 쓴다"
            " (기간은 매번 다르게 바꾼다)"
        )
    return lines


def reply2_prompt_block(rule: "BrandRule", golden: list[str] | None = None) -> list[str]:
    """질문형 대대댓글2 작성 지시 (기존 완성 원고 구조를 그대로 적은 것).

    예전에는 "60자 안쪽" 같은 길이부터 못 박아서 모델이 문장을 조각내
    `참기만 하면 제자리 농촌진흥청 25% 검증 검색해봐요` 같은 낱말 나열이 나왔다.
    이제는 **완전한 문장**과 논리 흐름을 먼저 요구하고 길이는 길이감만 말한다
    (사용자 지시 2026-09-21).
    """
    product = rule.product_in_comment or rule.product
    out = [
        "",
        "<대대댓글2 — 질문형에서 가장 중요한 자리. 아래 네 마디를 이 차례로 쓴다>",
        f"1) {product} (이)라고 있어요 — 제품을 먼저 꺼낸다",
        "2) 왜 믿을 만한지 **완전한 문장 하나**로 적는다"
        " (권위기관 / 검증받은 성분 / 어떤 원리로 작용하는지 중 하나를 문장으로 풀어 쓴다)",
        "3) 다른 방법으로는 왜 안 되는지 한 문장 (그냥 참는 걸로는 / 잠깐뿐이라 / 소용없더라구요)",
        "4) 검색해보시면 후기 많아요 류로 마무리한다",
        "- 완전한 문장 3~4개로 쓴다. 문장은 반드시 서술 어미(~요 ~에요 ~더라구요)로 끝낸다",
        "- 근거를 말하는 문장(의사·기관·검증·성분·원리)에는 ㅋㅋ ㅎㅎ 를 붙이지 않는다 (ㅠㅠ 는 괜찮다)"
        " (웃으면서 말하면 그 근거의 신뢰가 깎인다)."
        " 웃음 표기는 다른 방법의 한계를 말하는 문장이나 마지막 검색 유도 문장에서만 쓴다",
        "- 근거를 낱말로 나열하지 않는다"
        " (나쁜 보기: `참기만 하면 제자리 농촌진흥청 25% 검증 검색해봐요`"
        " — 조각난 구와 명사 나열이라 사람이 쓴 말이 아니다)",
        f"- 길이는 기존 원고와 비슷한 느낌으로 쓴다 (대략 {REPLY2_MIN_LEN}~{REPLY2_MAX_LEN}자)."
        " 길이를 맞추려고 조사나 서술어를 빼지 마라 — 문장이 온전한 것이 먼저다",
        f"- 근거는 이 브랜드가 실제로 쓸 수 있는 것({rule.authority})만 쓴다."
        " 다른 브랜드의 기관명·검증 수치를 가져다 쓰면 안 된다",
        "- 다음 말은 쓰는 순간 버린다: " + " / ".join(REPLY2_BANNED_PHRASES),
    ]
    if rule.reply2_logic and rule.reply2_logic.note:
        out.append(f"- 이 브랜드의 논리: {rule.reply2_logic.note}")
    if rule.brand in MINUS_COPY_BRANDS:
        out.append(f"- {MINUS_COPY_RULE}")
    out += golden_block(rule, golden)
    if not any("골든 문장" in line for line in out):
        out.append(
            f"- 좋은 보기) {product}라고 있어요 국가기관인 농촌진흥청에서 체지방 25% 감소"
            " 검증받은 성분이에요 그냥 참는거로는 한계있어서 검색해보시면 후기 많아요"
        )
    return out


def body_lines(body: str) -> list[str]:
    """본문의 빈 줄이 아닌 줄 목록 (자리표시자 줄은 뺀다)."""
    out = []
    for line in (body or "").splitlines():
        text = line.strip()
        if not text or re.fullmatch(r"\{[^{}]*\}", text):
            continue
        out.append(text)
    return out


def long_lines(body: str, line_max: int = LINE_MAX) -> list[str]:
    """상한을 넘긴 줄 목록 (공백 포함으로 센다)."""
    return [line for line in body_lines(body) if len(line) > line_max]


def placeholder_paragraph_index(body: str) -> int:
    """`{키워드}` 앞에 놓인 문단이 몇 개인지. 없으면 -1."""
    blocks = [p.strip() for p in re.split(r"\n\s*\n", body or "")]
    count = 0
    for block in blocks:
        if not block:
            continue
        if re.fullmatch(r"\{키워드\}", block):
            return count
        count += 1
    return -1


def leaked_internal_terms(text: str) -> list[str]:
    """글에 **낱말로** 새어 나온 내부 용어 목록 (사용자 지시 2026-09-21).

    앞에 다른 한글 글자가 붙어 있으면 그 용어가 아니라 **다른 낱말의 일부**로 본다.
    (`불규칙` 안의 `규칙`, `사본문서` 안의 `본문` 같은 오탐을 막는다.)
    빈칸은 하나로 줄여서 보되 없애지는 않는다 — 없애면 낱말 경계가 사라진다.
    """
    spaced = re.sub(r"\s+", " ", strip_placeholders(text or ""))
    return [
        term
        for term in INTERNAL_TERMS
        if re.search(r"(?<![가-힣])" + re.escape(term), spaced)
    ]


def _drop_token(body: str, token: str) -> str:
    """자리표시자 토큰을 본문에서 전부 지우고 빈 줄을 정리한다."""
    out = (body or "").replace(token, "")
    out = re.sub(r"(?m)^[ \t]+$", "", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip("\n")


def insert_placeholder(body: str, token: str, after_paragraph: int) -> str:
    """`token`을 **정해진 문단 뒤**에 단독 줄로 넣어 준다 (결정적 보정).

    자리표시자 위치는 브랜드 규칙이 이미 정해 둔 값이라 모델에게 맡길 일이
    아니다 (사용자 지시 2026-09-21). 이미 들어 있던 같은 토큰은 지우고
    다시 제자리에 넣는다. 문단 수가 모자라면 맨 뒤에 붙인다.
    """
    return _place_tokens(body, [(token, after_paragraph)])


def _place_tokens(body: str, wanted: list[tuple[str, int]]) -> str:
    """여러 자리표시자를 **글 문단 기준** 위치에 한 번에 넣는다.

    위치를 셀 때 자리표시자 줄은 문단으로 세지 않는다 (서로 밀지 않게).
    """
    clean = body or ""
    for token, _ in wanted:
        clean = _drop_token(clean, token)
    blocks = paragraphs(clean)
    if not blocks:
        return body or ""
    plan = [
        (min(max(int(after), 1), len(blocks)), token)
        for token, after in wanted
        if token
    ]
    for idx, token in sorted(plan, key=lambda item: -item[0]):
        blocks.insert(idx, token)
    return "\n\n".join(blocks)


def drop_bare_keyword_paragraphs(body: str, keyword: str) -> str:
    """키워드만 덩그러니 적힌 문단을 지운다.

    모델이 `{키워드}` 대신 키워드 값을 한 줄 짜리 문단으로 써 버리는 일이 잦다.
    그 줄은 자리표시자 노릇을 하려던 것이라 글에 남으면 뜬금없이 읽힌다.
    """
    needle = _squash(keyword)
    if not needle:
        return body or ""
    keep = [p for p in paragraphs(body) if _squash(p) != needle]
    return "\n\n".join(keep)


def apply_placeholders(body: str, rule: "BrandRule", keyword: str = "") -> str:
    """브랜드 규칙이 요구하는 자리표시자를 본문에 결정적으로 넣는다."""
    out = drop_bare_keyword_paragraphs(body, keyword) if keyword else (body or "")
    wanted = [("{키워드}", rule.placeholder_after_paragraph)]
    if rule.extra_placeholder:
        wanted.append((rule.extra_placeholder, rule.extra_placeholder_after_paragraph))
    return _place_tokens(out, wanted)


def opening_word(body: str) -> str:
    """본문 첫 줄의 첫 어절 (오프닝 반복 검사에 쓴다)."""
    for line in (body or "").splitlines():
        text = line.strip()
        if text and not re.fullmatch(r"\{[^{}]*\}", text):
            return text.split()[0] if text.split() else ""
    return ""


def load_recent_openings(generated_dir: str | Path, limit: int = 20) -> list[str]:
    """최근 생성 원고(`warehouse/manuscripts/generated/**/*.json`)의 오프닝 첫 어절.

    파일이 없으면 빈 목록이라 검사가 통째로 건너뛰어진다 (설계서 F-c).
    """
    root = Path(generated_dir)
    if not root.exists():
        return []
    files = sorted(root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[str] = []
    for path in files[: max(0, int(limit))]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            # 원고 1건이 아닌 다른 모양의 JSON(예: 시험용 요약 목록)이 같은
            # generated/ 트리에 섞여 있어도 조용히 건너뛴다 (2026-09-23 실측에서
            # `_compare5_summary.json`처럼 list 꼴이 섞여 있어 재현된 문제).
            continue
        word = opening_word(str(data.get("body") or ""))
        if word:
            out.append(word)
    return out


def methods_already_tried(body: str) -> list[str]:
    """본문에서 "작성자가 이미 해 본 방법" 줄을 뽑는 간단한 휴리스틱 (설계서 G).

    댓글4가 본문과 어긋난 지적을 하지 않도록 프롬프트에 그대로 넘긴다.
    """
    marks = ("해봤", "해 봤", "써봤", "써 봤", "발라봤", "먹어봤", "먹어 봤", "다녀봤", "끊어봤")
    out: list[str] = []
    for line in body_lines(strip_placeholders(body)):
        if any(m in line for m in marks):
            out.append(line)
    return out[:3]


# ------------------------------------------------------------------ 예시(few-shot)
#: 브랜드 시트에서 완성 원고를 읽어 오는 탭 이름
EXAMPLE_SHEET = "게시글 쓰기 원본"
#: few-shot으로 붙일 최대 예시 수 (많을수록 캐시 블록이 커진다)
EXAMPLE_COUNT = 2
#: 완성 행으로 인정하는 본문 최소 글자 수
EXAMPLE_MIN_BODY = 150


def _example_row_text(row: dict) -> tuple[str, str]:
    """행 하나에서 `(원고유형, 본문 셀)`을 꺼낸다 (열 이름 또는 A~E 자리)."""
    from v2r.sources.sheets import _by_letter

    return _by_letter(row, "E", ("원고유형",)), _by_letter(row, "B", ("본문",))


def _is_complete_example(text: str) -> bool:
    """`sheet_text()` 형식의 완성 원고인가 (본문 150자 이상 + 댓글 12개)."""
    from v2r.content.manuscript import parse_article

    if not text or "제목" not in text:
        return False
    try:
        parsed = parse_article(text)
    except Exception:  # pragma: no cover - 파서가 못 읽는 셀은 그냥 버린다
        return False
    if body_length(parsed.body) < EXAMPLE_MIN_BODY:
        return False
    table = {re.sub(r"\s+", "", c.label): (c.text or "").strip() for c in parsed.comments}
    return all(table.get(label) for label in COMMENT_LABELS)


def load_examples(
    brand: str,
    manuscript_type: str = "",
    rows_or_path: Any = None,
) -> list[str]:
    """기존 완성 원고 예시 `EXAMPLE_COUNT`개 (설계서 B).

    `rows_or_path`는 행 목록(dict 리스트) 또는 `data/brand_sheet_<브랜드>.xlsx` 경로다.
    같은 원고유형의 **완성 행**(본문 150자 이상 + 댓글 12개)을 시트 **앞에서부터**
    고르므로 같은 시트면 늘 같은 예시가 나온다 (캐시가 깨지지 않는다).
    시트가 없으면 예시 없이 진행한다 (경고 로그만 남긴다).
    """
    want = (manuscript_type or "").strip() or "질문형"
    rows: list[dict]
    if rows_or_path is None:
        return []
    if isinstance(rows_or_path, (str, Path)):
        path = Path(rows_or_path)
        if not path.exists():
            log.warning("브랜드 시트가 없어 예시 없이 씁니다: %s (%s)", path, brand)
            return []
        try:
            from v2r.sources.keyword_list import rows_from_xlsx

            from v2r.sources.sheets import _restore_header_row

            # 머리글 없이 데이터부터 시작하는 탭이면 첫 줄을 되살린다
            rows = _restore_header_row(rows_from_xlsx(path, EXAMPLE_SHEET))[0]
        except Exception as exc:
            log.warning("브랜드 시트를 읽지 못해 예시 없이 씁니다: %s (%s)", exc, brand)
            return []
    else:
        rows = list(rows_or_path or [])

    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mtype, text = _example_row_text(row)
        if ((mtype or "").strip() or "질문형") != want:
            continue
        text = (text or "").strip()
        if not _is_complete_example(text):
            continue
        out.append(text)
        if len(out) >= EXAMPLE_COUNT:
            break
    if not out:
        log.warning("%s/%s: 쓸 만한 완성 원고 예시를 찾지 못했습니다", brand, want)
    return out


#: system에 넣는 시트 예시 개수 상한 (2026-09-22 토큰 절약 — 전부 넣지 않는다)
MAX_PROMPT_EXAMPLES = 3


def examples_block(examples: list[str] | None) -> list[str]:
    """few-shot 예시를 system 캐시 블록에 넣을 줄 목록으로 만든다 (최대 3개)."""
    items = [e for e in (examples or []) if (e or "").strip()][:MAX_PROMPT_EXAMPLES]
    if not items:
        return []
    lines = [
        "",
        "<기존 완성 원고 예시 — 형태와 흐름을 그대로 따를 것>",
        "예시의 문장과 소재는 절대 베끼지 않는다."
        " 문단 수 / 줄 길이 / 댓글 역할 / 브랜드가 등장하는 시점 / 말투만 따라 한다",
    ]
    for i, text in enumerate(items, start=1):
        lines += [f"[예시 {i}]", "```text", text.strip(), "```"]
    return lines


# ------------------------------------------------------------------ 프롬프트
def _guide_block(guide_text: str) -> str:
    """지침을 참고 자료로 붙인다.

    **프롬프트에 실을 때만** `compress_guide_text`로 줄인다 (2026-09-22). 코드
    검증기가 이미 검사하는 규칙(줄 나눔·글자 수·`{키워드}` 자리·웃음 표기·출력
    형식)과 Make 운영 메모·중복 문장은 빼고, 모델만 할 수 있는 판단(브랜드 정보·
    설득 논리·구조)만 남긴다. 원본 md 파일은 건드리지 않는다.
    """
    text = compress_guide_text(guide_text)
    if not text:
        return ""
    return "\n<지침 (이 글의 최종 기준이다)>\n" + text + "\n"


def _body_rules_block(
    rule: BrandRule, guide_text: str = "", examples: list[str] | None = None
) -> list[str]:
    """본문 규칙 중 **키워드와 상관없이 늘 같은** 부분 (프롬프트 캐시 대상)."""
    lines: list[str] = [
        f"브랜드: {rule.brand} (원고유형 {rule.manuscript_type})",
        f"타겟: {rule.target}",
        "",
        BRAND_LINE_BREAK_RULE,
        "",
        NO_SCENE_RULE,
        "",
        "<반드시 지켜야 할 규칙>",
        f"- 본문 글자 수는 공백 제외 {rule.body_max}자를 넘지 않는다",
        "- 분량을 맞추려고 중간에 끊지 말고 반드시 끝을 맺는다",
        f"- 작성 키워드를 한 글자도 빼먹지 말고 본문에 정확히 {rule.keyword_count}번 넣는다",
    ]
    if rule.banned_in_body:
        lines.append(
            "- 본문에 다음 낱말을 절대 쓰지 않는다: " + ", ".join(rule.banned_in_body)
        )
    lines.append(
        f"- 본문 {rule.placeholder_after_paragraph}번째 문단이 끝난 뒤 줄 하나에 "
        "`{키워드}` 만 단독으로 적는다 (앞뒤로 빈 줄을 둔다). "
        "이때 {키워드}는 키워드 값이 아니라 글자 그대로 `키워드` 다"
    )
    if rule.extra_placeholder:
        lines.append(
            f"- 병행 효과를 말하는 문단 뒤에 줄 하나에 `{rule.extra_placeholder}` 만 단독으로 적는다"
        )
    lines.append("")
    lines.append("<자리표시자 보기 — body 값 안에서 이렇게 생겨야 한다>")
    lines.append("```")
    example = ["...첫 번째 문단 마지막 줄", ""]
    if rule.placeholder_after_paragraph >= 2:
        example += ["두 번째 문단 첫 줄", "두 번째 문단 마지막 줄", ""]
    example += ["{키워드}", "", "다음 문단 첫 줄"]
    lines.extend(example)
    lines.append("```")
    lines.append(
        "즉 `{키워드}` 는 앞뒤가 빈 줄인 **자기 혼자만 있는 줄**이다. "
        "문장 안에 섞어 넣으면 안 되고 다른 글자를 같은 줄에 붙이면 안 된다"
    )
    lines.append("")
    lines.append("<본문 구조>")
    lines.extend(f"- {s}" for s in rule.body_structure)
    lines.append("")
    lines.append("<작성 지침>")
    lines.extend(f"- {s}" for s in rule.body_notes)
    lines.append(f"- 본문 한 줄은 {rule.line_max}자를 넘지 않는다 (모바일 한 줄 20자 안팎)")
    lines.append("")
    lines.append(BRAND_CLICHE_RULE)
    lines.extend(examples_block(examples))
    lines.append(_guide_block(guide_text))
    return lines


def _body_dynamic_block(rule: BrandRule, keyword: str, cafe: str = "") -> list[str]:
    """본문 프롬프트 중 **키워드마다 달라지는** 부분 (캐시하지 않는다)."""
    lines = [
        f"작성 키워드: {keyword}",
        f"- 작성 키워드 `{keyword}` 를 한 글자도 빼먹지 말고 본문에 정확히 "
        f"{rule.keyword_count}번 넣는다",
        "",
        "<첫 줄 — 문제부터 바로>",
        f"- `{keyword}` 로 고민하는 사람이 겪는 **증상이나 고민**을 첫 줄에 바로 적는다",
        "- 글을 쓰는 장소·시간·상태를 적지 않는다 (한산해서 / 잠깐 앉아 / 글 남겨요 금지)",
    ]
    if cafe:
        lines.append(f"올릴 카페: {cafe}")
    return lines


def build_shared_system(
    brand: str,
    manuscript_type: str = "",
    guide_text: str = "",
    examples: list[str] | None = None,
) -> str:
    """브랜드(+원고유형)당 **하나뿐인** system 프롬프트 (2026-09-22 토큰 절약).

    본문·댓글·부분 재시도가 **글자 하나까지 같은** 문자열을 쓴다. 그래야 원고 한
    건 안에서도 2·3번째 호출이 프롬프트 캐시(`cache_read_input_tokens`)를 타고,
    같은 브랜드를 잇달아 만들면 다음 원고까지 캐시가 이어진다.

    "이번에 무엇을 쓸 차례인가"(할 일)와 출력 형식은 여기에 넣지 않는다 — 그걸
    넣는 순간 호출마다 system이 달라져 캐시가 깨지기 때문이다. 그쪽은 user로 간다.
    지침·시트 예시도 **딱 한 번만** 넣는다 (예전에는 본문용·댓글용에 두 번 실렸다).
    """
    rule = rule_for(brand, manuscript_type)
    return "\n".join(
        [
            BRAND_SHARED_SYSTEM.format(
                tone=HUMAN_TONE_RULES,
                internal=NO_INTERNAL_TERMS_RULE,
                first_mention=BRAND_FIRST_MENTION_RULE,
            ),
            "",
            "## 본문 규칙",
            *_body_rules_block(rule),
            "",
            "## 댓글 규칙",
            *_comments_rules_block(rule),
            "",
            *examples_block(examples),
            _guide_block(guide_text),
        ]
    )


def build_body_prompt(
    brand: str,
    keyword: str,
    cafe: str = "",
    guide_text: str = "",
    manuscript_type: str = "",
    examples: list[str] | None = None,
    reference_brief: str = "",
    relevance: int | None = None,
    bridge_rationale: str = "",
) -> tuple[str, str]:
    """본문 생성용 `(공용 system, 이번 할 일 user)` 프롬프트.

    system은 `build_shared_system`이 만든 **브랜드 공용** 문자열 그대로다.
    "본문을 써라"와 출력 형식, 키워드·카페는 user로 간다. `reference_brief`(통검
    1등 글 형식 요약, 2026-09-23 지시)도 키워드마다 달라지므로 user에만 넣는다
    — system(브랜드 공용, 캐시 대상)은 건드리지 않는다.

    `relevance`가 3(당위성, `keyword_relevance.RELEVANCE_BRIDGE`)이면
    `bridge_rationale`(연관도 재산정 때 모델이 낸 연결 논리 한 줄)을 user에
    별도 블록으로 넣는다(사용자 지시 2026-09-24). system은 건드리지 않는다.
    """
    rule = rule_for(brand, manuscript_type)
    system = build_shared_system(brand, manuscript_type, guide_text, examples)
    lines = [BRAND_BODY_TASK, "", *_body_dynamic_block(rule, keyword, cafe)]
    if reference_brief:
        lines += ["", "【참고 형식(통검 1등 글)】", reference_brief]
    if relevance == 3 and bridge_rationale:
        lines += [
            "",
            "【당위성 논리】 " + bridge_rationale
            + " — 이 논리로 자연스럽게 브랜드로 이어라(억지 연결 금지)",
        ]
    user = "\n".join(lines)
    return system, user


def _comments_rules_block(
    rule: BrandRule, guide_text: str = "", examples: list[str] | None = None
) -> list[str]:
    """댓글 규칙 중 **키워드·본문과 상관없이 늘 같은** 부분 (프롬프트 캐시 대상)."""
    product = rule.product_in_comment or rule.product

    lines: list[str] = [
        f"브랜드: {rule.brand} (원고유형 {rule.manuscript_type})",
        f"댓글에서 쓸 제품 표기: {product}",
        f"원씽 이동(설득 논리): {rule.one_thing}",
        f"쓸 수 있는 권위재: {rule.authority}",
        "",
        "<댓글 글자 수>",
        f"- 댓글2와 대대댓글2를 뺀 모든 댓글은 {rule.comment_max}자를 넘지 않는다",
        f"- 댓글2도 {rule.comment2_max}자를 넘지 않는다",
        f"- 대대댓글2는 {rule.reply2_max}자 안팎으로 쓴다 (길이보다 문장이 온전한 것이 먼저다)",
        "- 줄 나눔 없이 한 줄로 쓴다",
        "",
        "<12개 구조와 역할>",
        "댓글1 / 대댓글1 / 댓글2 / 대댓글2 / 대대댓글2 / 대대대댓글2 /"
        " 댓글3 / 대댓글3 / 댓글4 / 대댓글4 / 댓글5 / 대댓글5 — 정확히 이 12개만",
        f"- 제품명({product})은 {rule.first_mention_label} 에서 처음 나온다."
        f" 그 앞 댓글에는 절대 제품명을 쓰지 않는다",
    ]
    lines.append(
        f"- {rule.first_mention_label} 은 {product} 를 꺼낸 뒤 물러서지 않는다."
        " 쓰게 된 계기 한 가지와 쓰고 나서 느낀 변화 한 가지를 꼭 적고"
        " 제품보다 방법이 중요 / 결국은 습관 / 개인차 / 참고만 같은 말은 쓰지 않는다"
    )
    lines.append(
        "- 프레임 페르소나 키워드 본문 원고 댓글1 대댓글 규칙 같은 낱말은"
        " 댓글 어디에도 쓰지 않는다 (작업용 용어라 실제 회원 말투가 아니다)"
    )
    if rule.manuscript_type == "후기형":
        writer = ", ".join(author_labels(rule.manuscript_type))
        lines.append("")
        lines.append(BRAND_REVIEW_THREAD_FLOW)
        lines.append("")
        lines.append("<후기형 — 노드별 작성 계정 역할 (질문형과 다르다)>")
        lines.append(f"- 본문 작성자가 쓰는 자리: {writer}")
        lines.append(
            f"- 대댓글2 = 본문 작성자다. 작성자는 이미 써 본 사람이라 여기서 직접"
            f" {product} 를 꺼내 알려 준다 (되묻지 않는다)"
        )
        lines.append(
            "- 대대댓글2 = 여분 댓글풀 계정이다. 저도 이거 먹는 중이라고 거든다"
            " (제품명을 다시 쓰지 말고 이거 로 받는다)"
        )
        lines.append("- 대대대댓글2 = 본문 작성자다. 맞장구치며 마무리한다")
        lines.append(
            "- 작성자 자리에서는 자기가 이미 쓴 제품을 두고"
            " 그거 어디서 사요 / 어떤 성분이에요 / 효과 있나요 처럼 절대 되묻지 않는다"
        )
        lines.extend(golden_block(rule))
    else:
        # 질문형 대대댓글2 구조 (기존 원고 구조 그대로, 2026-09-21 개편)
        lines.extend(reply2_prompt_block(rule))
    if rule.required_phrases:
        lines.append("")
        lines.append("<반드시 들어가야 하는 멘트 (자리까지 지침이 정해 둔 것)>")
        for label, options in rule.required_phrases:
            lines.append(f"- {label}: " + " 또는 ".join(options) + " 를 반드시 넣는다")
    lines.append("")
    lines.append("<근거 문장에는 웃지 않는다 (사용자 지시 2026-09-22)>")
    lines.append(
        "- 댓글2와 대대댓글2에서 **근거를 말하는 문장**(의사 기관 논문 검증 성분 원리)에는"
        " ㅋㅋ ㅎㅎ 를 붙이지 않는다 — 웃으면서 말하면 신뢰가 떨어진다"
    )
    lines.append(
        "- 나쁜 보기) 의사분들이 추천하는 보호막 강화 성분이 들어있어요 ㅋㅋㅋㅋ"
    )
    lines.append(
        "- ㅋㅋ ㅎㅎ 는 다른 방법의 한계를 말하는 문장이나 마지막 검색 유도 문장에서만 쓴다"
    )
    lines.append("- ㅠㅠ 는 웃는 게 아니라 안타까움이라 근거 문장에 붙어도 괜찮다")
    lines.append("")
    lines.append("<댓글4·댓글5 추가 규칙>")
    lines.append("- 댓글4는 본문에서 작성자가 이미 해 본 방법을 짚어 준다 (본문에 없는 방법을 지어내지 않는다)")
    lines.append("- 댓글5는 기간과 감량·변화 수치를 반드시 숫자로 적는다 (3주 -5kg 처럼)")
    lines.append("")
    lines.append(BRAND_CLICHE_RULE)
    lines.extend(f"- {s}" for s in rule.comment_notes)
    lines.extend(examples_block(examples))
    lines.append(_guide_block(guide_text))
    return lines


def build_comments_prompt(
    brand: str,
    keyword: str,
    title: str,
    body: str,
    manuscript_type: str = "",
    guide_text: str = "",
    examples: list[str] | None = None,
) -> tuple[str, str]:
    """댓글 12개 생성용 `(공용 system, 이번 할 일 user)` 프롬프트.

    system은 본문 생성 때와 **똑같은** 브랜드 공용 문자열이라 이 호출은 캐시를
    읽고 지나간다. "댓글을 써라"와 출력 형식, 이번 본문은 user로 간다.
    """
    rule = rule_for(brand, manuscript_type)
    system = build_shared_system(brand, manuscript_type, guide_text, examples)
    tried = methods_already_tried(body)
    user = "\n".join(
        [
            BRAND_COMMENTS_TASK,
            "",
            f"작성 키워드: {keyword}",
            "",
            "<본문>",
            f"제목: {title}",
            strip_placeholders(body).strip(),
        ]
        + (
            ["", "<작성자가 본문에서 이미 해 봤다고 한 방법 — 댓글4는 이 중에서 짚는다>"]
            + [f"- {t}" for t in tried]
            if tried
            else []
        )
    )
    return system, user


def build_combined_prompt(
    brand: str,
    keyword: str,
    cafe: str = "",
    guide_text: str = "",
    manuscript_type: str = "",
    examples: list[str] | None = None,
    reference_brief: str = "",
    relevance: int | None = None,
    bridge_rationale: str = "",
) -> tuple[str, str]:
    """본문 + 댓글 12개를 **한 번에** 받는 `(공용 system, 이번 할 일 user)` 프롬프트.

    `relevance`/`bridge_rationale`은 `build_body_prompt`와 같은 규칙(연관도 3=당위성일
    때만 【당위성 논리】 블록 추가, 사용자 지시 2026-09-24)을 따른다.
    """
    rule = rule_for(brand, manuscript_type)
    system = build_shared_system(brand, manuscript_type, guide_text, examples)
    combined_lines = [BRAND_COMBINED_TASK, "", *_body_dynamic_block(rule, keyword, cafe)]
    if reference_brief:
        combined_lines += ["", "【참고 형식(통검 1등 글)】", reference_brief]
    if relevance == 3 and bridge_rationale:
        combined_lines += [
            "",
            "【당위성 논리】 " + bridge_rationale
            + " — 이 논리로 자연스럽게 브랜드로 이어라(억지 연결 금지)",
        ]
    user = "\n".join(
        combined_lines
    )
    return system, user


def build_partial_retry_prompt(
    brand: str,
    keyword: str,
    labels: list[str],
    problems: list[str],
    manuscript_type: str = "",
    guide_text: str = "",
    examples: list[str] | None = None,
) -> tuple[str, str]:
    """검증에 걸린 **그 자리만** 다시 받는 `(공용 system, 이번 할 일 user)` 프롬프트.

    직전 출력 12개를 통째로 되보내지 않는다. 고친 자리만 JSON으로 받아
    `merge_comments`로 갈아 끼운다. system은 본문·댓글 때와 **똑같은** 공용
    문자열이라 재시도 호출은 거의 전부 캐시 읽기로 지나간다.
    """
    system = build_shared_system(brand, manuscript_type, guide_text, examples)
    want = [label for label in COMMENT_LABELS if label in set(labels)] or list(COMMENT_LABELS)
    user = "\n".join(
        [
            BRAND_PARTIAL_RETRY_TASK,
            "",
            f"작성 키워드: {keyword}",
            "",
            "<다시 쓸 자리>",
            " ".join(want),
            "",
            "<어긴 규칙 — 이번에는 반드시 지킬 것>",
            *(f"- {p}" for p in problems),
            "",
            f"이 자리만 다시 써서 같은 JSON 형식으로 돌려라 (키는 {' '.join(want)} 뿐이다)",
        ]
    )
    return system, user


def build_body_fix_user(keyword: str, body: str, problems: list[str]) -> str:
    """본문을 **처음부터 다시 쓰지 않고 고쳐 쓰게** 하는 user 프롬프트.

    통째로 다시 쓰게 하면 모델이 매번 새 글을 뽑아 같은 항목에서 또 걸린다
    (2026-09-21 재시도 실패 원인). 직전 본문을 그대로 돌려주고 **걸린 곳만**
    손보게 하면 같은 실패를 되풀이하지 않는다. `system` 프롬프트는
    `build_body_prompt` 가 만든 것을 그대로 다시 쓴다 (캐시도 그대로 탄다).
    """
    return "\n".join(
        [
            f"작성 키워드: {keyword}",
            "",
            "<직전에 쓴 본문 — 이 글을 **살려서 고쳐 쓴다**>",
            "```",
            strip_placeholders(body or "").strip(),
            "```",
            "",
            "<이 본문이 어긴 규칙 — 이것만 고쳐라>",
            *(f"- {p}" for p in problems),
            "",
            "고칠 곳만 손보고 나머지 문장·말투·흐름은 그대로 둔다.",
            "자리표시자(`{키워드}` `{B/A}`)는 넣지 마라 — 그건 프로그램이 알아서 넣는다.",
            '고친 본문 전체를 `{"body": "..."}` 형식 JSON 하나로 돌려라',
        ]
    )


def failing_comment_labels(checks: list[dict]) -> list[str]:
    """검증 결과에서 **문제가 있는 댓글 라벨**만 추려 낸다."""
    found: set[str] = set()
    for c in checks:
        if c["통과"] or c.get("구간") != "댓글":
            continue
        text = f"{c.get('항목', '')} {c.get('실제', '')}"
        for label in COMMENT_LABELS:
            # `댓글2` 가 `대댓글2` 안에서 잡히지 않도록 앞 글자 `대` 를 막는다
            if re.search(r"(?<!대)" + label, text):
                found.add(label)
    return [label for label in COMMENT_LABELS if label in found]


def merge_comments(current: list[CommentNode], payload: Any) -> list[CommentNode]:
    """부분 재시도 응답(`{"댓글2": "...", ...}`)을 기존 12개에 라벨로 갈아 끼운다."""
    patch = {
        node.label: node.text
        for node in _comment_nodes(payload)
        if (node.text or "").strip()
    }
    merged: list[CommentNode] = []
    for node in current:
        text = patch.get(node.label, "")
        merged.append(
            CommentNode(
                label=node.label,
                text=text or node.text,
                depth=node.depth,
                role=node.role,
            )
        )
    return merged


# ------------------------------------------------------------------ 검증
def _check(
    item: str,
    expected: str,
    actual: str,
    ok: bool,
    hard: bool = True,
    scope: str = "본문",
) -> dict:
    return {
        "항목": item,
        "기준": expected,
        "실제": actual,
        "통과": ok,
        "필수": hard,
        "구간": scope,
    }


def first_product_mention_label(manuscript: Manuscript, rule: BrandRule) -> str:
    """제품명이 **실제로** 처음 나온 댓글 라벨. 아무 데도 없으면 빈 문자열.

    (예전에는 라벨 문자열만 보고 판단해 오판이 있었다 — 설계서 D)
    """
    names = [
        _squash(p) for p in (rule.product_in_comment, rule.product) if (p or "").strip()
    ]
    order = {label: i for i, label in enumerate(COMMENT_LABELS)}
    found = [
        c.label
        for c in sorted(
            manuscript.comments, key=lambda c: order.get(re.sub(r"\s+", "", c.label), 99)
        )
        if any(n in _squash(c.text) for n in names)
    ]
    return re.sub(r"\s+", "", found[0]) if found else ""


def validate(
    manuscript: Manuscript,
    rule: BrandRule | None = None,
    soft_limits: bool | None = None,
    recent_openings: list[str] | None = None,
) -> list[dict]:
    """원고를 검증해 항목별 결과를 돌려준다 (예외를 던지지 않는다).

    `soft_limits`가 켜져 있으면 댓글 길이는 상한의 1.5배까지 **경고**이고
    그 위부터 실패다 (평가 2026-09-19 제안 3). 지정하지 않으면 브랜드 규칙값을 쓴다.
    """
    rule = rule or rule_for(brand_of(manuscript), manuscript.manuscript_type)
    # `soft_limits`는 이제 뜻이 없다 (길이는 늘 상한의 200%까지 통과).
    # 부르는 쪽을 깨지 않으려고 자리만 남겨 둔다.
    _ = soft_limits if soft_limits is not None else rule.soft_limits
    body = manuscript.body or ""
    keyword = manuscript.keyword or ""
    checks: list[dict] = []

    length = body_length(body)
    limit = int(rule.body_max * LENGTH_TOLERANCE)
    checks.append(
        _check("본문 글자 수(공백 제외)", f"{limit}자 이하 (상한 {rule.body_max}자의 200%)", f"{length}자", length <= limit)
    )

    hits = keyword_hits(body, keyword)
    short = max(rule.keyword_count - hits, 0)
    checks.append(
        _check(
            "키워드 포함 횟수",
            f"`{keyword}` 를 글자 그대로 {rule.keyword_count}회 이상",
            f"{hits}회" + (f" — `{keyword}` 가 들어간 문장 {short}개를 더 넣어야 한다" if short else ""),
            hits >= rule.keyword_count,
        )
    )

    squashed = _squash(body)
    found = [w for w in rule.banned_in_body if _squash(w) and _squash(w) in squashed]
    checks.append(
        _check(
            "본문 브랜드·제품명 미언급",
            "없음" if not rule.banned_in_body else ", ".join(rule.banned_in_body) + " 금지",
            ", ".join(found) if found else "없음",
            not found,
        )
    )

    # --- 상황 연출 금지 (사용자 지시 2026-09-21): 글 쓰는 장소·시간·상태 묘사
    scene = [
        w
        for w in SCENE_BANNED_PHRASES
        if w in _squash(body) or w in _squash(manuscript.title)
    ]
    checks.append(
        _check(
            "상황 연출 금지",
            "글 남겨요 / 한산해서 / 잠깐 앉아 / 쓰는 중 / 끄적 / 적어봐요 류 금지"
            " (문제 상황부터 바로 시작)",
            ", ".join(scene) if scene else "없음",
            not scene,
        )
    )

    leaked = leaked_internal_terms(body)
    checks.append(
        _check(
            "내부 용어 미노출",
            ", ".join(INTERNAL_TERMS) + " 금지",
            ", ".join(leaked) if leaked else "없음",
            not leaked,
        )
    )

    # --- 줄 나눔: 상한을 넘는 줄이 20%를 넘으면 실패 (설계서 F-a)
    lines_all = body_lines(body)
    over_lines = long_lines(body, rule.line_max)
    ratio = (len(over_lines) / len(lines_all)) if lines_all else 0.0
    checks.append(
        _check(
            "본문 줄 나눔",
            f"한 줄 {rule.line_max}자 이하 (넘는 줄이 전체의"
            f" {int(LINE_OVER_RATIO * 100)}% 이하)",
            f"{len(over_lines)}/{len(lines_all)}줄 초과 ({int(ratio * 100)}%)"
            + (f" 보기: {over_lines[0][:20]}…" if over_lines else ""),
            ratio <= LINE_OVER_RATIO,
        )
    )

    has_ph = bool(re.search(r"^\s*\{키워드\}\s*$", body, re.MULTILINE))
    checks.append(_check("{키워드} 자리표시자", "단독 줄로 1개", "있음" if has_ph else "없음", has_ph))
    if has_ph:
        idx = placeholder_paragraph_index(body)
        checks.append(
            _check(
                "{키워드} 문단 위치",
                f"{rule.placeholder_after_paragraph}번째 문단 뒤",
                f"{idx}번째 문단 뒤",
                idx == rule.placeholder_after_paragraph,
            )
        )

    # --- 오프닝 반복 (경고만, 설계서 F-c / H)
    if recent_openings:
        word = opening_word(body)
        repeated = bool(word) and word in list(recent_openings)
        checks.append(
            _check(
                "오프닝 반복",
                "최근 원고 20편과 첫 어절이 겹치지 않음",
                f"`{word}` 가 최근 원고와 겹침" if repeated else "겹치지 않음",
                not repeated,
                hard=False,
            )
        )
    if rule.extra_placeholder:
        extra = rule.extra_placeholder in body
        checks.append(
            _check(
                f"{rule.extra_placeholder} 자리표시자",
                "본문에 1개",
                "있음" if extra else "없음",
                extra,
            )
        )

    labels = [re.sub(r"\s+", "", c.label) for c in manuscript.comments]
    ok_labels = labels == list(COMMENT_LABELS)
    empty = [c.label for c in manuscript.comments if not (c.text or "").strip()]
    checks.append(
        _check(
            "댓글 12개 구조",
            " ".join(COMMENT_LABELS),
            f"{len(labels)}개" + (f" (빈 칸: {', '.join(empty)})" if empty else ""),
            ok_labels and not empty,
            scope="댓글",
        )
    )

    # --- 댓글 글자 수: soft_limits면 1.5배까지 경고, 그 위는 실패
    def _limit_of(label: str) -> int:
        name = re.sub(r"\s+", "", label)
        if name == "댓글2":
            return rule.comment2_max
        if name == "대대댓글2":
            return rule.reply2_max
        return rule.root_max

    # 상한의 200%를 넘을 때만 실패다. 그 이하는 경고도 내지 않는다
    # (경고가 재시도를 불러 글 완성도를 깎던 문제, 사용자 지시 2026-09-21).
    way_over: list[str] = []
    for c in manuscript.comments:
        cap = int(_limit_of(c.label) * SOFT_LIMIT_FACTOR)
        size = len(c.text)
        if size > cap:
            way_over.append(f"{c.label} {size}자 → {cap}자로 줄일 것")
    factor = int(SOFT_LIMIT_FACTOR * 100)
    checks.append(
        _check(
            "댓글 글자 수",
            f"상한의 {factor}% 이하 (댓글2 {int(rule.comment2_max * SOFT_LIMIT_FACTOR)}자"
            f" 대대댓글2 {int(rule.reply2_max * SOFT_LIMIT_FACTOR)}자"
            f" 그 외 {int(rule.root_max * SOFT_LIMIT_FACTOR)}자)",
            ", ".join(way_over) if way_over else "모두 통과",
            not way_over,
            scope="댓글",
        )
    )

    # --- 댓글에 내부 용어가 새지 않았는가
    leaked_c = sorted(
        {
            f"{c.label}({w})"
            for c in manuscript.comments
            for w in leaked_internal_terms(c.text)
        }
    )
    checks.append(
        _check(
            "댓글 내부 용어 미노출",
            ", ".join(INTERNAL_TERMS) + " 금지",
            ", ".join(leaked_c) if leaked_c else "없음",
            not leaked_c,
            scope="댓글",
        )
    )

    # --- 브랜드를 처음 꺼내는 댓글이 스스로 물러서지 않는가
    first_node = next(
        (
            c
            for c in manuscript.comments
            if re.sub(r"\s+", "", c.label) == rule.first_mention_label
        ),
        None,
    )
    retreat = (
        [w for w in RETREAT_PHRASES if w in _squash(first_node.text)] if first_node else []
    )
    checks.append(
        _check(
            f"{rule.first_mention_label} 물러서기 금지",
            "제품보다 방법이 중요 / 결국은 습관 / 개인차 / 참고만 같은 말 금지"
            " (사용 계기와 느낀 변화 1가지를 쓴다)",
            ", ".join(retreat) if retreat else "없음",
            not retreat,
            scope="댓글",
        )
    )

    # --- 후기형: 작성자는 자기가 쓴 제품을 두고 되묻지 않는다
    if rule.manuscript_type == "후기형":
        mine = set(author_labels(rule.manuscript_type))
        asking = [
            f"{c.label}({w})"
            for c in manuscript.comments
            if re.sub(r"\s+", "", c.label) in mine
            and "?" in (c.text or "")
            for w in AUTHOR_QUESTION_TERMS
            if w in _squash(c.text)
        ]
        checks.append(
            _check(
                "작성자 되묻기 금지(후기형)",
                "작성자 댓글(" + ", ".join(sorted(mine)) + ")에 구매·효과 되묻기 금지",
                ", ".join(asking) if asking else "없음",
                not asking,
                scope="댓글",
            )
        )

    # --- 이모지 금지 (사용자 절대 규칙: 이모지는 모든 원고에서 제외)
    from v2r.content.sanitize import has_emoji

    title_body = [
        name
        for name, value in (("제목", manuscript.title), ("본문", body))
        if has_emoji(value)
    ]
    checks.append(
        _check(
            "이모지 포함",
            "제목·본문에 이모지 없음",
            ", ".join(title_body) + "에 이모지" if title_body else "없음",
            not title_body,
        )
    )
    emoji_comments = [c.label for c in manuscript.comments if has_emoji(c.text)]
    checks.append(
        _check(
            "댓글 이모지 포함",
            "댓글에 이모지 없음",
            ", ".join(emoji_comments) if emoji_comments else "없음",
            not emoji_comments,
            scope="댓글",
        )
    )

    # --- 제품명이 **실제로** 처음 나온 자리 (라벨 문자열이 아니라 본문을 본다, 설계서 D)
    product = rule.product_in_comment or rule.product
    actual = first_product_mention_label(manuscript, rule)
    checks.append(
        _check(
            "제품명 최초 언급 위치",
            f"{rule.first_mention_label} 에서 처음",
            f"{actual} 에서 먼저 나옴" if actual else "어느 댓글에도 제품명이 없음",
            actual == rule.first_mention_label,
            scope="댓글",
        )
    )

    # --- 대대댓글2 금칙어 (설계서 C)
    reply2 = next(
        (c for c in manuscript.comments if re.sub(r"\s+", "", c.label) == "대대댓글2"),
        None,
    )
    banned2 = (
        [w for w in REPLY2_BANNED_PHRASES if w in _squash(reply2.text)] if reply2 else []
    )
    checks.append(
        _check(
            "대대댓글2 금칙어",
            " / ".join(REPLY2_BANNED_PHRASES) + " 금지",
            "대대댓글2(" + ", ".join(banned2) + ")" if banned2 else "없음",
            not banned2,
            scope="댓글",
        )
    )

    # --- 근거 문장 옆 웃음 표기 금지 (사용자 지시 2026-09-22)
    laughs = [
        f"{c.label}: {p}"
        for c in manuscript.comments
        if re.sub(r"\s+", "", c.label) in EVIDENCE_COMMENT_LABELS
        for p in evidence_laughter_problems(c.text)
    ]
    checks.append(
        _check(
            "근거 문장 웃음 표기",
            ", ".join(EVIDENCE_COMMENT_LABELS) + " 의 근거 문장에 ㅋㅋ ㅎㅎ 금지"
            " (한계·마무리 문장에서만 쓴다. ㅠㅠ 는 괜찮다)",
            " / ".join(laughs) if laughs else "없음",
            not laughs,
            scope="댓글",
        )
    )

    # --- 대대댓글2 구조·문장 (기존 원고 구조 그대로, 2026-09-21 개편)
    struct2 = reply2_problems(reply2.text, rule) if reply2 else ["대대댓글2 없음"]
    checks.append(
        _check(
            "대대댓글2 구조",
            "제품 소개 → 근거 문장 → 대안의 한계 → 검색 유도, 완전한 문장 3~4개"
            if rule.manuscript_type != "후기형"
            else "저도 이거 먹는 중 + 기간·감량 수치 + 기관 검증 결과, 완전한 문장",
            " / ".join(struct2) if struct2 else "통과",
            not struct2,
            scope="댓글",
        )
    )

    # --- 지침이 자리까지 정해 둔 필수 멘트 (설계서 D)
    if rule.required_phrases:
        table = {re.sub(r"\s+", "", c.label): _squash(c.text) for c in manuscript.comments}
        missing = [
            f"{label}({options[0]})"
            for label, options in rule.required_phrases
            if not any(_squash(o) in table.get(label, "") for o in options)
        ]
        checks.append(
            _check(
                "필수 멘트",
                ", ".join(f"{label}={'/'.join(o)}" for label, o in rule.required_phrases),
                ", ".join(missing) + " 빠짐" if missing else "모두 있음",
                not missing,
                scope="댓글",
            )
        )

    # --- 댓글5는 수치·기간을 반드시 적는다 (설계서 G)
    c5 = next(
        (c for c in manuscript.comments if re.sub(r"\s+", "", c.label) == "댓글5"), None
    )
    has_number = bool(c5 and _NUMBER_RE.search(c5.text or ""))
    checks.append(
        _check(
            "댓글5 수치",
            "기간·감량 같은 수치를 1개 이상",
            "있음" if has_number else "댓글5 에 수치가 없음",
            has_number,
            scope="댓글",
        )
    )

    # --- 후기형 본문: 문단 끝 한글 이모티콘 + 요요 수치 (정리본, 2026-09-22)
    if rule.manuscript_type == "후기형":
        body_bad = review_body_problems(body)
        checks.append(
            _check(
                "후기형 본문 이모티콘·요요 수치",
                "문단 끝마다 ㅠㅠ ㅋㅋ ㅎㅎ !! 중 하나, 요요를 말하면 기간+kg",
                " / ".join(body_bad) if body_bad else "통과",
                not body_bad,
            )
        )
        # 댓글5 = 요요로 여러 번 실패했다는 토로, 수치는 3~10kg
        c5_bad = review_comment5_problems(c5.text if c5 else "")
        checks.append(
            _check(
                "후기형 댓글5 역할·수치",
                f"요요로 여러 번 실패 + 감량 수치는"
                f" {REVIEW_COMMENT_MIN_KG:g}~{REVIEW_COMMENT_MAX_KG:g}kg",
                " / ".join(c5_bad) if c5_bad else "통과",
                not c5_bad,
                scope="댓글",
            )
        )

    # --- 권위 근거는 원고 전체에서 한 번만 (사용자 원고 피드백 2026-09-22)
    c2 = next(
        (c for c in manuscript.comments if re.sub(r"\s+", "", c.label) == "댓글2"), None
    )
    dup_auth = (
        duplicate_authority_problems(c2.text if c2 else "", reply2.text if reply2 else "")
    )
    checks.append(
        _check(
            "권위 근거 중복",
            "의사·논문·기관·클리닉은 원고 전체에서 한 번만"
            " (댓글2에서 썼으면 대대댓글2는 성분·원리·체감으로)",
            " / ".join(dup_auth) if dup_auth else "없음",
            not dup_auth,
            scope="댓글",
        )
    )

    # --- 댓글3은 해결 경험·도움이 된 노력을 담는다 (바이럴 4종 지침)
    if rule.comment3_needs_solution:
        c3 = next(
            (c for c in manuscript.comments if re.sub(r"\s+", "", c.label) == "댓글3"),
            None,
        )
        c3_bad = comment3_role_problems(c3.text if c3 else "")
        checks.append(
            _check(
                "댓글3 역할",
                "비슷한 고충 + **해결된 경험이나 도움이 된 노력**",
                " / ".join(c3_bad) if c3_bad else "통과",
                not c3_bad,
                scope="댓글",
            )
        )

    # --- 근거 출처 겹침 (경고): 수면클리닉 이비인후과 의사 논문 같은 나열
    stacked = [
        f"{c.label}: {p}"
        for c in manuscript.comments
        for p in stacked_authority_problems(c.text)
    ]
    checks.append(
        _check(
            "근거 출처 하나만",
            "기관·사람·문헌을 겹쳐 붙이지 않는다 (출처는 1개)",
            " / ".join(stacked) if stacked else "없음",
            not stacked,
            hard=False,
            scope="댓글",
        )
    )

    # --- 고민 문장의 ㅋㅋ ㅎㅎ (경고): 그 자리는 ㅠㅠ 다
    worry_body = worry_laughter_problems(body)
    worry_comments = [
        f"{c.label}: {p}"
        for c in manuscript.comments
        for p in worry_laughter_problems(c.text)
    ]
    for scope, found in (("본문", worry_body), ("댓글", worry_comments)):
        checks.append(
            _check(
                f"고민 문장 웃음 표기({scope})",
                "걱정·고민을 말하는 문장에 ㅋㅋ ㅎㅎ 가 붙었는가만 본다"
                " (ㅠㅠ 는 있으면 좋지만 없어도 위반이 아니다)",
                " / ".join(found) if found else "없음",
                not found,
                hard=False,
                scope=scope,
            )
        )

    # --- 의도적 오탈자·띄어쓰기 (경고): 너무 반듯하면 AI 티가 난다 (사용자 결정 2026-09-22)
    typo_bad = intentional_typo_problems(body)
    typo_hits = intentional_typo_hits(body)
    checks.append(
        _check(
            "의도적 오탈자·띄어쓰기",
            "의도적 오탈자·띄어쓰기 오류가 본문에 있는가"
            " (정리본 [AI 티 제거] 규칙: 맞춤법 1~2개, 띄어쓰기 2~3개를 일부러 틀린다."
            f" 하나라도 있으면 통과, 하나도 없이 너무 반듯하면 위반. 기준 {TYPO_MIN_COUNT}개 이상)",
            ", ".join(typo_hits) if typo_hits else "0개 (하나도 안 틀렸다)",
            not typo_bad,
            hard=False,
            # 구간을 `참고` 로 둔다 = 검증 표에는 남지만 **재시도를 부르지 않는다**.
            # (본문/댓글 재시도 고리는 `violations(scope="본문")` `scope="댓글")` 만 본다)
            # 일부러 틀리라는 규칙 때문에 모델을 다시 부르면 토큰만 더 든다
            # (사용자 지시 2026-09-22 토큰 절약).
            scope=INFO_SCOPE,
        )
    )
    return checks


def failures(checks: list[dict]) -> list[str]:
    """필수 검증 실패 항목의 한국어 설명."""
    return [
        f"{c['항목']}: 기준 {c['기준']}, 실제 {c['실제']}"
        for c in checks
        if c["필수"] and not c["통과"]
    ]


def violations(checks: list[dict], scope: str = "", include_warnings: bool = True) -> list[str]:
    """모델에게 다시 시킬 때 들이밀 구체적인 위반 목록.

    사용자 지시(2026-09-19): 한 번 어긋났다고 포기하지 말고, **무엇이 어떻게**
    어긋났는지(`본문 312자 > 250자` 같은 형태로) 짚어서 될 때까지 다시 시킨다.

    구간이 `참고`인 항목은 **다시 시키지 않는 알림**이라 여기서 빼고 돌려준다
    (검증 표에는 그대로 남는다, 사용자 지시 2026-09-22 토큰 절약).
    """
    out: list[str] = []
    for c in checks:
        if c["통과"]:
            continue
        if not scope and c.get("구간") == INFO_SCOPE:
            continue
        if scope and c.get("구간") != scope:
            continue
        if not include_warnings and not c["필수"]:
            continue
        out.append(f"{c['항목']} — 기준 {c['기준']} 인데 실제 {c['실제']}")
    return out


def brand_of(manuscript: Manuscript) -> str:
    """원고에 새겨 둔 브랜드 이름 (`source`가 `generated:<브랜드>`)."""
    src = manuscript.source or ""
    return src.split(":", 1)[1] if ":" in src else src


def is_brand_manuscript(manuscript: Any) -> bool:
    """이 원고가 **브랜드 원고**인가 (일상 글·일상 글 댓글과 가르는 기준).

    브랜드 원고는 `source` 가 `generated:<브랜드>` 이고 그 브랜드가
    `BRAND_RULES` 에 등록돼 있다. 자사 카페 일상 글(xlsx에서 읽어 온 글)과
    제휴 일상 글은 여기에 해당하지 않는다 (사용자 결정 2026-09-22).
    """
    src = str(getattr(manuscript, "source", "") or "")
    if not src.startswith("generated:"):
        return False
    brand = src.split(":", 1)[1].strip()
    return any(rule.brand == brand for rule in BRAND_RULES.values())


# ------------------------------------------------------------------ 생성
def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _comment_nodes(payload: Any) -> list[CommentNode]:
    """모델 응답 → CommentNode 12개 (라벨 순서 고정)."""
    table: dict[str, str] = {}
    if isinstance(payload, dict):
        inner = payload.get("comments") if "comments" in payload else payload
        if isinstance(inner, dict):
            table = {re.sub(r"\s+", "", str(k)): _as_text(v) for k, v in inner.items()}
        elif isinstance(inner, list):
            payload = inner
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                label = re.sub(r"\s+", "", _as_text(item.get("label") or item.get("라벨")))
                if label:
                    table[label] = _as_text(item.get("text") or item.get("내용"))
    nodes: list[CommentNode] = []
    for label in COMMENT_LABELS:
        depth = len(label) - len(label.lstrip("대"))
        nodes.append(
            CommentNode(
                label=label,
                text=table.get(label, ""),
                depth=depth,
                role={0: "comment", 1: "reply", 2: "reply2", 3: "reply3"}[depth],
            )
        )
    return nodes


def _clean_body(body: str) -> str:
    """모델이 남긴 마크다운 장식과 과한 빈 줄을 정리한다."""
    out = re.sub(r"\*{1,3}", "", body or "")
    out = re.sub(r"^\s*본문\s*[:：]\s*", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip("\n")


def violation_names(items: list[str]) -> list[str]:
    """위반 문장(`항목 — 기준 … 인데 실제 …`)에서 **항목 이름만** 뽑는다."""
    return [str(t).split(" — ", 1)[0].strip() for t in items if str(t).strip()]


def _note_retry(stats: dict | None, slot: str, bad: list[str]) -> None:
    """이번 시도에서 **무엇 때문에** 다시 시키는지 `stats` 에 적어 둔다.

    (사용자 지시 2026-09-22 토큰 절약 후속 — 재시도 원인을 집계하려면 원인이
    남아 있어야 한다. 예전에는 시도 **횟수**만 남아서 왜 10번 돌았는지 알 수 없었다.)

    남는 모양: `stats["retry_reasons"] = {"본문": [[...], ...], "댓글": [[...], ...]}`
    — 리스트 한 칸이 시도 한 번이고, 그 안에 그때 걸린 항목 이름이 들어 있다.
    """
    if stats is None:
        return
    table = stats.setdefault("retry_reasons", {})
    table.setdefault(slot, []).append(violation_names(bad))


def _retry_note(items: list[str]) -> str:
    """다시 시킬 때 붙이는 구체적 위반 목록."""
    return (
        "\n\n<직전 시도에서 어긴 규칙 — 이번에는 반드시 전부 지킬 것>\n"
        + "\n".join(f"- {e}" for e in items)
        + "\n같은 실수를 되풀이하지 말고 위 항목을 하나하나 확인하면서 다시 써라"
    )


def generate_manuscript(
    rt: Any,
    brand: str,
    keyword: str,
    cafe: str = "",
    manuscript_type: str = "",
    guide_text: str = "",
    stats: dict | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    mode: str = "",
    examples: list[str] | None = None,
    recent_openings: list[str] | None = None,
    reference_brief: str = "",
    relevance: int | None = None,
    bridge_rationale: str = "",
) -> Manuscript:
    """키워드 한 개로 제목·본문·댓글 12개를 만든다.

    `relevance`/`bridge_rationale`을 주면(연관도 3=당위성일 때만) 본문 프롬프트에
    `build_body_prompt`가 【당위성 논리】 블록을 넣는다(사용자 지시 2026-09-24).
    값이 없으면 기존 동작 그대로다.

    검증에 걸리면 **무엇이 어떻게 어긋났는지 짚어서 될 때까지 다시 시킨다**
    (사용자 지시 2026-09-19). 안전장치로 본문·댓글 각각 `max_attempts`번까지만
    시도하고, 그래도 남은 위반은 포기 대신 `stats["unresolved"]`에 적어 둔다.
    본문이 통과했으면 본문은 그대로 두고 **댓글만** 다시 만든다.
    댓글 재시도는 직전 12개를 통째로 되보내지 않고 **걸린 자리만** 다시 받아
    라벨로 갈아 끼운다 (입력 토큰 절약).

    `mode`:
    - `single` (기본): 본문 1회 + 댓글 1회로 나눠 부른다
    - `combined`: 본문과 댓글 12개를 **한 번에** 받는다. 댓글만 어긋나면
      위의 부분 재시도로 이어 붙인다

    `stats`를 주면 시도 횟수(`body_attempts`/`comment_attempts`/`attempts`)와
    끝내 못 지킨 규칙(`unresolved`), 토큰 사용량(`usage`)과 어림 비용
    (`estimated_usd`)을 그 dict에 담아 준다.
    """
    llm = getattr(rt, "llm", None) or rt
    if llm is None:
        raise BrandWriteError("모델을 쓸 수 없습니다 (ANTHROPIC_API_KEY를 확인하세요)")
    rule = rule_for(brand, manuscript_type)
    if not (keyword or "").strip():
        raise BrandWriteError("작성 키워드가 비어 있습니다")
    cap = max(1, int(max_attempts))
    mode = (mode or DEFAULT_MODE).strip() or "single"
    before = _usage_snapshot(llm)

    if mode == "combined":
        return _generate_combined(
            llm, rule, brand, keyword, cafe, guide_text, stats, cap, before, examples,
            reference_brief, relevance, bridge_rationale,
        )

    body_sys, body_user = build_body_prompt(
        brand, keyword, cafe, guide_text, rule.manuscript_type, examples, reference_brief,
        relevance, bridge_rationale,
    )
    draft: Manuscript | None = None
    body_bad: list[str] = []
    body_attempts = 0
    for body_attempts in range(1, cap + 1):
        if draft is not None and body_bad:
            # 두 번째 시도부터는 **직전 본문을 고쳐 쓰게** 한다 (통째로 다시 쓰지 않는다)
            user = build_body_fix_user(keyword, draft.body, body_bad)
        else:
            user = body_user
        try:
            data = llm.complete_json("brand_body", body_sys, user, max_tokens=2500)
        except Exception as exc:  # 모델 오류는 재시도로 풀리지 않는다
            raise BrandWriteError(f"본문 생성 실패({brand}/{keyword}): {exc}") from exc
        if not isinstance(data, dict):
            body_bad = ["출력 형식 — 기준 JSON 객체 하나 인데 실제 다른 형식"]
            continue
        title = _as_text(data.get("title") or data.get("제목")) or (
            draft.title if draft else ""
        )
        body = _clean_body(_as_text(data.get("body") or data.get("본문")))
        if not body:
            body_bad = ["본문 — 기준 내용이 있어야 함 인데 실제 비어 있음"]
            continue
        # 자리표시자는 규칙이 자리를 정해 둔 값이라 **코드가** 넣는다 (모델에게 맡기지 않는다)
        body = apply_placeholders(body, rule, keyword)
        candidate = Manuscript(
            title=title,
            body=body,
            cafe=cafe,
            keyword=keyword,
            tags=tags_from_keyword(keyword),
            manuscript_type=rule.manuscript_type,
            source=f"generated:{rule.brand}",
            comments=_comment_nodes({}),
            content_hash=content_hash(title, body),
        )
        bad = violations(validate(candidate, rule), scope="본문")
        _note_retry(stats, "본문", bad)
        # 위반이 적은 쪽을 들고 간다 (끝내 못 지켜도 버리지 않기 위해)
        if draft is None or len(bad) < len(body_bad):
            draft, body_bad = candidate, bad
            # 이 원고를 만든 호출의 길·프롬프트 지문을 남긴다
            _note_call(llm, stats, "body")
        if not bad:
            break
    if draft is None:
        raise BrandWriteError(f"본문 생성 실패({brand}/{keyword}): 쓸 만한 본문을 받지 못했습니다")

    # 오프닝 첫 어절이 최근 원고와 겹치면 딱 한 번만 다시 써 본다 (설계서 F-c)
    body_attempts += _retry_repeated_opening(
        llm, draft, rule, brand, keyword, body_sys, body_user, recent_openings
    )

    comment_attempts = _fill_comments(
        llm, draft, rule, brand, keyword, guide_text, cap, examples=examples, stats=stats
    )
    if all(not c.text for c in draft.comments):
        raise BrandWriteError(f"댓글 생성 실패({brand}/{keyword}): 댓글을 하나도 받지 못했습니다")

    _finish(
        draft,
        rule,
        stats,
        body_attempts,
        comment_attempts,
        cap,
        llm,
        before,
        "single",
        recent_openings,
    )
    return draft


def _retry_repeated_opening(
    llm: Any,
    draft: Manuscript,
    rule: BrandRule,
    brand: str,
    keyword: str,
    body_sys: str,
    body_user: str,
    recent_openings: list[str] | None,
) -> int:
    """오프닝 첫 어절이 최근 원고와 겹치면 **한 번만** 본문을 다시 받는다.

    다시 받은 본문이 검증을 통과하고 오프닝도 안 겹칠 때만 바꿔 끼운다.
    돌려주는 값은 늘어난 시도 횟수(0 또는 1)다.
    """
    words = [w for w in (recent_openings or []) if w]
    word = opening_word(draft.body)
    if not words or not word or word not in words:
        return 0
    note = _retry_note(
        [
            f"오프닝 반복 — 기준 최근 원고와 다른 첫 어절 인데 실제 `{word}` 로 또 시작함"
            " (다른 소재·다른 문장 구조로 완전히 새로 시작할 것)"
        ]
    )
    try:
        data = llm.complete_json("brand_body", body_sys, body_user + note, max_tokens=2500)
    except Exception:  # 다시 쓰기는 덤이라 실패해도 원고를 버리지 않는다
        return 1
    if not isinstance(data, dict):
        return 1
    body = _clean_body(_as_text(data.get("body") or data.get("본문")))
    if body:
        body = apply_placeholders(body, rule, keyword)
    title = _as_text(data.get("title") or data.get("제목")) or draft.title
    if not body or opening_word(body) in words:
        return 1
    candidate = Manuscript(
        title=title,
        body=body,
        cafe=draft.cafe,
        keyword=keyword,
        tags=tags_from_keyword(keyword),
        manuscript_type=rule.manuscript_type,
        source=f"generated:{rule.brand}",
        comments=_comment_nodes({}),
        content_hash=content_hash(title, body),
    )
    if not violations(validate(candidate, rule), scope="본문"):
        draft.title, draft.body = title, body
        draft.content_hash = content_hash(title, body)
    return 1


def _usage_snapshot(llm: Any) -> dict:
    """호출 전 토큰 누계를 복사해 둔다 (이 원고가 쓴 몫만 빼내기 위해)."""
    usage = getattr(llm, "usage", None)
    if not isinstance(usage, dict):
        return {}
    # `by_model` 안쪽까지 복사해야 나중에 뺄 때 같은 dict를 보는 일이 없다
    import copy

    return copy.deepcopy(usage)


def _usage_delta(llm: Any, before: dict) -> dict:
    """이 원고 한 건이 쓴 토큰만 빼낸다 (모델별 누계 `by_model`도 같이 뺀다).

    주의: 라우터의 토큰 누계는 **프로세스 전체가 함께 쓰는 하나**다. 그래서
    브랜드 2개를 동시에 돌리면(`brand_batch`) 이 뺄셈에 옆 원고의 몫이 섞인다.
    묶음 전체의 정확한 수치는 사용량 장부(`data/llm_usage-YYYY-MM.jsonl`)를 봐야
    한다 — 거기는 호출 한 건이 한 줄이라 섞이지 않는다.
    """
    usage = getattr(llm, "usage", None)
    if not isinstance(usage, dict):
        return {}
    out: dict = {}
    for key, value in usage.items():
        if isinstance(value, int):
            out[key] = value - int(before.get(key, 0) or 0)
    # `by_model`(단가표를 태우는 자리)과 `by_backend`(길별 누계) 둘 다 뺀다
    for bucket in ("by_model", "by_backend"):
        now_groups = usage.get(bucket)
        if not isinstance(now_groups, dict):
            continue
        old_groups = before.get(bucket)
        old_groups = old_groups if isinstance(old_groups, dict) else {}
        per: dict = {}
        for name, counts in now_groups.items():
            old = old_groups.get(name) or {}
            if not isinstance(counts, dict):
                continue
            per[name] = {
                k: int(v) - int(old.get(k, 0) or 0)
                for k, v in counts.items()
                if isinstance(v, int)
            }
        out[bucket] = per
    return out


def _note_call(llm: Any, stats: dict | None, slot: str) -> None:
    """방금 한 모델 호출의 **길**과 **프롬프트 지문**을 `stats`에 적어 둔다.

    지문(`prompt_sha256`)은 system+user 문자열의 sha256이다. 요금제 길로 만든
    원고와 API 길로 만든 원고의 지문이 같으면 **같은 지침으로 만들었다는 증거**가
    된다 (설계서 §품질 동일성).
    """
    if stats is None:
        return
    call = getattr(llm, "last_call", None)
    if not isinstance(call, dict) or not call:
        return
    stats[f"{slot}_backend"] = call.get("backend", "")
    stats[f"{slot}_prompt_sha256"] = call.get("prompt_sha256", "")
    stats[f"{slot}_model"] = call.get("model", "")


def _finish(
    draft: Manuscript,
    rule: BrandRule,
    stats: dict | None,
    body_attempts: int,
    comment_attempts: int,
    cap: int,
    llm: Any,
    before: dict,
    mode: str,
    recent_openings: list[str] | None = None,
) -> None:
    """해시를 다시 찍고 `stats`(시도 횟수·남은 위반·토큰·어림 비용)를 채운다."""
    draft.content_hash = content_hash(draft.title, draft.body)
    checks = validate(draft, rule, recent_openings=recent_openings)
    unresolved = violations(checks)
    # 경고를 뺀 **필수 실패**만 따로 — 이게 남으면 원고는 미완성이다
    hard = violations(checks, include_warnings=False)
    if stats is None:
        return
    from v2r.llm import usage_ledger as cache_ledger
    from v2r.llm.router import estimate_cost

    stats["mode"] = mode
    stats["body_attempts"] = body_attempts
    stats["comment_attempts"] = comment_attempts
    stats["attempts"] = body_attempts + comment_attempts
    # 재시도를 부른 항목을 많이 걸린 차례로 (사용자 지시 2026-09-22 — 원인 집계용)
    from collections import Counter

    counter: Counter = Counter()
    for rounds in (stats.get("retry_reasons") or {}).values():
        for names in rounds:
            counter.update(names)
    stats["retry_top"] = [
        {"항목": name, "걸린 시도 수": n} for name, n in counter.most_common()
    ]
    stats["unresolved"] = unresolved
    stats["unresolved_hard"] = hard
    stats["ok"] = not hard
    stats["hit_cap"] = bool(unresolved) and (
        body_attempts >= cap or comment_attempts >= cap
    )
    # 어느 길로 만들었는가 + 프롬프트 지문 (본문·댓글 각각).
    # 길이 달라도 지문이 같으면 같은 지침으로 만든 것이다.
    body_backend = stats.get("body_backend", "")
    stats["backend"] = body_backend or stats.get("comments_backend", "")
    stats["prompt_sha256"] = {
        "body": stats.get("body_prompt_sha256", ""),
        "comments": stats.get("comments_prompt_sha256", ""),
    }
    usage = _usage_delta(llm, before)
    stats["usage"] = usage
    stats["by_backend"] = usage.get("by_backend") or {}
    stats["input_tokens"] = usage.get("input_tokens", 0)
    stats["output_tokens"] = usage.get("output_tokens", 0)
    stats["cache_read_input_tokens"] = usage.get("cache_read_input_tokens", 0)
    stats["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0)
    # 요금제 길은 최상위 누계에 넣지 않으니 길별 누계(`by_backend`)에서 찾아 합친다
    counts = {
        key: int(usage.get(key, 0) or 0) for key in cache_ledger.TOKEN_FIELDS
    }
    for per in (usage.get("by_backend") or {}).values():
        if not isinstance(per, dict):
            continue
        for key in cache_ledger.TOKEN_FIELDS:
            counts[key] += int(per.get(key, 0) or 0)
    # 이번 원고가 읽은 입력 가운데 **캐시로 지나간 몫** (1에 가까울수록 좋다)
    stats["cache_hit_ratio"] = round(cache_ledger.cache_hit_ratio(counts), 4)
    stats["estimated_usd"] = estimate_cost(usage)


def _fill_comments(
    llm: Any,
    draft: Manuscript,
    rule: BrandRule,
    brand: str,
    keyword: str,
    guide_text: str,
    cap: int,
    seed: bool = False,
    examples: list[str] | None = None,
    stats: dict | None = None,
) -> int:
    """본문이 정해진 뒤 댓글 12개를 채운다. 두 번째 시도부터는 **걸린 자리만** 다시 받는다.

    `seed=True`면 원고에 이미 들어 있는 댓글을 출발점으로 삼아 **첫 시도부터**
    부분 재시도를 쓴다 (`combined` 모드에서 댓글만 어긋났을 때).
    """
    cmt_sys, cmt_user = build_comments_prompt(
        brand,
        keyword,
        draft.title,
        draft.body,
        rule.manuscript_type,
        guide_text,
        examples,
    )
    best_comments: list[CommentNode] = []
    comment_bad: list[str] = []
    comment_attempts = 0
    if seed and any((c.text or "").strip() for c in draft.comments):
        best_comments = list(draft.comments)
        comment_bad = violations(validate(draft, rule), scope="댓글")
    for comment_attempts in range(1, cap + 1):
        if not best_comments:
            try:
                payload = llm.complete_json(
                    "brand_comments", cmt_sys, cmt_user, max_tokens=3000
                )
            except Exception as exc:
                raise BrandWriteError(f"댓글 생성 실패({brand}/{keyword}): {exc}") from exc
            _note_call(llm, stats, "comments")
            draft.comments = _comment_nodes(payload)
        else:
            labels = failing_comment_labels(validate(draft, rule))
            sys_p, user_p = build_partial_retry_prompt(
                brand,
                keyword,
                labels,
                comment_bad,
                rule.manuscript_type,
                guide_text,
                examples,
            )
            try:
                payload = llm.complete_json("brand_comments", sys_p, user_p, max_tokens=1500)
            except Exception as exc:
                raise BrandWriteError(f"댓글 생성 실패({brand}/{keyword}): {exc}") from exc
            draft.comments = merge_comments(best_comments, payload)
        bad = violations(validate(draft, rule), scope="댓글")
        _note_retry(stats, "댓글", bad)
        if not best_comments or len(bad) < len(comment_bad):
            best_comments, comment_bad = list(draft.comments), bad
        if not bad:
            break
    draft.comments = best_comments
    return comment_attempts


def _generate_combined(
    llm: Any,
    rule: BrandRule,
    brand: str,
    keyword: str,
    cafe: str,
    guide_text: str,
    stats: dict | None,
    cap: int,
    before: dict,
    examples: list[str] | None = None,
    reference_brief: str = "",
    relevance: int | None = None,
    bridge_rationale: str = "",
) -> Manuscript:
    """본문 + 댓글 12개를 한 번에 받는 방식 (`mode="combined"`).

    본문이 어긋나면 통째로 다시, 댓글만 어긋나면 **걸린 자리만** 다시 받는다.
    """
    sys_p, user_p = build_combined_prompt(
        brand, keyword, cafe, guide_text, rule.manuscript_type, examples, reference_brief,
        relevance, bridge_rationale,
    )
    draft: Manuscript | None = None
    bad_all: list[str] = []
    attempts = 0
    for attempts in range(1, cap + 1):
        user = user_p + (_retry_note(bad_all) if bad_all else "")
        try:
            data = llm.complete_json("brand_body", sys_p, user, max_tokens=4500)
        except Exception as exc:
            raise BrandWriteError(f"원고 생성 실패({brand}/{keyword}): {exc}") from exc
        if not isinstance(data, dict):
            bad_all = ["출력 형식 — 기준 JSON 객체 하나 인데 실제 다른 형식"]
            continue
        title = _as_text(data.get("title") or data.get("제목"))
        body = _clean_body(_as_text(data.get("body") or data.get("본문")))
        if not body:
            bad_all = ["본문 — 기준 내용이 있어야 함 인데 실제 비어 있음"]
            continue
        body = apply_placeholders(body, rule, keyword)
        candidate = Manuscript(
            title=title,
            body=body,
            cafe=cafe,
            keyword=keyword,
            tags=tags_from_keyword(keyword),
            manuscript_type=rule.manuscript_type,
            source=f"generated:{rule.brand}",
            comments=_comment_nodes(data.get("comments") or data.get("댓글") or {}),
            content_hash=content_hash(title, body),
        )
        checks = validate(candidate, rule)
        body_bad = violations(checks, scope="본문")
        bad = violations(checks)
        if draft is None or len(bad) < len(bad_all):
            draft, bad_all = candidate, bad
            # 한 번에 받는 방식이라 본문·댓글이 같은 호출에서 나온다
            _note_call(llm, stats, "body")
            _note_call(llm, stats, "comments")
        if not body_bad:
            break
    if draft is None:
        raise BrandWriteError(f"원고 생성 실패({brand}/{keyword}): 쓸 만한 본문을 받지 못했습니다")

    comment_attempts = 0
    if violations(validate(draft, rule), scope="댓글") or all(
        not c.text for c in draft.comments
    ):
        comment_attempts = _fill_comments(
            llm,
            draft,
            rule,
            brand,
            keyword,
            guide_text,
            cap,
            seed=True,
            examples=examples,
            stats=stats,
        )
    if all(not c.text for c in draft.comments):
        raise BrandWriteError(f"댓글 생성 실패({brand}/{keyword}): 댓글을 하나도 받지 못했습니다")

    _finish(draft, rule, stats, attempts, comment_attempts, cap, llm, before, "combined")
    return draft


# ------------------------------------------------------------------ 출력
def sheet_text(manuscript: Manuscript) -> str:
    """시트 B열에 그대로 붙여 넣을 수 있는 원고 텍스트."""
    lines = ["제목 :", manuscript.title, "", "본문 :", manuscript.body, ""]
    for node in manuscript.comments:
        lines.append(f"{node.label} :")
        lines.append(node.text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def to_dict(manuscript: Manuscript, stats: dict | None = None) -> dict:
    """저장용 dict (원고 + 시트 텍스트 + 검증 결과 + 시도 횟수)."""
    data = manuscript.model_dump()
    data["brand"] = brand_of(manuscript)
    data["sheet_text"] = sheet_text(manuscript)
    data["checks"] = validate(manuscript)
    if stats:
        data["stats"] = dict(stats)
        # 어느 길(backend)로 만들었고 어떤 프롬프트를 넣었는지를 **맨 위에도** 둔다.
        # 나중에 길별 품질을 비교할 때 이 두 값만 보면 된다 (설계서 §5).
        data["backend"] = stats.get("backend", "")
        data["prompt_sha256"] = dict(
            stats.get("prompt_sha256")
            or {
                "body": stats.get("body_prompt_sha256", ""),
                "comments": stats.get("comments_prompt_sha256", ""),
            }
        )
        # 요금제 길은 구독 안이라 추가 비용이 0원이다
        data["cost_usd"] = (
            0.0 if data["backend"] == "plan" else stats.get("estimated_usd", 0.0)
        )
    return data


def save_json(manuscript: Manuscript, path: str | Path, stats: dict | None = None) -> Path:
    """원고 1건을 JSON으로 저장."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(to_dict(manuscript, stats), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


#: 길 이름을 사람 말로
BACKEND_LABELS = {
    "plan": "요금제(구독) 길 — 추가 비용 0원",
    "batch": "배치 API 길",
    "api": "일반 API 길 — 비용 발생",
}


def backend_lines(stats: dict | None) -> list[str]:
    """검토 MD에 넣는 "어느 길로 만들었나" 줄들."""
    if not stats:
        return []
    backend = stats.get("backend") or ""
    if not backend:
        return []
    fingerprints = stats.get("prompt_sha256") or {}
    body = str(fingerprints.get("body", ""))
    comments = str(fingerprints.get("comments", ""))
    lines = [f"- 생성 경로(backend): **{backend}** — {BACKEND_LABELS.get(backend, backend)}"]
    if body or comments:
        # 지문이 같으면 다른 길로 만들어도 같은 지침을 넣었다는 뜻이다
        lines.append(
            f"- 프롬프트 지문(sha256): 본문 `{body[:16] or '-'}` / 댓글 `{comments[:16] or '-'}`"
        )
    return lines


def review_block(
    manuscript: Manuscript, heading_level: int = 2, stats: dict | None = None
) -> str:
    """원고 1건의 사람이 읽는 블록 (제목·본문·댓글 표·검증 표)."""
    rule = rule_for(brand_of(manuscript), manuscript.manuscript_type)
    roles = dict(ACCOUNT_ROLES_COMMON)
    roles.update(ACCOUNT_ROLES_BY_TYPE.get(rule.manuscript_type, {}))
    h = "#" * heading_level
    checks_all = validate(manuscript, rule)
    hard_fail = [c for c in checks_all if c["필수"] and not c["통과"]]
    status = (
        f"미완성 — 검증 실패 {len(hard_fail)}개 ({', '.join(c['항목'] for c in hard_fail)})"
        if hard_fail
        else "완료 — 검증 전부 통과"
    )
    out: list[str] = [
        f"{h} {manuscript.keyword} ({rule.brand} / {rule.manuscript_type})"
        + (" — 미완성" if hard_fail else ""),
        "",
        f"- 상태: **{status}**",
        f"- 키워드: `{manuscript.keyword}`",
        f"- 카페: {manuscript.cafe or '(지정 없음)'}",
        f"- 원고유형: {rule.manuscript_type}",
        f"- 본문 글자 수(공백 제외): {body_length(manuscript.body)}자"
        f" / 키워드 {keyword_hits(manuscript.body, manuscript.keyword)}회",
        *backend_lines(stats),
        "",
        f"{h}# 제목",
        "",
        f"> {manuscript.title}",
        "",
        f"{h}# 본문 (자리표시자 그대로)",
        "",
        "```text",
        manuscript.body,
        "```",
        "",
        f"{h}# 댓글 12개",
        "",
        "| 라벨 | 작성 계정 역할 | 글자 수 | 내용 |",
        "|---|---|---:|---|",
    ]
    for node in manuscript.comments:
        out.append(
            f"| {node.label} | {roles.get(node.label, '-')} | {len(node.text)} |"
            f" {_md_escape(node.text)} |"
        )
    out += ["", f"{h}# 검증 결과", "", "| 항목 | 기준 | 실제 | 결과 |", "|---|---|---|---|"]
    for check in checks_all:
        mark = "통과" if check["통과"] else ("실패" if check["필수"] else "경고")
        out.append(
            f"| {_md_escape(check['항목'])} | {_md_escape(check['기준'])} |"
            f" {_md_escape(check['실제'])} | {mark} |"
        )
    out.append("")
    return "\n".join(out)


def write_review_md(
    manuscripts: Manuscript | list[Manuscript],
    path: str | Path,
    title: str = "",
    stats: list[dict] | dict | None = None,
) -> Path:
    """사람이 읽는 검토용 MD를 쓴다.

    `stats`를 주면 원고마다 **어느 길(backend)로 만들었는지**와 프롬프트 지문을
    함께 적는다 (원고와 같은 차례여야 한다).
    """
    items = [manuscripts] if isinstance(manuscripts, Manuscript) else list(manuscripts)
    stat_list: list[dict] = (
        [stats] if isinstance(stats, dict) else list(stats or [])
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    head = title or (
        f"브랜드 원고 검토 — {brand_of(items[0]) if items else ''}"
    )
    unfinished = [
        m for m in items if any(c["필수"] and not c["통과"] for c in validate(m))
    ]
    parts = [f"# {head}", "", f"원고 {len(items)}건. 발행하지 않았고 시트에도 쓰지 않았다.", ""]
    if unfinished:
        parts += [
            f"> **미완성 {len(unfinished)}건**: "
            + ", ".join(f"`{m.keyword}`" for m in unfinished)
            + " — 검증을 끝내 통과하지 못했다. 그대로 쓰면 안 된다.",
            "",
        ]
    backends = sorted({str(s.get("backend")) for s in stat_list if s.get("backend")})
    if backends:
        parts += [
            "생성 경로(backend): "
            + ", ".join(f"`{b}` — {BACKEND_LABELS.get(b, b)}" for b in backends),
            "",
        ]
    for idx, m in enumerate(items):
        parts.append(
            review_block(
                m,
                heading_level=2,
                stats=stat_list[idx] if idx < len(stat_list) else None,
            )
        )
    target.write_text("\n".join(parts), encoding="utf-8")
    return target


__all__ = [
    "BACKEND_LABELS",
    "BRAND_RULES",
    "BrandRule",
    "backend_lines",
    "BrandWriteError",
    "COMMENT_LABELS",
    "INTERNAL_TERMS",
    "LINE_MAX",
    "LINE_OVER_RATIO",
    "SCENE_BANNED_PHRASES",
    "NO_SCENE_RULE",
    "REPLY2_MIN_LEN",
    "REPLY2_MAX_LEN",
    "reply2_problems",
    "reply2_sentences",
    "reply2_prompt_block",
    "REPLY2_BANNED_PHRASES",
    "RETREAT_PHRASES",
    "SOFT_LIMIT_FACTOR",
    "author_labels",
    "body_length",
    "body_lines",
    "examples_block",
    "first_product_mention_label",
    "load_examples",
    "load_recent_openings",
    "long_lines",
    "methods_already_tried",
    "opening_word",
    "placeholder_paragraph_index",
    "leaked_internal_terms",
    "insert_placeholder",
    "apply_placeholders",
    "build_body_fix_user",
    "build_body_prompt",
    "build_comments_prompt",
    "failures",
    "generate_manuscript",
    "keyword_hits",
    "review_block",
    "rule_for",
    "save_json",
    "sheet_text",
    "validate",
    "violations",
    "MAX_ATTEMPTS",
    "write_review_md",
    # --- 사용자 결정 2026-09-22
    "MINUS_COPY_BRANDS",
    "MINUS_COPY_RULE",
    "INTENTIONAL_TYPO_RULE",
    "authority_words_in",
    "shared_authority_words",
    "duplicate_authority_problems",
    "is_minus_copy_sentence",
    "minus_copy_problems",
    "intentional_typo_hits",
    "intentional_typo_problems",
    "is_brand_manuscript",
]
