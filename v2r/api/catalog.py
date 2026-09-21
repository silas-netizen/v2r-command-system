"""카페·계정·게시판·말머리 카탈로그 (api-spec §3)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Iterable

from .client import V2RClient, field, walk_dicts
from .errors import V2RApiError, classify

log = logging.getLogger(__name__)

PATH_ACCOUNTS = "/navers/accounts"
PATH_JOIN_CAFES = "/naver_cafes/naver_join_cafes"
PATH_JOIN_CAFE = "/naver_cafes/naver_join_cafe"
PATH_SYNC_ACCOUNT = "/naver_cafes/naver_join_cafe/sync/account"
PATH_MENUS = "/naver_cafes/menus"
PATH_HEADS = "/naver_cafes/heads"

#: 게시판 권한 조회가 일시 오류(429·5xx·네트워크)로 실패했을 때 다시 부르기 전 대기(초).
#: 응답에 `Retry-After`가 있으면 그 값을 먼저 쓴다(상한 `MENU_RETRY_MAX_WAIT`).
MENU_RETRY_WAITS = (2.0, 5.0, 10.0, 20.0)
#: 한 번 기다릴 수 있는 최대 시간(초). 이보다 긴 제한은 기다리지 않고 건너뛴다.
MENU_RETRY_MAX_WAIT = 60.0
#: 다시 부르면 풀릴 수 있는 오류 종류 — 절대 조용히 건너뛰지 않는다.
TRANSIENT_MENU_KINDS = {"rate_limited", "server", "ambiguous", "network", "other"}


class CatalogError(Exception):
    """이름 매칭 실패/중복 등 카탈로그 오류."""


@dataclass(frozen=True)
class Cafe:
    """가입 카페."""

    cafe_id: int
    name: str


@dataclass(frozen=True)
class CafeAccount:
    """카페별 계정 연결정보."""

    login_id: str
    member_key: str | None = None
    nick: str | None = None
    level: Any = None
    level_name: str | None = None
    force_drop: bool = False
    stop_member: bool = False


@dataclass
class Menu:
    """게시판(메뉴). writable_accounts = 이 게시판에 쓸 수 있는 계정 집합."""

    menu_id: int
    name: str
    writable_accounts: set[str] = dc_field(default_factory=set)


@dataclass(frozen=True)
class Head:
    """말머리."""

    head_id: Any
    name: str


_NORMALIZE_RE = re.compile(r"[^0-9a-z가-힣]+")
_KOREAN_RE = re.compile(r"[^가-힣]+")


def normalize_name(s: str) -> str:
    """비교용 정규화: 소문자화 후 영숫자·한글만 남김."""
    return _NORMALIZE_RE.sub("", (s or "").casefold())


def korean_only(s: str) -> str:
    """한글만 남긴 문자열."""
    return _KOREAN_RE.sub("", s or "")


def match_name(query: str, candidates: Iterable[Any], key: Callable[[Any], str]) -> Any:
    """정규화 완전일치 → 한글만 완전일치 순으로 1건만 선택. 중복/없음이면 에러."""
    items = list(candidates)
    names = [key(item) for item in items]

    target = normalize_name(query)
    matches = [item for item, name in zip(items, names) if normalize_name(name) == target]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CatalogError(
            f"'{query}'와(과) 일치하는 항목이 여러 개입니다: "
            + ", ".join(key(m) for m in matches)
        )

    ktarget = korean_only(query)
    if ktarget:
        kmatches = [
            item for item, name in zip(items, names) if korean_only(name) == ktarget
        ]
        if len(kmatches) == 1:
            return kmatches[0]
        if len(kmatches) > 1:
            raise CatalogError(
                f"'{query}'와(과) 일치하는 항목이 여러 개입니다: "
                + ", ".join(key(m) for m in kmatches)
            )

    # 줄임말 허용: 정규화한 질의가 후보 이름에 포함되고 그 후보가 1건이면 채택 (예: 태극 → 태극마케팅센터)
    if target:
        pmatches = [item for item, name in zip(items, names) if target in normalize_name(name)]
        if len(pmatches) == 1:
            return pmatches[0]
        if len(pmatches) > 1:
            raise CatalogError(
                f"'{query}'와(과) 부분 일치하는 항목이 여러 개입니다: "
                + ", ".join(key(m) for m in pmatches)
            )

    raise CatalogError(
        f"'{query}'와(과) 일치하는 항목이 없습니다. 후보: " + ", ".join(names)
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "y", "yes"}
    return bool(value)


class Catalog:
    """V2R 카탈로그 조회 및 이름 해석."""

    def __init__(self, client: V2RClient) -> None:
        self.client = client
        #: 게시판 권한 조회에서 끝내 건너뛴 계정 → 사유(누적).
        self.unhealthy_accounts: dict[str, str] = {}
        #: 마지막 `menus()` 호출에서 건너뛴 계정 → 사유(호출자가 이벤트로 남긴다).
        self.last_menu_skips: dict[str, str] = {}

    # ---- 계정 ----
    def accounts(self) -> list[dict]:
        """`/navers/accounts` 계정 목록(정상 계정만)."""
        payload = self.client.get(PATH_ACCOUNTS)
        rows: list[dict] = []
        seen: set[str] = set()
        for d in walk_dicts(payload):
            login_id = field(d, "naver_login_id", "login_id", "loginId")
            if not isinstance(login_id, str) or not login_id or login_id in seen:
                continue
            if _truthy(field(d, "is_block", "isBlock")) or _truthy(
                field(d, "is_login_fail", "isLoginFail")
            ):
                continue
            seen.add(login_id)
            rows.append({**d, "login_id": login_id})
        return rows

    def account_ids(self) -> list[str]:
        """정상 계정 login_id 목록."""
        return [row["login_id"] for row in self.accounts()]

    # ---- 카페 ----
    def cafes(self) -> list[Cafe]:
        """가입 카페 목록."""
        payload = self.client.get(PATH_JOIN_CAFES)
        out: list[Cafe] = []
        seen: set[int] = set()
        for d in walk_dicts(payload):
            cafe_id = _as_int(field(d, "cafe_id", "cafeId"))
            if cafe_id is None or cafe_id in seen:
                continue
            name = field(
                d, "pc_cafe_name", "cafe_name", "cafeName", "mobile_cafe_name", "name"
            )
            if not isinstance(name, str) or not name:
                continue
            seen.add(cafe_id)
            out.append(Cafe(cafe_id=cafe_id, name=name))
        return out

    def cafe_accounts(self, cafe_id: int) -> list[CafeAccount]:
        """카페별 계정 연결정보. 탈퇴/활동중지 계정은 제외."""
        payload = self.client.get(PATH_JOIN_CAFE, params={"cafe_id": cafe_id})
        out: list[CafeAccount] = []
        seen: set[str] = set()
        for d in walk_dicts(payload):
            login_id = field(d, "login_id", "naver_login_id", "loginId")
            if not isinstance(login_id, str) or not login_id or login_id in seen:
                continue
            if "member_key" not in d and "memberKey" not in d:
                continue
            force_drop = _truthy(field(d, "force_drop", "forceDrop"))
            stop_member = _truthy(field(d, "stop_cafe_member", "stopCafeMember"))
            if force_drop or stop_member:
                continue
            level_info = field(d, "level_info", "levelInfo", default={}) or {}
            seen.add(login_id)
            out.append(
                CafeAccount(
                    login_id=login_id,
                    member_key=field(d, "member_key", "memberKey"),
                    nick=field(d, "nick_name", "nick", "nickname"),
                    level=field(level_info, "member_level", "memberLevel"),
                    level_name=field(level_info, "member_level_name", "memberLevelName"),
                    force_drop=force_drop,
                    stop_member=stop_member,
                )
            )
        return out

    # ---- 게시판 ----
    def _menus_payload(self, cafe_id: int, login_id: str) -> dict:
        """한 계정의 게시판 목록. 일시 오류는 지수 백오프로 다시 부른다.

        429·5xx·네트워크 오류는 **건너뛰지 않는다**(그렇게 건너뛰면 쓰기 가능 계정이
        조용히 쪼그라든다 — 2026-09-21 실측). 영구 오류만 그대로 올려보낸다.
        """
        last: Exception | None = None
        for attempt in range(len(MENU_RETRY_WAITS) + 1):
            try:
                return self.client.get(
                    PATH_MENUS, params={"cafe_id": cafe_id, "naver_login_id": login_id}
                )
            except V2RApiError as exc:
                kind = exc.kind or classify(exc)
                if kind not in TRANSIENT_MENU_KINDS:
                    raise
                wait = MENU_RETRY_WAITS[min(attempt, len(MENU_RETRY_WAITS) - 1)]
                if exc.retry_after is not None:
                    wait = float(exc.retry_after)
                if wait > MENU_RETRY_MAX_WAIT:
                    raise  # 장기 제한 — 기다리지 않고 호출자가 판단한다
                last = exc
            except Exception as exc:  # 네트워크·타임아웃 등
                wait = MENU_RETRY_WAITS[min(attempt, len(MENU_RETRY_WAITS) - 1)]
                last = exc
            if attempt >= len(MENU_RETRY_WAITS):
                break
            log.warning(
                "게시판 권한 조회 재시도(%s, %d회차, %.0f초 대기): %s",
                login_id, attempt + 1, wait, last,
            )
            time.sleep(wait)
        assert last is not None
        raise last

    def menus(self, cafe_id: int, login_ids: list[str]) -> list[Menu]:
        """계정별 쓰기 가능 게시판의 합집합.

        일시 오류는 재시도하고, 영구 오류(계정 없음·권한 없음 등)만 건너뛴다.
        건너뛴 계정과 사유는 `unhealthy_accounts`와 `last_menu_skips`에 남는다.
        """
        merged: dict[int, Menu] = {}
        self.last_menu_skips = {}
        for login_id in login_ids:
            try:
                payload = self._menus_payload(cafe_id, login_id)
            except V2RApiError as exc:
                kind = exc.kind or classify(exc)
                self.unhealthy_accounts[login_id] = kind
                self.last_menu_skips[login_id] = kind
                continue
            except Exception as exc:  # 네트워크 오류가 끝까지 안 풀린 경우
                self.unhealthy_accounts[login_id] = "network"
                self.last_menu_skips[login_id] = "network"
                log.warning("게시판 권한 조회 실패(%s): %s", login_id, exc)
                continue
            for d in walk_dicts(payload):
                menu_id = _as_int(field(d, "menuId", "menu_id"))
                name = field(d, "menuName", "menu_name")
                if menu_id is None or not isinstance(name, str) or not name:
                    continue
                # 필드 자체가 없으면 쓰기 가능으로 본다. 명시적 False만 제외.
                if not _truthy(field(d, "writable", "isWritable", default=True)):
                    continue
                menu = merged.get(menu_id)
                if menu is None:
                    menu = Menu(menu_id=menu_id, name=name)
                    merged[menu_id] = menu
                menu.writable_accounts.add(login_id)
        return list(merged.values())

    def heads(self, cafe_id: int, login_id: str, menu_id: int) -> list[Head]:
        """말머리 목록."""
        payload = self.client.get(
            PATH_HEADS,
            params={
                "cafe_id": cafe_id,
                "naver_login_id": login_id,
                "menu_id": menu_id,
            },
        )
        out: list[Head] = []
        seen: set[Any] = set()
        for d in walk_dicts(payload):
            head_id = field(d, "head_id", "headId")
            name = field(d, "head_name", "headName")
            if head_id is None or not isinstance(name, str) or not name:
                continue
            if head_id in seen:
                continue
            seen.add(head_id)
            out.append(Head(head_id=head_id, name=name))
        return out

    # ---- 해석 ----
    def resolve(
        self, cafe_name: str, board_name: str, login_id: str, head_name: str = ""
    ) -> tuple[Cafe, Menu, Head | None]:
        """카페명·게시판명(+말머리명)을 실제 객체로 해석.

        말머리는 `head_name`으로만 찾는다(게시판명으로 말머리를 찾는 건 무의미).
        """
        cafe = match_name(cafe_name, self.cafes(), key=lambda c: c.name)
        menus = self.menus(cafe.cafe_id, [login_id])
        if not menus:
            reason = self.unhealthy_accounts.get(login_id)
            detail = f" (계정 상태: {reason})" if reason else ""
            raise CatalogError(
                f"'{cafe.name}'에서 계정 {login_id}가 쓸 수 있는 게시판이 없습니다.{detail}"
            )
        menu = match_name(board_name, menus, key=lambda m: m.name)
        head: Head | None = None
        if head_name:
            heads = self.heads(cafe.cafe_id, login_id, menu.menu_id)
            if heads:
                try:
                    head = match_name(head_name, heads, key=lambda h: h.name)
                except CatalogError:
                    head = None
        return cafe, menu, head

    def sync_account(self, cafe_id: int, login_id: str) -> dict:
        """카페-계정 연결정보 동기화."""
        return self.client.put(
            PATH_SYNC_ACCOUNT, json={"cafe_id": cafe_id, "naver_login_id": login_id}
        )


__all__ = [
    "Cafe",
    "CafeAccount",
    "Catalog",
    "CatalogError",
    "Head",
    "Menu",
    "korean_only",
    "match_name",
    "normalize_name",
]
