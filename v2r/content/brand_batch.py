"""묶음 생성 순서와 동시 실행 (2026-09-22 토큰 절약·속도).

두 가지만 한다.

1. **브랜드끼리 묶어 잇달아 만든다** (`order_bundles`). 프롬프트 캐시는 "앞부분이
   똑같을 때"만 걸리고 5분쯤 살아 있다. 같은 브랜드를 연달아 만들면 두 번째
   원고부터 지침을 다시 읽지 않는다. 브랜드가 왔다 갔다 하면 캐시가 매번 깨진다.
2. **브랜드는 최대 2개까지 동시에** 돌린다 (`run_bundles`). 같은 브랜드 안에서는
   반드시 **순차**다 — 동시에 쏘면 서로 캐시를 만들기 전에 출발해 지침을 두 번
   읽는다.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_MAX_BRANDS", "group_by_brand", "order_bundles", "run_bundles"]

#: 동시에 돌리는 브랜드 수 상한 (사용자 지시 2026-09-22: 브랜드별 최대 2개 동시)
DEFAULT_MAX_BRANDS = 2

#: 묶음 하나 = (브랜드, 원고유형)
Bundle = tuple[str, str]


def group_by_brand(bundles: Iterable[Bundle]) -> list[list[Bundle]]:
    """브랜드별 묶음 목록. 처음 나온 브랜드 차례를 그대로 지킨다.

    한 브랜드 안에서는 **원고유형끼리** 다시 붙인다 (질문형 → 질문형 → 후기형).
    유형이 다르면 system 프롬프트도 다르니 유형이 섞이면 캐시가 깨진다.
    """
    order: list[str] = []
    per: dict[str, dict[str, list[Bundle]]] = {}
    for brand, mtype in bundles:
        if brand not in per:
            per[brand] = {}
            order.append(brand)
        per[brand].setdefault(mtype, []).append((brand, mtype))
    out: list[list[Bundle]] = []
    for brand in order:
        flat: list[Bundle] = []
        for items in per[brand].values():
            flat.extend(items)
        out.append(flat)
    return out


def order_bundles(bundles: Iterable[Bundle]) -> list[Bundle]:
    """같은 브랜드(+유형)끼리 붙여 놓은 실행 순서."""
    return [item for group in group_by_brand(bundles) for item in group]


def run_bundles(
    bundles: Sequence[Bundle],
    run_one: Callable[[str, str, int], Any],
    max_brands: int = DEFAULT_MAX_BRANDS,
) -> list[Any]:
    """묶음을 브랜드별로 나눠 **최대 `max_brands`개 동시**로 만든다.

    `run_one(brand, manuscript_type, index)` 는 원고 1건을 만들어 결과를 돌려준다
    (`index` 는 원래 목록에서의 자리 = 결과 순서). 돌려주는 값은 **원래 순서**로
    맞춰 둔 결과 목록이다. 한 건이 터져도 나머지는 계속 만들고, 터진 자리에는
    `{"error": ...}` 를 넣는다.
    """
    items = list(bundles)
    if not items:
        return []
    # 같은 (브랜드, 유형)이 여러 번 나오면 자리 번호를 순서대로 나눠 준다
    seats: dict[Bundle, list[int]] = {}
    for i, item in enumerate(items):
        seats.setdefault(item, []).append(i)
    results: list[Any] = [None] * len(items)

    def run_group(group: list[Bundle]) -> None:
        for brand, mtype in group:  # 같은 브랜드 안에서는 반드시 순차 (캐시 재사용)
            index = seats[(brand, mtype)].pop(0)
            try:
                results[index] = run_one(brand, mtype, index)
            except Exception as exc:  # noqa: BLE001 - 한 건 실패가 묶음을 죽이지 않는다
                log.exception("묶음 생성 실패 (%s/%s): %s", brand, mtype, exc)
                results[index] = {"brand": brand, "type": mtype, "error": str(exc)}

    groups = group_by_brand(items)
    workers = max(1, min(int(max_brands or 1), len(groups)))
    if workers == 1:
        for group in groups:
            run_group(group)
        return results
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="brand") as pool:
        list(pool.map(run_group, groups))
    return results
