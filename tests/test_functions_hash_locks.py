"""The hash lock is a projection of the role version authority, not a new owner."""
from pathlib import Path
import re

import pytest

from scripts.compile_functions_locks import pinned_versions

ROOT = Path(__file__).resolve().parents[1]


def test_hashed_functions_closure_is_exactly_the_canonical_role():
    canonical = (ROOT / "constraints/standard-py312.lock").read_text(encoding="utf-8")
    hashed = (ROOT / "constraints/functions-py312.hashes.lock").read_text(encoding="utf-8")
    assert pinned_versions(hashed) == pinned_versions(canonical)
    for requirement in re.split(r"(?m)(?=^[A-Za-z0-9])", hashed):
        if "==" in requirement:
            assert re.search(r"--hash=sha256:[0-9a-f]{64}", requirement)
    assert not pinned_versions((ROOT / "constraints/core-host-py312.lock").read_text(encoding="utf-8"))


def test_portable_hash_lock_preserves_all_functions_and_its_own_closure():
    canonical = pinned_versions((ROOT / "constraints/standard-py312.lock").read_text(encoding="utf-8"))
    portable = pinned_versions((ROOT / "packaging/portable_windows/requirements.lock").read_text(encoding="utf-8"))
    assert all(portable.get(name) == value for name, value in canonical.items())
    assert portable.keys() - canonical.keys() == {"edge-tts", "playwright", "pyee", "greenlet", "tabulate"}


@pytest.mark.parametrize("invalid", ["aiohttp>=3", "aiohttp==3\naiohttp==4", "-r elsewhere.txt"])
def test_hash_lock_projection_rejects_ambiguous_or_unpinned_requirements(invalid):
    with pytest.raises(ValueError):
        pinned_versions(invalid)
# HASHI-Fingerprint: hfp_6e6b28c1418c4e7ba74a177770db73f6
