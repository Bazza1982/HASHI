#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _count_table(db_path: Path, table: str) -> int | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except Exception:
        return None
    return int(row[0]) if row else 0


def _observer_summary(config: dict[str, Any]) -> tuple[int, int]:
    raw = config.get("observers", [])
    if not isinstance(raw, list):
        return 0, 0
    total = 0
    enabled = 0
    for item in raw:
        if isinstance(item, str):
            total += 1
            enabled += 1
        elif isinstance(item, dict):
            total += 1
            if item.get("enabled", True):
                enabled += 1
    return total, enabled


def build_report(workspace: Path, *, full: bool = False) -> str:
    workspace = workspace.expanduser().resolve()
    anatta_config_path = workspace / "anatta_config.json"
    observer_config_path = workspace / "post_turn_observers.json"
    db_path = workspace / "bridge_memory.sqlite"

    anatta_config = _load_json(anatta_config_path)
    observer_config = _load_json(observer_config_path)
    observer_total, observer_enabled = _observer_summary(observer_config)
    mode = str(anatta_config.get("mode", "off")).strip() or "off"

    annotation_count = _count_table(db_path, "emotional_annotations")
    relationship_count = _count_table(db_path, "relationship_events")

    lines = [
        f"Anatta diagnostics: {workspace.name}",
        "",
        "Runtime",
        f"- mode: {mode}",
        f"- anatta_config.json: {'present' if anatta_config_path.exists() else 'absent'}",
        f"- post_turn_observers.json: {'present' if observer_config_path.exists() else 'absent'}",
        f"- observers: {observer_enabled}/{observer_total} enabled",
        "",
        "Storage",
        f"- bridge_memory.sqlite: {'present' if db_path.exists() else 'absent'}",
        f"- emotional_annotations: {_format_count(annotation_count)}",
        f"- relationship_events: {_format_count(relationship_count)}",
        "",
        "Safety",
        "- command mode: read-only",
        "- memory contents: not printed",
        "- config writes: disabled",
    ]
    if full:
        observer_factories = _observer_factories(observer_config)
        lines.extend(
            [
                "",
                "Config detail",
                f"- anatta keys: {', '.join(sorted(anatta_config.keys())) or 'none'}",
                f"- observer factories: {', '.join(observer_factories) or 'none'}",
            ]
        )
    return "\n".join(lines)


def _format_count(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _observer_factories(config: dict[str, Any]) -> list[str]:
    raw = config.get("observers", [])
    if not isinstance(raw, list):
        return []
    factories: list[str] = []
    for item in raw:
        if isinstance(item, str):
            factories.append(item)
        elif isinstance(item, dict) and item.get("factory"):
            factories.append(str(item["factory"]))
    return factories


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Anatta workspace diagnostics.")
    parser.add_argument("--workspace", required=True, help="Workspace directory to inspect.")
    parser.add_argument("--full", action="store_true", help="Include config keys and observer factories.")
    args = parser.parse_args()

    print(build_report(Path(args.workspace), full=args.full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
