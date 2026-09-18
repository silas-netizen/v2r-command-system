"""2026-09-19 결정 사항 테스트: 사진 키워드 폴더, 짧은 일상 글, 각색 xlsx."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from v2r.command.parser import describe_spec, parse_korean_command
from v2r.sources import local_files
from v2r.warehouse import daily_generator
from v2r.warehouse.photo_collector import import_inbox
from v2r.warehouse.store import (
    NoPhotoError,
    Warehouse,
    brand_folder_name,
    token_folder_name,
    token_select_mode,
)


def _img(path: Path, color=(1, 2, 3), size=(24, 24)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


# --------------------------------------------------------------------
# 결정 1: 사진은 브랜드/키워드 폴더만 쓴다
# --------------------------------------------------------------------
def test_브랜드_별칭과_폴더명():
    assert brand_folder_name("뉴더미스") == "뉴더미스"
    assert brand_folder_name("자연방패") == "뉴더미스"  # 별칭
    assert brand_folder_name("없는브랜드") == "없는브랜드"


def test_토큰_폴더_규칙():
    # 공유 키워드 폴더 브랜드
    assert token_folder_name("팥순이", "키워드") == "키워드"
    assert token_folder_name("팥순이", "다이어트 유산균", "다이어트 유산균") == "키워드"
    assert token_folder_name("팥순이", "B/A") == "BA"
    assert token_folder_name("뉴더미스", "좌욕사진") == "좌욕사진"
    # 우아덤은 키워드 분기 대상이 아니다 → 토큰 그대로
    assert token_folder_name("우아덤", "다이어트", "다이어트") == "다이어트"
    assert token_select_mode("팥순이", "키워드") == "filename_match"
    assert token_select_mode("장으뜸", "키워드") == "random"


def test_keyword_folder_경로(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    root = tmp_path / "wh" / "images" / "originals"
    assert wh.keyword_folder("팥순이", "다이어트") == root / "팥순이" / "키워드"
    assert wh.keyword_folder("팥순이", "다이어트", token="B/A") == root / "팥순이" / "BA"
    assert wh.keyword_folder("자연방패", "키워드") == root / "뉴더미스" / "키워드"


def test_ensure_keyword_pool_브랜드_루트에서_채운다(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    brand_root = wh.originals_dir / "장으뜸"
    _img(brand_root / "원본1.jpg", (10, 20, 30))

    originals = wh.ensure_keyword_pool("장으뜸", "장어즙", min_variants=2)

    folder = wh.keyword_folder("장으뜸", "장어즙")
    assert folder.is_dir() and originals and originals[0].parent == folder
    variants = wh.washed_variants(wh.sha256(originals[0]))
    assert len(variants) >= 2


def test_ensure_keyword_pool_인박스에서_채운다(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    _img(wh.root / "inbox" / "image" / "image" / "우아덤" / "a.jpg", (5, 6, 7))

    originals = wh.ensure_keyword_pool("우아덤", "키워드", min_variants=1)
    assert originals and originals[0].parent == wh.keyword_folder("우아덤", "키워드")


def test_ensure_keyword_pool_사진이_없으면_한국어_오류(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    with pytest.raises(NoPhotoError) as exc:
        wh.ensure_keyword_pool("코숨핏", "코골이")
    message = str(exc.value)
    assert "사진이 필요합니다" in message
    assert "코숨핏" in message and "코골이" in message


def test_import_inbox_이중경로와_브랜드매핑(tmp_path: Path):
    wh = Warehouse(tmp_path / "wh")
    wh.ensure_dirs()
    base = wh.root / "inbox" / "image" / "image"
    _img(base / "팥순이" / "키워드" / "다이어트.jpg", (1, 1, 1))
    _img(base / "팥순이" / "BA" / "b.jpg", (2, 2, 2))
    _img(base / "팥순이" / "루트직접.jpg", (3, 3, 3))
    _img(base / "photowasher2.1" / "무시.jpg", (4, 4, 4))
    _img(base / "기관총_선택이미지" / "무시2.jpg", (5, 5, 5))

    stats = import_inbox(wh)

    assert stats["added"] == 3
    assert stats["brands"]["팥순이"]["키워드"] == 1
    assert stats["brands"]["팥순이"]["BA"] == 1
    assert stats["brands"]["팥순이"]["(루트)"] == 1
    assert "photowasher2.1" not in stats["brands"]
    # 인박스 파일은 그대로 남는다
    assert (base / "팥순이" / "키워드" / "다이어트.jpg").exists()


# --------------------------------------------------------------------
# 결정 2: 짧은 일상 글 생성
# --------------------------------------------------------------------
class _FakeRouter:
    """complete_json만 흉내내는 가짜 라우터(실제 API 호출 없음)."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def complete_json(self, purpose, system, user, max_tokens=1200):
        self.calls.append((purpose, system, user))
        return self.batches.pop(0) if self.batches else []


def test_generate_daily_pool_짧은글만_통과():
    payload = [
        {"title": "오늘 장보기 실패", "body": "우유만 사러갔는데 카트가 가득찼어요ㅋㅋ"},
        {"title": "마침표가 들어간 글.", "body": "이건 정리된다, 쉼표도 지워짐"},
        {"title": "가" * 30, "body": "제목이 너무 길다"},  # 탈락
        {"title": "사진 토큰", "body": "{키워드} 들어감"},  # 탈락
    ]
    router = _FakeRouter([payload])
    out = daily_generator.generate_daily_pool(router, ["양평맘"], 3, None)

    assert [m.title for m in out] == ["오늘 장보기 실패", "마침표가 들어간 글"]
    assert all("," not in m.body and "." not in m.body for m in out)
    assert all(m.images_enabled is False and m.cafe == "양평맘" for m in out)
    assert router.calls[0][0] == "daily_adapt"
    assert "20자 이내" in router.calls[0][1]


def test_generate_daily_pool_카페별_배치():
    batches = [
        [{"title": f"제목{i}", "body": f"본문{i}"} for i in range(20)],
        [{"title": f"추가{i}", "body": f"본문추가{i}"} for i in range(5)],
        [{"title": f"다른{i}", "body": f"본문다른{i}"} for i in range(3)],
    ]
    router = _FakeRouter(batches)
    out = daily_generator.generate_daily_pool(router, ["씨씨앙", "양평맘"], 25, None)
    assert len(out) == 28  # 씨씨앙 25 + 양평맘 3
    # 씨씨앙 20+5 두 번, 양평맘 3 한 번, 모자란 만큼 한 번 더 요청하고 빈 응답에 멈춘다
    assert len(router.calls) == 4


def test_daily_pool_파일_추가와_중복제거(tmp_path: Path):
    router = _FakeRouter([[{"title": "비오는 아침", "body": "우산을 또 놓고왔어요ㅠㅠ"}]])
    items = daily_generator.generate_daily_pool(router, ["고요한 아침"], 1, None)

    assert daily_generator.save_pool(tmp_path, items) == 1
    assert daily_generator.save_pool(tmp_path, items) == 0  # 같은 content_hash
    pool = daily_generator.load_pool(tmp_path)
    assert len(pool) == 1
    assert pool[0].source == "daily_pool" and pool[0].images_enabled is False


def test_generate_daily_명령_해석():
    spec = parse_korean_command("일상 글 30개 만들어줘")
    assert spec is not None
    assert spec.task == "generate_daily" and spec.count == 30
    assert "일상 글 생성" in describe_spec(spec)
    assert parse_korean_command("일상 글 생성해줘").task == "generate_daily"
    # 발행 명령은 여전히 발행으로
    assert parse_korean_command("일상 글 5개 올려줘").task == "publish_daily"


# --------------------------------------------------------------------
# 결정 3·7: 각색 xlsx = 자사 카페 일상 글
# --------------------------------------------------------------------
def _xlsx(path: Path, header: list[str], rows: list[list]) -> Path:
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)
    wb.close()
    return path


FULL_HEADER = ["카페명", "게시판명", "게시판링크", "작성계정", "각색제목", "각색본문"]
CAFE_HEADER = ["카페명", "게시판명", "각색제목", "각색본문", "등록시간", "상태"]


def test_discover_xlsx(tmp_path: Path):
    _xlsx(tmp_path / "20260915_각색_전체_1.xlsx", FULL_HEADER, [])
    _xlsx(tmp_path / "그냥파일.xlsx", FULL_HEADER, [])
    found = local_files.discover_xlsx(tmp_path)
    assert [e["name"] for e in found] == ["20260915_각색_전체_1"]
    assert found[0]["kind"] == "xlsx_daily"
    assert Path(found[0]["path"]).is_file()
    assert local_files.discover_xlsx(tmp_path / "없음") == []


def test_xlsx_전체_배치_파싱(tmp_path: Path):
    path = _xlsx(
        tmp_path / "20260915_각색_전체_1.xlsx",
        FULL_HEADER,
        [
            ["글로시마이", "수다방", "https://cafe.naver.com/x", "abc123", "제목1", "본문1"],
            ["송도포털", "자유게시판", "", "", "제목2", "본문2"],
            ["", "", "", "", "", ""],
        ],
    )
    out = local_files.parse_xlsx_entry({"name": path.stem, "path": str(path)})
    assert [m.title for m in out] == ["제목1", "제목2"]
    assert out[0].account == "abc123" and out[1].account == ""
    assert out[0].source == "20260915_각색_전체_1" and out[0].source_row == 2
    assert out[1].source_row == 3
    assert all(m.images_enabled is False for m in out)


def test_xlsx_카페별_배치는_발행된_행을_건너뛴다(tmp_path: Path):
    path = _xlsx(
        tmp_path / "20260814_각색_고요한아침_9_v2r(전체글 모음).xlsx",
        CAFE_HEADER,
        [
            ["고요한아침", "수다방", "제목A", "본문A", "", ""],
            ["고요한아침", "수다방", "제목B", "본문B", "2026-09-01 10:00", ""],
            ["고요한아침", "수다방", "제목C", "본문C", "", "완료"],
        ],
    )
    out = local_files.parse_xlsx_entry({"name": path.stem, "path": str(path)})
    assert [m.title for m in out] == ["제목A"]
    assert out[0].source_row == 2


def test_sniff_layout():
    assert local_files.sniff_layout(FULL_HEADER) == "full"
    assert local_files.sniff_layout(CAFE_HEADER) == "cafe"
