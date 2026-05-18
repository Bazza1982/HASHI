from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


HASHI_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = HASHI_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import reset_dual_brain_notepads as reset


def test_reset_notepads_archives_and_clears_all_agents(tmp_path: Path) -> None:
    root = tmp_path / "hashi"
    lily_memory = root / "workspaces" / "lily" / "memory"
    sakura_memory = root / "workspaces" / "sakura" / "memory"
    lily_memory.mkdir(parents=True)
    sakura_memory.mkdir(parents=True)
    lily_notepad = lily_memory / "left_brain_continuity.jsonl"
    sakura_notepad = sakura_memory / "left_brain_continuity.jsonl"
    lily_notepad.write_text('{"note":"one"}\n{"note":"two"}\n', encoding="utf-8")
    sakura_notepad.write_text("", encoding="utf-8")

    results = reset.reset_notepads(
        [root],
        trigger="test-wiki",
        publish_id="publish-1",
        now=datetime(2026, 5, 16, 4, 30, tzinfo=ZoneInfo("Australia/Sydney")),
    )

    statuses = {(result.agent, result.status) for result in results}
    assert statuses == {("lily", "cleared"), ("sakura", "skipped")}
    assert lily_notepad.read_text(encoding="utf-8") == ""

    archive_dir = lily_memory / "left_brain_archives" / "2026-05-16"
    archives = list(archive_dir.glob("left_brain_continuity_*.jsonl"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == '{"note":"one"}\n{"note":"two"}\n'

    manifest = json.loads(archives[0].with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["agent"] == "lily"
    assert manifest["trigger"] == "test-wiki"
    assert manifest["publish_id"] == "publish-1"
    assert manifest["lines"] == 2


def test_reset_notepads_dry_run_does_not_clear(tmp_path: Path) -> None:
    root = tmp_path / "hashi"
    memory = root / "workspaces" / "lily" / "memory"
    memory.mkdir(parents=True)
    notepad = memory / "left_brain_continuity.jsonl"
    notepad.write_text('{"note":"keep"}\n', encoding="utf-8")

    results = reset.reset_notepads(
        [root],
        dry_run=True,
        trigger="test",
        now=datetime(2026, 5, 16, 4, 30, tzinfo=ZoneInfo("Australia/Sydney")),
    )

    assert [(result.agent, result.status) for result in results] == [("lily", "dry-run")]
    assert notepad.read_text(encoding="utf-8") == '{"note":"keep"}\n'
    assert not (memory / "left_brain_archives").exists()


def test_main_writes_jsonl_audit_log(tmp_path: Path) -> None:
    root = tmp_path / "hashi"
    memory = root / "workspaces" / "lily" / "memory"
    memory.mkdir(parents=True)
    notepad = memory / "left_brain_continuity.jsonl"
    notepad.write_text('{"note":"archive"}\n', encoding="utf-8")
    log_path = tmp_path / "logs" / "reset.jsonl"

    exit_code = reset.main(
        [
            "--root",
            str(root),
            "--trigger",
            "test-main",
            "--publish-id",
            "publish-2",
            "--log-file",
            str(log_path),
        ]
    )

    assert exit_code == 0
    event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["success"] is True
    assert event["trigger"] == "test-main"
    assert event["publish_id"] == "publish-2"
    assert event["cleared"] == 1
    assert event["checked"] == 1
    assert event["results"][0]["agent"] == "lily"
    assert event["results"][0]["status"] == "cleared"
