#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _clip(text: str, limit: int = 120) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def _bar(value: float, *, width: int = 18, scale: float = 40.0) -> str:
    filled = int(round(min(max(value, 0.0), scale) / scale * width))
    return ("█" * filled) + ("░" * (width - filled))


def _level(value: float) -> str:
    if value >= 30:
        return "dominant"
    if value >= 15:
        return "active"
    if value >= 7:
        return "present"
    if value > 0:
        return "residue"
    return "quiet"


def _drive_role(name: str) -> str:
    roles = {
        "SEEKING": "inquiry, tracking, problem-solving",
        "FEAR": "caution, threat checking",
        "RAGE": "boundary pressure, protest",
        "LUST": "attraction, closeness tension",
        "CARE": "warmth, repair, containment",
        "PANIC_GRIEF": "rupture sensitivity, attachment pain",
        "PLAY": "teasing, flexibility, lightness",
    }
    return roles.get(name, "drive pressure")


def _state_read(drive_values: dict[str, float], dominant: list[str]) -> str:
    active = [name for name, value in sorted(drive_values.items(), key=lambda item: item[1], reverse=True) if value >= 7]
    if not active:
        return "No strong drive pressure is active for this cue."
    if active[:3] == ["CARE", "SEEKING", "LUST"] or {"CARE", "SEEKING", "LUST"}.issubset(set(active[:4])):
        return "Warm, contained closeness is active, with inquiry still holding the response steady."
    if active and active[0] == "CARE" and "SEEKING" in active:
        return "Care is leading, but the self-state is still organized around checking and usefulness."
    if active and active[0] == "SEEKING":
        return "The current self-state is evidence-seeking and task-oriented."
    if "FEAR" in active or "RAGE" in active:
        return "The current self-state is shaped by reliability pressure, caution, or boundary repair."
    if "LUST" in active:
        return "Attraction is active, but it is being held as bounded relational tension."
    if dominant:
        return f"{dominant[0]} is leading the transient self-state for this cue."
    return "The current self-state is diffuse."


def _memory_label(annotation: Any) -> str:
    drives = "/".join(annotation.dominant_drives) or "none"
    return f"#{annotation.annotation_id} i{annotation.intensity} {annotation.event_type} {drives}"


class _StoreRef:
    def __init__(self, db_path: Path):
        self.db_path = db_path


def _latest_relationship_key(db_path: Path) -> str | None:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT relationship_key
                FROM emotional_annotations
                WHERE source != 'bootstrap'
                  AND relationship_key IS NOT NULL
                  AND relationship_key != ''
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row[0]) if row and row[0] else None
    except Exception:
        return None


def _counts(db_path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for table in ("emotional_annotations", "relationship_events"):
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                result[table] = int(row[0]) if row else 0
    except Exception:
        pass
    return result


def _recent_annotations(db_path: Path, limit: int = 5) -> list[dict[str, Any]]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, event_type, intensity, dominant_drives_json, summary
                FROM emotional_annotations
                WHERE source != 'bootstrap'
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            drives = json.loads(row["dominant_drives_json"])
            if not isinstance(drives, list):
                drives = []
        except Exception:
            drives = []
        items.append(
            {
                "id": int(row["id"]),
                "event_type": str(row["event_type"]),
                "intensity": int(row["intensity"]),
                "dominant_drives": [str(item) for item in drives],
                "summary": str(row["summary"]),
            }
        )
    return items


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Inspect an Anatta workspace without touching core runtime.")
    parser.add_argument("--workspace", default="workspaces/rika", help="Workspace path relative to project root or absolute path.")
    parser.add_argument("--probe", default="", help="Optional current cue to inspect. Defaults to a neutral status cue.")
    parser.add_argument("--model", default="gpt-5.5", help="Model profile name for prompt composition.")
    parser.add_argument("--full", action="store_true", help="Include config and injection preview.")
    parser.add_argument("--debug", action="store_true", help="Show internal Anatta warning logs while inspecting.")
    args = parser.parse_args()
    if not args.debug:
        logging.getLogger("Anatta").setLevel(logging.CRITICAL)

    root = _project_root()
    workspace = Path(args.workspace)
    if not workspace.is_absolute():
        workspace = root / workspace
    db_path = workspace / "bridge_memory.sqlite"
    if not workspace.exists():
        print(f"Workspace not found: {workspace}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"Bridge memory DB not found: {db_path}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(root))
    from orchestrator.anatta.layer import build_anatta_layer
    from orchestrator.anatta.models import TurnContext

    layer = build_anatta_layer(workspace, _StoreRef(db_path))
    probe = args.probe.strip() or "Anatta status inspection."
    relationship_key = _latest_relationship_key(db_path)
    context = TurnContext(
        user_text=probe,
        source="diagnostic",
        request_id="anatta-diagnostics",
        relationship_key=relationship_key,
        metadata={"summary": probe, "source": "diagnostic"},
    )
    state, injection = await layer.build_turn_state(context, args.model)
    retrieved = layer.memory_store.retrieve_relevant_annotations(context, limit=6)
    counts = _counts(db_path)

    print(f"🌙 Anatta · {workspace.name}")
    print("No fixed soul. Current self = cue + ranked emotional memory.")
    print()
    print("⚙️ Runtime")
    print(
        f"Mode {layer.mode()}  |  "
        f"Inject {'on' if layer.should_inject_prompt() else 'off'}  |  "
        f"Record {'on' if layer.config.should_record_annotations() else 'off'}"
    )
    print(f"Relation: {relationship_key or 'none'}")
    print(f"Cue: {_clip(probe, 180)}")
    print()
    print("🧭 Current Self")
    print(_state_read(state.drive_values, state.dominant_drives))
    print(f"Top drives: {' + '.join(state.dominant_drives) if state.dominant_drives else 'none'}")
    print()
    print("💠 Drive Mix")
    visible_drives = [(name, value) for name, value in sorted(state.drive_values.items(), key=lambda item: item[1], reverse=True) if value > 0]
    for name, value in visible_drives[:6]:
        print(f"{name:<12} {_bar(value)}  {value:>4.1f}  {_level(value)}")
        print(f"  {_drive_role(name)}")
    hidden = [name for name, value in visible_drives[6:] if value > 0]
    if hidden:
        print(f"Other residue: {', '.join(hidden)}")
    print()
    print("🧠 Memory")
    print(
        f"Annotations {counts.get('emotional_annotations', '?')}  |  "
        f"Relations {counts.get('relationship_events', '?')}  |  "
        f"Used now {len(state.contributing_annotation_ids)}"
    )
    print()
    print("🔎 Why This State")
    for index, annotation in enumerate(retrieved[:3], start=1):
        print(f"{index}. {_memory_label(annotation)}")
        print(f"   {_clip(annotation.summary, 150)}")
    if len(retrieved) > 3:
        print(f"+ {len(retrieved) - 3} lower-ranked memories hidden.")
    print()
    print("🕰️ Recent Live Memories")
    for item in _recent_annotations(db_path, limit=4):
        print(
            f"- #{item['id']} i{item['intensity']} | {item['event_type']} | "
            f"{'/'.join(item['dominant_drives'])}: {_clip(item['summary'])}"
        )
    print()
    print("Tip: /anatta full shows raw scores, config, and injection preview.")
    if args.full:
        print()
        print("Raw contributing memory ids:")
        print(", ".join(str(item) for item in state.contributing_annotation_ids[:24]) or "none")
        print()
        print("Full ranked memories:")
        for annotation in retrieved:
            print(
                f"- {_memory_label(annotation)} | score {annotation.metadata.get('_retrieval_score', '?')}: "
                f"{_clip(annotation.summary, 180)}"
            )
        print()
        print("Drive context policy:")
        for name, policy in layer.config.drive_context_policy().items():
            print(
                f"- {name}: off-context x{policy.get('off_context_multiplier')} | "
                f"cues {len(policy.get('cue_terms', []))} | suppressors {len(policy.get('suppression_terms', []))}"
            )
        print()
        print("Visible injection preview:")
        print(injection.body)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
