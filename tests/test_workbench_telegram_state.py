from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator.admin_local_testing as alt
from orchestrator.frontend_delivery import (
    telegram_delivery_for_admission,
    tui_request_metadata,
)
from orchestrator.workbench_telegram_state import (
    DEFAULT_MIRROR,
    load_state,
    mirror_enabled,
    parse_mirror_arg,
    set_mirror,
    state_path,
)

STATE_FILE_NAME = "workbench_telegram_state.json"


def _workbench_metadata(owner: str = "owner-a", surface: str = "workbench"):
    return {"session_surface": surface, "owner_id": owner}


# --------------------------------------------------------------------------
# State store
# --------------------------------------------------------------------------

def test_state_path_lives_under_bridge_state(tmp_path):
    assert state_path(tmp_path) == tmp_path / "state" / STATE_FILE_NAME


def test_missing_state_defaults_mirror_on(tmp_path):
    assert DEFAULT_MIRROR is True
    assert mirror_enabled(tmp_path, "owner-a") is True
    assert load_state(tmp_path) == {"revision": 0, "owners": {}}


def test_corrupt_state_fails_open_to_default(tmp_path):
    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert mirror_enabled(tmp_path, "owner-a") is True


def test_set_mirror_roundtrip_and_revision(tmp_path):
    first = set_mirror(tmp_path, "owner-a", False)
    assert mirror_enabled(tmp_path, "owner-a") is False
    assert first["revision"] == 1
    second = set_mirror(tmp_path, "owner-a", True)
    assert mirror_enabled(tmp_path, "owner-a") is True
    assert second["revision"] == 2
    raw = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["owners"]["owner-a"] is True
    assert raw["revision"] == 2


def test_per_owner_isolation(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    assert mirror_enabled(tmp_path, "owner-a") is False
    assert mirror_enabled(tmp_path, "owner-b") is True


def test_set_mirror_leaves_no_temp_files(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    state_dir = state_path(tmp_path).parent
    names = {path.name for path in state_dir.iterdir()}
    assert names <= {STATE_FILE_NAME, STATE_FILE_NAME + ".lock"}
    assert json.loads(state_path(tmp_path).read_text(encoding="utf-8"))["revision"] == 1


def test_parse_mirror_arg():
    assert parse_mirror_arg([]) is None
    assert parse_mirror_arg(["on"]) is True
    assert parse_mirror_arg(["OFF"]) is False
    with pytest.raises(ValueError):
        parse_mirror_arg(["maybe"])
    with pytest.raises(ValueError):
        parse_mirror_arg(["on", "off"])


# --------------------------------------------------------------------------
# Admission resolver
# --------------------------------------------------------------------------

def test_resolver_workbench_state_off(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    assert (
        telegram_delivery_for_admission(
            source="api",
            request_metadata=_workbench_metadata(),
            state_root=tmp_path,
        )
        is False
    )


def test_resolver_workbench_state_on_after_toggle(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    set_mirror(tmp_path, "owner-a", True)
    assert (
        telegram_delivery_for_admission(
            source="api",
            request_metadata=_workbench_metadata(),
            state_root=tmp_path,
        )
        is True
    )


def test_resolver_defaults_on_without_state_file(tmp_path):
    assert (
        telegram_delivery_for_admission(
            source="api",
            request_metadata=_workbench_metadata(),
            state_root=tmp_path,
        )
        is True
    )


def test_resolver_ignores_non_workbench_surface(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    assert (
        telegram_delivery_for_admission(
            source="api",
            request_metadata=_workbench_metadata(surface="voice"),
            state_root=tmp_path,
        )
        is True
    )


def test_resolver_ignores_missing_owner(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    assert (
        telegram_delivery_for_admission(
            source="api",
            request_metadata={"session_surface": "workbench"},
            state_root=tmp_path,
        )
        is True
    )


def test_resolver_without_state_root_keeps_legacy_default(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    assert (
        telegram_delivery_for_admission(
            source="api",
            request_metadata=_workbench_metadata(),
        )
        is True
    )


def test_resolver_other_sources_unchanged(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    sources = (
        "telegram.command",
        "telegram.reply",
        "telegram.send",
        "scheduler",
        "hchat",
    )
    for source in sources:
        assert (
            telegram_delivery_for_admission(
                source=source,
                request_metadata={"owner_id": "owner-a"},
                state_root=tmp_path,
            )
            is True
        )
        assert (
            telegram_delivery_for_admission(
                source=source,
                request_metadata=None,
                state_root=tmp_path,
            )
            is True
        )


def test_resolver_tui_policy_still_authoritative(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    assert (
        telegram_delivery_for_admission(
            source="tui",
            request_metadata={
                **_workbench_metadata(),
                **tui_request_metadata(telegram_mirror=True, client_id="tui-a"),
            },
            state_root=tmp_path,
        )
        is True
    )
    assert (
        telegram_delivery_for_admission(
            source="tui",
            request_metadata={
                **_workbench_metadata(),
                **tui_request_metadata(telegram_mirror=False, client_id="tui-a"),
            },
            state_root=tmp_path,
        )
        is False
    )


def test_resolver_missing_metadata_keeps_default(tmp_path):
    set_mirror(tmp_path, "owner-a", False)
    assert (
        telegram_delivery_for_admission(
            source="api",
            request_metadata=None,
            state_root=tmp_path,
        )
        is True
    )


# --------------------------------------------------------------------------
# api_chat /telegram command
# --------------------------------------------------------------------------

def _fake_runtime(tmp_path: Path, *, allowed: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        name="lily",
        is_function_worker_proxy=False,
        global_config=SimpleNamespace(
            bridge_home=str(tmp_path),
            authorized_id=12345,
        ),
        _is_command_allowed=(lambda _cmd: True) if allowed else (lambda _cmd: False),
    )


@pytest.fixture
def no_transport_dispatch(monkeypatch):
    async def _no_dispatch(*args, **kwargs):
        return None

    monkeypatch.setattr(
        alt, "try_dispatch_voice_confirmation_transport", _no_dispatch
    )
    monkeypatch.setattr(alt, "try_dispatch_chat_projection_transport", _no_dispatch)
    monkeypatch.setattr(alt, "try_dispatch_command_interaction_transport", _no_dispatch)


def _run_command(runtime, text: str, *, owner: str = "owner-a"):
    return asyncio.run(
        alt.try_execute_slash_command_text(
            runtime,
            text,
            source_channel="api_chat",
            session_metadata={"owner_id": owner},
        )
    )


def test_command_status_defaults_on(tmp_path, no_transport_dispatch):
    result = _run_command(_fake_runtime(tmp_path), "/telegram")
    assert result is not None
    assert result["ok"] is True
    assert result["telegram_mirror"] is True
    assert result["owner_id"] == "owner-a"
    assert result["revision"] == 0


def test_command_off_persists_and_reads_back(tmp_path, no_transport_dispatch):
    runtime = _fake_runtime(tmp_path)
    off = _run_command(runtime, "/telegram off")
    assert off["ok"] is True
    assert off["telegram_mirror"] is False
    assert off["revision"] == 1
    status = _run_command(runtime, "/telegram")
    assert status["telegram_mirror"] is False
    on = _run_command(runtime, "/telegram on")
    assert on["ok"] is True
    assert on["telegram_mirror"] is True
    assert on["revision"] == 2
    assert mirror_enabled(tmp_path, "owner-a") is True


def test_command_rejects_invalid_argument(tmp_path, no_transport_dispatch):
    result = _run_command(_fake_runtime(tmp_path), "/telegram maybe")
    assert result["ok"] is False
    assert "on|off" in str(result["error"])
    assert result.get("usage") == "/telegram on|off"


def test_command_disabled_is_blocked_and_audited(tmp_path, no_transport_dispatch):
    runtime = _fake_runtime(tmp_path, allowed=False)
    result = _run_command(runtime, "/telegram off")
    assert result["ok"] is False
    assert "disabled" in str(result["error"])
    audit = tmp_path / "workspaces" / "lily" / "slash_command_audit.jsonl"
    assert audit.is_file()
    lines = [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        line.get("command_name") == "telegram" and line.get("status") == "blocked"
        for line in lines
    )


def test_command_writes_audit_record(tmp_path, no_transport_dispatch):
    _run_command(_fake_runtime(tmp_path), "/telegram off")
    audit = tmp_path / "workspaces" / "lily" / "slash_command_audit.jsonl"
    lines = [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines
    assert lines[-1]["command_name"] == "telegram"
    assert lines[-1]["status"] == "success"
    assert lines[-1]["source_channel"] == "api_chat"


def test_command_ignored_for_other_channels(tmp_path, no_transport_dispatch):
    runtime = _fake_runtime(tmp_path)
    result = asyncio.run(
        alt.try_execute_slash_command_text(
            runtime,
            "/telegram off",
            source_channel="telegram",
            session_metadata={"owner_id": "owner-a"},
        )
    )
    assert result is None
