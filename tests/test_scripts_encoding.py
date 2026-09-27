"""모든 scripts/*.cmd 가 ASCII·CRLF 로만 쓰여 있는지 확인한다.

2026-09-26/27 사고: 노출 감시 스크립트(scripts/exposure-runner-watchdog.cmd)가
LF 로만 저장돼 23시간 동작하지 않았다. cmd.exe 는 순수 LF 줄바꿈이나 비-ASCII
바이트가 섞인 배치 파일을 깨뜨릴 수 있어, 이후 모든 .cmd 는 ASCII·CRLF 로만
작성하기로 했다(한글이 필요하면 scripts\\brands-*.txt 처럼 별도 UTF-8 파일로 뺀다).
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"


def _cmd_files():
    return sorted(SCRIPTS_DIR.glob("*.cmd"))


@pytest.mark.parametrize("path", _cmd_files(), ids=lambda p: p.name)
def test_cmd_file_is_ascii_only(path: Path):
    data = path.read_bytes()
    non_ascii = [i for i, b in enumerate(data) if b >= 128]
    assert not non_ascii, f"{path.name} has non-ASCII byte(s) at offset(s) {non_ascii[:5]}"


@pytest.mark.parametrize("path", _cmd_files(), ids=lambda p: p.name)
def test_cmd_file_uses_crlf_only(path: Path):
    data = path.read_bytes()
    bad_lf = [i for i, b in enumerate(data) if b == 10 and (i == 0 or data[i - 1] != 13)]
    assert not bad_lf, f"{path.name} has bare LF (no preceding CR) at offset(s) {bad_lf[:5]}"


def test_at_least_one_cmd_file_found():
    assert _cmd_files(), "scripts/*.cmd not found - check working directory"
