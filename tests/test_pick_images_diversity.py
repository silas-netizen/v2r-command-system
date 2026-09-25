"""2026-09-25 재발 방지: 같은 실행(연속 발행) 안에서 사진이 한 원본에 몰리지 않는다.

실측: 뉴더미스·우아덤·장으뜸에서 브랜드당 5건을 연속 발행했더니 전부 같은 원본
1장(세탁본만 다름)으로 몰렸다. 원인은 `pick_images`가 `키워드` 폴더 원본을 정렬된
고정 순서로 훑고, 첫 원본의 캐시된 세탁본이 여럿이라 `pick_variant`가 매번 통과돼
다음 원본으로 넘어가지 못했기 때문이다. 이제는 원본을 섞고, 실행(런타임) 단위로
"이미 쓴 원본 sha"를 기억해 다른 원본을 우선한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from v2r.command.spec import TaskSpec
from v2r.config import Settings
from v2r.content.manuscript import Manuscript
from v2r.engine.context import Runtime
from v2r.engine.publish import pick_images
from v2r.store.db import connect


def _img(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 40), color).save(path)
    return path


def _make_runtime(tmp_path: Path) -> Runtime:
    settings = Settings(
        v2r_email="tester@example.com",
        v2r_password="",
        data_dir=tmp_path / "data",
        warehouse_dir=tmp_path / "warehouse",
        db_path=tmp_path / "data" / "v2r.sqlite",
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "warehouse").mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    return Runtime.open(settings, conn)


def _manuscript(keyword: str) -> Manuscript:
    return Manuscript(
        title=f"{keyword} 제목",
        body="{키워드}\n본문",
        source="장으뜸",
        keyword=keyword,
    )


def test_연속_발행_5건이_서로_다른_원본을_고른다(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    root = rt.warehouse.originals_dir / "장으뜸"
    # 원본 5장을 브랜드 루트에 둔다(키워드 폴더가 비어 있으면 여기서 채운다).
    originals = [
        _img(root / f"원본{i}.jpg", (i * 10, i * 20, i * 30)) for i in range(1, 6)
    ]

    spec = TaskSpec(task="publish_batch")

    picked_originals: set[str] = set()
    for i, orig in enumerate(originals):
        keyword = f"키워드{i}"
        m = _manuscript(keyword)
        variants = pick_images(rt, m, spec, 1)
        assert len(variants) == 1
        sha = rt.scratch["variant_sha"][str(variants[0])]
        picked_originals.add(sha)
        # 사용 기록을 남겨야 다음 건에서도 "이미 쓴 세탁본"으로 잡힌다(실제 발행 흐름과 동일).
        from v2r.engine.publish import record_variant_use

        record_variant_use(rt, sha, variants[0], f"src-{i}")

    # 원본이 5장(발행 건수와 같음)이면 5건 모두 서로 다른 원본이어야 한다.
    assert len(picked_originals) == 5


def test_원본이_발행_건수보다_적으면_다_쓴_뒤_품절_에러(tmp_path: Path):
    """원본 2장으로 3건째를 고르면, 몰래 새 세탁본을 만들지 않고 품절 에러를 낸다.

    사진을 더 세탁하려면 '사진 생성 승인' 흐름(사용자 승인)을 거쳐야 한다
    (`notify_photo_shortage`) — `pick_images`가 조용히 더 만들면 안 된다.
    """
    from v2r.engine.publish import PublishError, record_variant_use

    rt = _make_runtime(tmp_path)
    root = rt.warehouse.originals_dir / "장으뜸"
    _img(root / "원본1.jpg", (10, 1, 1))
    _img(root / "원본2.jpg", (20, 1, 1))

    spec = TaskSpec(task="publish_batch")
    order: list[str] = []
    for i in range(2):
        m = _manuscript(f"키워드{i}")
        variants = pick_images(rt, m, spec, 1)
        sha = rt.scratch["variant_sha"][str(variants[0])]
        order.append(sha)
        record_variant_use(rt, sha, variants[0], f"src-{i}")

    # 원본 2장뿐이므로 앞의 두 건은 서로 다른 원본이어야 한다.
    assert order[0] != order[1]

    # 세 번째 건은 남은 미사용 세탁본이 없어 품절 에러가 나야 한다(자동 증산 금지).
    m3 = _manuscript("키워드2")
    with pytest.raises(PublishError):
        pick_images(rt, m3, spec, 1)
