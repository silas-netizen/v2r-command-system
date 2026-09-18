"""구글 시트(gviz CSV) 적재와 행 파서. DESIGN §1 E2, legacy §3·§5 기준."""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Callable
from urllib.parse import quote

import httpx

from v2r.content.manuscript import (
    Manuscript,
    content_hash,
    parse_article,
    tags_from_keyword,
)

GVIZ = "https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv"

#: 동기화 금지 문서
EXCLUDED_DOCUMENT_IDS = {"1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT"}

_LINK = re.compile(r"^https?://", re.IGNORECASE)


class SourceError(RuntimeError):
    """원본 적재 실패."""


def gviz_csv_url(
    spreadsheet_id: str,
    gid: str | int = 0,
    sheet: str = "",
    headers: int | None = None,
) -> str:
    """gviz CSV 내보내기 URL.

    - `sheet`(탭 이름)를 주면 `gid` 대신 탭 이름으로 지정한다.
    - `headers`는 gviz에게 "머리글 행이 몇 줄인가"를 알려 준다. **생략하면 gviz가
      스스로 추측하는데**, 모든 열이 문자열인 시트에서는 시트 전체를 머리글로 보고
      각 열의 값을 공백으로 이어 붙인 한 줄만 돌려주는 사고가 난다
      (팥순이 `게시글 쓰기 원본` 탭이 그랬다: 75행 → 31행, 후기형 행이 통째로 사라짐).
      `headers=0`을 주면 추측을 끄고 첫 줄부터 그대로 돌려준다.
    """
    if spreadsheet_id in EXCLUDED_DOCUMENT_IDS:
        raise SourceError(f"동기화 금지 문서입니다: {spreadsheet_id}")
    url = GVIZ.format(sid=spreadsheet_id)
    if sheet:
        url += "&sheet=" + quote(str(sheet))
    else:
        url += f"&gid={gid}"
    if headers is not None:
        url += f"&headers={int(headers)}"
    return url


def fetch_csv(url: str, timeout: float = 5.0) -> list[dict]:
    """CSV URL을 읽어 dict 행 목록으로 반환."""
    for doc_id in EXCLUDED_DOCUMENT_IDS:
        if doc_id in url:
            raise SourceError(f"동기화 금지 문서입니다: {doc_id}")
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:  # 네트워크·상태 코드 모두
        raise SourceError(f"시트 가져오기 실패: {exc}") from exc
    return rows_from_csv(resp.text)


def _uniquify(names: list[str]) -> list[str]:
    """중복·빈 머리글을 자리마다 다른 키로 만든다.

    `csv.DictReader`는 같은 이름이 두 번 나오면 뒤엣것만 남겨 **열 자리가 밀린다**.
    A~J를 자리로 읽는 제휴 시트에서는 그게 곧 오배정이라, 자리를 보존한다.
    """
    out: list[str] = []
    taken: set[str] = set()
    for i, raw in enumerate(names):
        name = str(raw or "").strip() or f"_{i}"
        candidate = name
        n = 1
        while candidate.casefold() in taken:
            n += 1
            candidate = f"{name}_{n}"
        taken.add(candidate.casefold())
        out.append(candidate)
    return out


def rows_from_csv(text: str) -> list[dict]:
    """CSV 본문 → dict 행 목록. 첫 줄이 머리글, 열 자리는 보존한다."""
    table = list(csv.reader(io.StringIO(text or "")))
    if not table:
        return []
    width = max(len(r) for r in table)
    header = _uniquify(list(table[0]) + [""] * (width - len(table[0])))
    return [
        dict(zip(header, list(row) + [""] * (width - len(row)))) for row in table[1:]
    ]


# ---------------------------------------------------------------- 값 꺼내기


def _norm_key(key: Any) -> str:
    return re.sub(r"\s+", "", str(key or "")).casefold()


def _get(row: dict, aliases: tuple[str, ...]) -> str:
    """헤더 별칭으로 값 꺼내기."""
    normalized = {_norm_key(k): v for k, v in row.items()}
    for alias in aliases:
        v = normalized.get(_norm_key(alias))
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _by_letter(row: dict, letter: str, aliases: tuple[str, ...] = ()) -> str:
    """A~J 열 문자 또는 별칭으로 값 꺼내기 (없으면 열 순서)."""
    v = _get(row, (letter,) + aliases)
    if v:
        return v
    idx = ord(letter.upper()) - 65
    keys = list(row.keys())
    if idx < len(keys):
        return str(row[keys[idx]] or "").strip()
    return ""


_TITLE = ("제목", "title")
_BODY = ("본문", "내용", "body")
_CAFE = ("카페", "카페명", "G")
_BOARD = ("게시판", "게시판명", "H")


def parse_daily_rows(rows: list[dict], source: str = "") -> list[Manuscript]:
    """일상 글 시트 행 → 원고 목록 (source_row는 2부터)."""
    out: list[Manuscript] = []
    for i, row in enumerate(rows or [], start=2):
        title = _get(row, _TITLE)
        body = _get(row, _BODY)
        if not title and not body:
            # 제목 열이 없고 한 칸에 `제목 :`/`본문 :`이 합쳐진 형태
            combined = next(
                (str(v) for v in row.values() if v and "제목" in str(v)), ""
            )
            if not combined:
                continue
            parsed = parse_article(combined)
            title, body = parsed.title, parsed.body
        elif not title and body and "제목" in body:
            parsed = parse_article(body)
            title, body = parsed.title, parsed.body
        if not title and not body:
            continue
        out.append(
            Manuscript(
                title=title,
                body=body,
                cafe=_get(row, _CAFE),
                board=_get(row, _BOARD),
                source=source,
                source_row=i,
                content_hash=content_hash(title, body),
            )
        )
    return out


def _canonical_cafe(name: str, cafes_cfg: dict | None) -> str:
    """공백 제거 후 cafes.yaml의 정식 이름으로 매핑."""
    key = re.sub(r"\s+", "", name or "")
    if not key or not cafes_cfg:
        return key
    candidates: list[str] = []
    for group in ("affiliate", "self_owned"):
        for entry in cafes_cfg.get(group) or []:
            candidates.append(entry.get("name", ""))
    candidates += list((cafes_cfg.get("test") or {}).keys())
    for cand in candidates:
        if re.sub(r"\s+", "", cand).casefold() == key.casefold():
            return cand
    return key


def parse_adapted_rows(
    rows: list[dict], cafes_cfg: dict | None = None, source: str = ""
) -> list[Manuscript]:
    """각색 시트 행 → 원고 목록. 필수 헤더 4종."""
    required = ("카페명", "게시판명", "각색제목", "각색본문")
    if not rows:
        return []
    have = {_norm_key(k) for k in rows[0].keys()}
    missing = [r for r in required if _norm_key(r) not in have]
    if missing:
        raise SourceError("각색 시트 필수 헤더 누락: " + ", ".join(missing))

    out: list[Manuscript] = []
    for i, row in enumerate(rows, start=2):
        title = _get(row, ("각색제목",))
        body = _get(row, ("각색본문",))
        if not title and not body:
            continue
        out.append(
            Manuscript(
                title=title,
                body=body,
                cafe=_canonical_cafe(_get(row, ("카페명",)), cafes_cfg),
                board=_get(row, ("게시판명",)),
                source=source,
                source_row=i,
                content_hash=content_hash(title, body),
            )
        )
    return out


#: 제휴 시트 A~J 머리글 (하나라도 보이면 첫 줄이 머리글이다)
AFFILIATE_HEADERS = (
    "키워드",
    "본문",
    "카페명",
    "작성계정",
    "원고유형",
    "완료 링크",
    "말머리",
    "계정유형",
    "이미지 없음",
    "게시판명",
)


def header_row_is_data(rows: list[dict]) -> bool:
    """첫 줄이 머리글이 아니라 **데이터**인가 (머리글 없는 시트).

    머리글 별칭(또는 열 문자 `A`~`J`)이 하나도 안 보이면 데이터로 본다. 이때 그
    줄을 되살려야 A~J를 자리로 읽는 파서가 한 행도 잃지 않는다.
    """
    if not rows:
        return False
    keys = {_norm_key(k) for k in rows[0].keys()}
    known = tuple(AFFILIATE_HEADERS) + tuple("ABCDEFGHIJ")
    return not any(_norm_key(h) in keys for h in known)


def _restore_header_row(rows: list[dict]) -> tuple[list[dict], int]:
    """머리글이 없으면 머리글 자리의 값을 첫 행으로 되살린다. (행 목록, 시작 행번호)"""
    if not rows or not header_row_is_data(rows):
        return list(rows or []), 2
    first = {k: ("" if str(k).startswith("_") else k) for k in rows[0].keys()}
    return [first, *rows], 1


def parse_affiliate_rows(rows: list[dict], source: str = "") -> list[Manuscript]:
    """제휴/브랜드 시트 A~J 행 → 원고 목록. legacy §5-2 건너뛰기 규칙 적용."""
    out: list[Manuscript] = []
    rows, start_row = _restore_header_row(rows)
    for i, row in enumerate(rows or [], start=start_row):
        keyword = _by_letter(row, "A", ("키워드",))
        body = _by_letter(row, "B", ("본문",))
        cafe = _by_letter(row, "C", ("카페명",))
        account = _by_letter(row, "D", ("작성계정",))
        mtype = _by_letter(row, "E", ("원고유형",))
        done_link = _by_letter(row, "F", ("완료링크",))
        head = _by_letter(row, "G", ("말머리",))
        account_type = _by_letter(row, "H", ("계정유형",))
        no_image = _by_letter(row, "I", ("이미지없음",))
        board = _by_letter(row, "J", ("게시판명",))

        if _LINK.match(done_link or ""):
            continue  # 이미 완료된 행
        if not account and not account_type:
            continue  # 작성계정·계정유형 둘 다 비면 건너뜀
        if not keyword and not body:
            continue

        # 태그 규칙: 태그는 키워드 1개(공백 제거)뿐이다. A열이 비면 G열을 키워드로 본다.
        # 실제 브랜드 시트는 말머리를 안 쓰는 카페에서도 G열에 키워드를 적어 둔다
        # (예: `단호박 샐러드` → 태그 `단호박샐러드`) → 이때 G열은 말머리가 아니다.
        if not keyword and head:
            keyword, head = head, ""

        out.append(
            Manuscript(
                title=keyword,
                body=body,
                cafe=cafe,
                board=board,
                head=head,
                source=source,
                source_row=i,
                keyword=keyword,
                tags=tags_from_keyword(keyword),
                account=account,
                account_type=account_type,
                images_enabled=(no_image or "").strip().casefold() != "y",
                manuscript_type=mtype,
                content_hash=content_hash(keyword, body),
            )
        )
    return out


NO_CACHE_MESSAGE = "원본 캐시가 없습니다. '시트 동기화' 명령을 먼저 실행하세요"


def load_source(
    cfg_entry: dict,
    cache_get: Callable[[str], list[dict] | None] | None = None,
    cache_put: Callable[[str, list[dict]], None] | None = None,
    prefer_cache: bool = False,
) -> list[dict]:
    """시트 한 건 적재. 실패하면 마지막 캐시로 대체.

    `prefer_cache=True`(모의 실행)면 네트워크를 전혀 쓰지 않는다.
    캐시가 없으면 `SourceError`로 알린다.
    """
    sid = str(cfg_entry.get("spreadsheet_id") or "")
    gid = cfg_entry.get("gid", 0)
    sheet = str(cfg_entry.get("sheet") or "")
    headers = cfg_entry.get("headers")
    key = cfg_entry.get("name") or f"{sid}:{sheet or gid}"
    if prefer_cache:
        cached = cache_get(key) if cache_get else None
        if cached is not None:
            return cached
        raise SourceError(NO_CACHE_MESSAGE)
    # 금지 문서면 여기서 SourceError
    url = gviz_csv_url(sid, gid, sheet=sheet, headers=headers)
    try:
        rows = fetch_csv(url)
    except SourceError:
        cached = cache_get(key) if cache_get else None
        if cached is None:
            raise
        return cached
    if cache_put:
        cache_put(key, rows)
    return rows
