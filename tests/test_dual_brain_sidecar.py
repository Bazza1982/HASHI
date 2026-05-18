from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock


HASHI_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = HASHI_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import dual_brain_context as context
import dual_brain_common as common
import run_dual_brain_turn as turn


def test_resolve_backend_reads_requested_agent_state(tmp_path: Path) -> None:
    root = tmp_path
    (root / "workspaces" / "akane").mkdir(parents=True)
    (root / "workspaces" / "akane" / "state.json").write_text(
        json.dumps({"active_backend": "codex-cli", "active_model": "akane-model"}),
        encoding="utf-8",
    )
    (root / "agents.json").write_text(
        json.dumps({"global": {"codex_cmd": "/bin/echo"}}),
        encoding="utf-8",
    )

    cfg = {
        "allowed_cli_backends": ["codex-cli"],
        "continuity_file": "memory/left_brain_continuity.jsonl",
        "output_dir": "memory/left_brain_artifacts",
        "wiki_root": "wiki",
    }
    resolved = common.resolve_backend(root, "akane", cfg, role="left_brain")

    assert resolved.backend == "codex-cli"
    assert resolved.model == "akane-model"
    assert Path(resolved.source).parts[-3:] == ("workspaces", "akane", "state.json")


def test_extract_json_object_uses_first_balanced_object() -> None:
    parsed = context._extract_json_object('prefix {"key": {"nested": true}} suffix {"other": 1}')

    assert parsed == {"key": {"nested": True}}


def test_read_bool_treats_string_false_as_false() -> None:
    assert context._read_bool({"should_write": "false"}, "should_write", True) is False
    assert context._read_bool({"should_write": "yes"}, "should_write", False) is True


def test_wiki_candidates_from_multiple_roots(tmp_path: Path) -> None:
    first = tmp_path / "10_GENERATED_TOPICS"
    second = tmp_path / "30_GENERATED_INDEXES"
    first.mkdir()
    second.mkdir()
    (first / "a.md").write_text("topic", encoding="utf-8")
    (second / "b.md").write_text("index", encoding="utf-8")

    candidates = context._wiki_candidates_from_roots([first, second], 10)
    paths = {Path(item["path"]).name for item in candidates}

    assert paths == {"a.md", "b.md"}


def test_stale_lock_is_removed_and_recreated(tmp_path: Path) -> None:
    lock_path = tmp_path / ".turn_active.lock"
    lock_path.write_text(
        json.dumps({"pid": 999999999, "started_at": "old"}),
        encoding="utf-8",
    )
    payload = {"pid": os.getpid(), "started_at": "now"}

    turn._create_turn_lock(lock_path, payload)

    assert json.loads(lock_path.read_text(encoding="utf-8")) == payload


def test_permission_denied_pid_is_treated_as_alive() -> None:
    with mock.patch("run_dual_brain_turn.os.kill", side_effect=PermissionError):
        assert turn._pid_is_alive(12345) is True


def test_add_text_arg_uses_file_for_long_text(tmp_path: Path) -> None:
    argv: list[str] = []
    temp_file = turn._add_text_arg(
        argv,
        flag="--prompt",
        file_flag="--prompt-file",
        text="x" * 20,
        temp_dir=tmp_path,
        inline_max_chars=5,
    )

    assert temp_file is not None
    assert argv == ["--prompt-file", str(temp_file)]
    assert temp_file.read_text(encoding="utf-8") == "x" * 20
