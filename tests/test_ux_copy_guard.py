from __future__ import annotations

import json
from pathlib import Path

LOCALE = Path(__file__).resolve().parents[1] / "locales" / "runtime" / "zh-CN.json"

# User-facing jargon that the P1 glossary bans. Add a term only when the
# replacement is safe everywhere in the runtime copy.
BANNED = (
    "采用", "回执", "投影", "运行代", "努力档位", "母机", "画布卫生", "滞回控制",
    "跨界通话", "智能工", "本地主权", "后果性操作", "安全失败", "启用中的指针",
    "交办", "开始办理", "系统没有重试", "槽位", "权威", "代际", "原子", "幂等", "副作用",
)


def test_chinese_runtime_copy_has_no_banned_jargon():
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
    assert not hits, f"{len(hits)} jargon hits: {hits[:8]}"
