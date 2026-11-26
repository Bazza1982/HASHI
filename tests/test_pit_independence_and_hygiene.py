"""Guardrail: locale integrity, test-suite anti-bloat hygiene, and resource independence."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from orchestrator.execution_resources import FileLease, leases

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCALE_DIR = REPO_ROOT / "locales" / "runtime"
TESTS_DIR = REPO_ROOT / "tests"

# Only obsolete branding and private personal names are banned unconditionally.
# Ordinary domain terminology must never be globally banned.
BANNED_BRAND_OR_PRIVATE_NAMES = (
    "小能",
    "Aptenra",
    "爸爸",
    "Lily",
    "Arale",
    "阿拉蕾",
    "zhaojun",
)

# Prevent re-introducing one-off PR/wave/remediation/blind-box test dumpsters.
FORBIDDEN_TEST_FILE_PATTERNS = (
    re.compile(r"^test_.*_pr\d+.*\.py$"),
    re.compile(r"^test_.*_remediation.*\.py$"),
    re.compile(r"^test_.*_wave_[a-z0-9_]+\.py$"),
    re.compile(r"^test_.*_failures?_details\.py$"),
)

# Existing Keep Shield files explicitly grandfathered by name.
ALLOWED_GRANDFATHERED_FILES = frozenset({"test_legacy_pr_gaps.py"})

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _load_json_rejecting_duplicates(path: Path) -> dict:
    def _reject_dups(pairs: list[tuple[str, object]]) -> dict:
        out: dict = {}
        for key, value in pairs:
            if key in out:
                raise AssertionError(f"Duplicate JSON key {key!r} in {path}")
            out[key] = value
        return out

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_dups)


def test_runtime_locales_have_no_duplicate_keys_or_stale_brand_names():
    locale_files = sorted(LOCALE_DIR.glob("*.json"))
    assert locale_files, f"No runtime locale files found under {LOCALE_DIR}"

    parsed: dict[str, dict] = {}
    for path in locale_files:
        data = _load_json_rejecting_duplicates(path)
        assert isinstance(data.get("strings"), dict), f"{path.name} missing 'strings' map"
        assert isinstance(data.get("commands"), dict), f"{path.name} missing 'commands' map"
        parsed[path.stem] = data

        hits = []
        for section in ("strings", "commands"):
            for key, value in data[section].items():
                if not isinstance(value, str):
                    continue
                for term in BANNED_BRAND_OR_PRIVATE_NAMES:
                    if term in value:
                        hits.append((path.name, f"{section}.{key}", term))
        assert not hits, f"Stale branding or private names found in {path.name}: {hits[:8]}"

    # Shared keys across en and zh-CN must keep matching format placeholders.
    if "en" in parsed and "zh-CN" in parsed:
        en_strings = parsed["en"]["strings"]
        zh_strings = parsed["zh-CN"]["strings"]
        mismatches = []
        for key in sorted(set(en_strings) & set(zh_strings)):
            if not (isinstance(en_strings[key], str) and isinstance(zh_strings[key], str)):
                continue
            en_ph = set(_PLACEHOLDER_RE.findall(en_strings[key]))
            zh_ph = set(_PLACEHOLDER_RE.findall(zh_strings[key]))
            if en_ph != zh_ph:
                mismatches.append((key, sorted(en_ph), sorted(zh_ph)))
        assert not mismatches, f"Locale placeholder mismatches between en and zh-CN: {mismatches[:8]}"


def test_suite_blocks_pr_wave_remediation_and_blindbox_dumpster_filenames():
    violations: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        name = path.name
        if name in ALLOWED_GRANDFATHERED_FILES:
            continue
        if any(pattern.match(name) for pattern in FORBIDDEN_TEST_FILE_PATTERNS):
            violations.append(path.relative_to(REPO_ROOT).as_posix())

    assert not violations, (
        "Do not add one-off PR/wave/remediation/failure_details dumpster test files; "
        f"add behavior tests to the owning module instead: {violations}"
    )


@pytest.mark.asyncio
async def test_independent_execution_resources_never_block_each_other(tmp_path: Path):
    entered_a = asyncio.Event()
    entered_b = asyncio.Event()
    release = asyncio.Event()

    async def worker_a():
        async with leases(tmp_path, [("resource-alpha", 1, False)]):
            entered_a.set()
            await asyncio.wait_for(release.wait(), timeout=2.0)

    async def worker_b():
        async with leases(tmp_path, [("resource-beta", 1, False)]):
            entered_b.set()
            await asyncio.wait_for(release.wait(), timeout=2.0)

    task_a = asyncio.create_task(worker_a())
    task_b = asyncio.create_task(worker_b())
    try:
        await asyncio.wait_for(
            asyncio.gather(entered_a.wait(), entered_b.wait()),
            timeout=2.0,
        )
    finally:
        release.set()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

    # Confirm exclusive lease is cleanly freed after both exit.
    check = FileLease(tmp_path, "resource-alpha:0")
    assert check.try_acquire()
    check.close()
