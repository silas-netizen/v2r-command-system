"""V2R에 **이미 올라가 있는 글 목록** 색인 (`article_index`).

왜 필요한가
-----------
`publications` 표는 **이 시스템이 발행한 글**만 안다. 그런데 자사 카페 V2R 글 목록에는
이 시스템이 생기기 전에 (메이크/수동으로) 올린 글이 잔뜩 있다. 사용자 절대 규칙은
"자사 카페 V2R 글 목록에 이미 있는 모든 글과 앞으로 발행하는 모든 글 사이에 중복이
절대 없어야 함"이다. 그러니 **서버에 있는 글 목록 자체**를 우리 쪽에 베껴 두고
(그게 이 표다), 발행 직전에 그 목록과 대조한다.

한 행 = 카페에 올라간 글 1건. `(cafe_id, source_id)`로 유일하다.

- `title_norm`: 제목을 견줄 수 있게 정규화한 값 (소문자·공백/문장부호/이모지 제거,
  `ㅋㅋㅋ`/`ㅠㅠㅠ` 같은 반복 줄이기).
- `body_hash`: 본문까지 받아 왔을 때만 채운다. 원고 쪽 `content_hash()`와 **같은
  방식**이라 서로 바로 비교할 수 있다.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any, Iterable

from v2r.content.sanitize import strip_emoji
from v2r.store.db import now_iso

#: 한글 낱자(자모) 구간. NFKC는 호환 자모(`ㅋ` U+314B)를 조합용 자모(U+110F)로 바꾸기
#: 때문에 두 구간을 모두 살려 둔다. 그러지 않으면 `ㅋㅋ`가 통째로 사라진다.
_JAMO = "ᄀ-ᇿㄱ-ㆎ"

#: 제목 정규화에서 남기는 글자 — 숫자·영문·한글(완성형)·한글 낱자(ㅋ, ㅠ …)
_KEEP = re.compile(f"[^0-9a-z가-힣{_JAMO}]")

#: 같은 낱자가 2번 이상 이어지는 자리 (`ㅋㅋㅋㅋ`, `ㅠㅠㅠ`)
_JAMO_RUN = re.compile(f"([{_JAMO}])\\1+")


def normalize_title(text: str) -> str:
    """제목 → 비교용 정규화 문자열.

    소문자로 내리고, 이모지·공백·문장부호를 지우고, `ㅋㅋㅋ`/`ㅠㅠㅠ` 같은 반복은
    한 글자로 줄인다. "오늘 너무 웃겼어ㅋㅋㅋ"와 "오늘 너무 웃겼어ㅋㅋ!!"는 같은 제목이다.
    """
    raw = strip_emoji(str(text or ""))
    norm = unicodedata.normalize("NFKC", raw).casefold()
    norm = _KEEP.sub("", norm)
    return _JAMO_RUN.sub(r"\1", norm)


def _key(source_id: Any, article_id: Any) -> str:
    """색인 열쇠. `source_id`가 없으면 네이버 글 번호로 대신한다."""
    sid = str(source_id or "").strip()
    if sid:
        return sid
    aid = str(article_id or "").strip()
    return f"article:{aid}" if aid else ""


class ArticleIndexStore:
    """`article_index` 표 조작."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- 쓰기 ----
    def upsert(
        self,
        *,
        cafe_id: Any,
        cafe: str = "",
        source_id: Any = None,
        article_id: Any = None,
        login_id: str = "",
        title: str = "",
        body_hash: str | None = None,
    ) -> bool:
        """한 건 upsert. 새로 들어갔으면 True.

        `body_hash`를 주지 않으면 이미 저장된 값을 **지우지 않는다**(본문 조회는
        따로 돌기 때문이다). 제목·카페 이름 같은 값은 최신으로 덮어쓴다.
        """
        key = _key(source_id, article_id)
        if not key:
            return False
        ts = now_iso()
        cid = str(cafe_id or "")
        before = self.conn.execute(
            "SELECT 1 FROM article_index WHERE cafe_id = ? AND source_id = ?", (cid, key)
        ).fetchone()
        self.conn.execute(
            "INSERT INTO article_index (cafe_id, cafe, source_id, article_id, login_id,"
            " title, title_norm, body_hash, created_at, synced_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(cafe_id, source_id) DO UPDATE SET"
            "  cafe = CASE WHEN excluded.cafe != '' THEN excluded.cafe ELSE article_index.cafe END,"
            "  article_id = COALESCE(excluded.article_id, article_index.article_id),"
            "  login_id = COALESCE(NULLIF(excluded.login_id, ''), article_index.login_id),"
            "  title = CASE WHEN excluded.title != '' THEN excluded.title ELSE article_index.title END,"
            "  title_norm = CASE WHEN excluded.title != '' THEN excluded.title_norm"
            "               ELSE article_index.title_norm END,"
            "  body_hash = COALESCE(excluded.body_hash, article_index.body_hash),"
            "  synced_at = excluded.synced_at",
            (
                cid,
                str(cafe or ""),
                key,
                str(article_id) if article_id is not None else None,
                str(login_id or ""),
                str(title or ""),
                normalize_title(title),
                body_hash or None,
                ts,
                ts,
            ),
        )
        return before is None

    def upsert_many(self, rows: Iterable[dict]) -> dict:
        """여러 건 upsert → `{"new": n, "updated": n}`."""
        new = updated = 0
        for row in rows:
            if self.upsert(**row):
                new += 1
            else:
                updated += 1
        return {"new": new, "updated": updated}

    def set_body_hash(self, cafe_id: Any, source_id: str, body_hash: str) -> None:
        """본문 해시만 채운다."""
        self.conn.execute(
            "UPDATE article_index SET body_hash = ?, synced_at = ?"
            " WHERE cafe_id = ? AND source_id = ?",
            (body_hash or None, now_iso(), str(cafe_id or ""), str(source_id)),
        )

    # ---- 읽기 ----
    def count(self, cafe: str = "") -> int:
        """전체 또는 그 카페의 색인 건수."""
        if cafe:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM article_index WHERE cafe = ?", (cafe,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM article_index").fetchone()
        return int(row["n"])

    def rows_for_cafe(self, cafe: str) -> list[dict]:
        """그 카페 색인 전체(카페 이름 기준)."""
        rows = self.conn.execute(
            "SELECT * FROM article_index WHERE cafe = ?", (cafe,)
        ).fetchall()
        return [dict(r) for r in rows]

    def titles_for_cafe(self, cafe: str) -> list[tuple[str, str]]:
        """그 카페의 `(제목, title_norm)` 목록."""
        rows = self.conn.execute(
            "SELECT title, title_norm FROM article_index WHERE cafe = ?", (cafe,)
        ).fetchall()
        return [(str(r["title"] or ""), str(r["title_norm"] or "")) for r in rows]

    def title_match(self, cafe: str, title_norm: str) -> dict | None:
        """그 카페에 같은 `title_norm`이 있는가."""
        if not title_norm:
            return None
        row = self.conn.execute(
            "SELECT * FROM article_index WHERE cafe = ? AND title_norm = ?",
            (cafe, title_norm),
        ).fetchone()
        return dict(row) if row else None

    def hash_match(self, body_hash: str, cafes: "set[str] | None" = None) -> dict | None:
        """같은 본문 해시가 (지정한 카페들 안에) 있는가."""
        if not body_hash:
            return None
        rows = self.conn.execute(
            "SELECT * FROM article_index WHERE body_hash = ?", (body_hash,)
        ).fetchall()
        for r in rows:
            if cafes is None or str(r["cafe"] or "") in cafes:
                return dict(r)
        return None

    def rows_missing_body(self, cafe: str = "", limit: int = 0) -> list[dict]:
        """본문 해시가 아직 비어 있는 행."""
        sql = "SELECT * FROM article_index WHERE (body_hash IS NULL OR body_hash = '')"
        params: list[Any] = []
        if cafe:
            sql += " AND cafe = ?"
            params.append(cafe)
        sql += " ORDER BY synced_at"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]


__all__ = ["ArticleIndexStore", "normalize_title"]
