from __future__ import annotations

import json
from pathlib import Path

LOCALE = Path(__file__).resolve().parents[1] / "locales" / "runtime" / "zh-CN.json"

# Only old branding and private names are absolute bans. Contextual terms such
# as 槽位/权威/回执 remain legal where they are the accurate user-facing word.
BANNED = ("小能", "Aptenra", "爸爸", "Lily", "Arale", "阿拉蕾", "zhaojun")


def test_chinese_runtime_copy_has_no_old_branding_or_private_names():
    data = json.loads(LOCALE.read_text(encoding="utf-8"))
    values = list(data["strings"].items())
    values += [(f"commands.{key}", value) for key, value in data["commands"].items()]
    hits = [
        (key, term)
        for key, value in values
        if isinstance(value, str)
        for term in BANNED
        if term in value
    ]
    assert not hits, f"{len(hits)} branding/name hits: {hits[:8]}"
