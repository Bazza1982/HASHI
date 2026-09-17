from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.gemini_cli import GeminiCLIAdapter
from adapters.registry import get_backend_class, resolve_backend_class
from onboarding import connection
from orchestrator.backend_preflight import BackendPreflight
from orchestrator.api_gateway_preflight import check_gateway_engine
from orchestrator.browser_mode import CLI_NATIVE_BROWSER_BACKENDS
from orchestrator.config_admin import ConfigAdmin
from orchestrator.flexible_backend_registry import (
    get_available_models,
    get_gateway_models,
    is_selectable_backend,
    normalize_allowed_backends,
)
from orchestrator.runtime_model_selection import backend_keyboard
from tui import onboarding


def test_gemini_cli_is_retired_without_erasing_legacy_configuration():
    legacy = normalize_allowed_backends(
        [{"engine": "gemini-cli", "model": "gemini-2.5-flash"}]
    )

    assert legacy == [
        {"engine": "gemini-cli", "model": "gemini-2.5-flash"}
    ]
    assert get_available_models("gemini-cli")
    assert get_gateway_models("gemini-cli") == []
    assert is_selectable_backend("gemini-cli") is False


def test_gemini_cli_adapter_execution_fails_with_retirement_message():
    assert resolve_backend_class("gemini-cli") is GeminiCLIAdapter
    with pytest.raises(RuntimeError, match="(?i)retired"):
        get_backend_class("gemini-cli")


def test_gateway_preflight_reports_gemini_retired_before_path_probe(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.api_gateway_preflight.shutil.which",
        lambda _command: pytest.fail("retired backend must not probe the executable"),
    )

    status = check_gateway_engine(
        SimpleNamespace(gemini_cmd="gemini"),
        {},
        "gemini-cli",
    )

    assert status == {
        "available": False,
        "reason": "retired backend; choose another configured backend",
    }


def test_antigravity_remains_selectable_and_its_models_are_unchanged():
    assert is_selectable_backend("antigravity-cli") is True
    assert "gemini-3.8-flash-high" in get_available_models("antigravity-cli")
    assert get_gateway_models("antigravity-cli")


def test_retired_gemini_has_no_native_browser_route():
    assert "gemini-cli" not in CLI_NATIVE_BROWSER_BACKENDS
    assert {"codex-cli", "claude-cli"} <= CLI_NATIVE_BROWSER_BACKENDS


def test_retired_gemini_active_backend_is_skipped_without_fallback(monkeypatch):
    preflight = BackendPreflight()
    global_cfg = SimpleNamespace(
        gemini_cmd="gemini",
        claude_cmd="claude",
        codex_cmd="codex",
        agy_cmd="agy",
        agy_launch_mode="direct",
        grok_cmd="grok",
    )
    cfg = SimpleNamespace(
        name="legacy",
        active_backend="gemini-cli",
        allowed_backends=[
            {"engine": "gemini-cli"},
            {"engine": "codex-cli"},
        ],
    )
    monkeypatch.setattr(
        "orchestrator.backend_preflight.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )

    status = preflight.check_backend_availability(global_cfg, [cfg], {})
    startable, skipped = preflight.partition_agents_by_availability([cfg], status)

    assert status["gemini-cli"][0] is False
    assert "retired" in status["gemini-cli"][1].lower()
    assert startable == []
    assert skipped == [("legacy", "gemini-cli: retired backend; choose another configured backend")]
    assert cfg.active_backend == "gemini-cli"


def test_backend_picker_hides_gemini_but_keeps_antigravity():
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            active_backend="codex-cli",
            allowed_backends=[
                {"engine": "gemini-cli"},
                {"engine": "antigravity-cli"},
                {"engine": "codex-cli"},
            ],
        )
    )

    callbacks = [
        button.callback_data
        for row in backend_keyboard(runtime).inline_keyboard
        for button in row
    ]

    assert all("gemini-cli" not in callback for callback in callbacks)
    assert any("antigravity-cli" in callback for callback in callbacks)


def test_local_connection_hides_and_rejects_gemini(tmp_path, monkeypatch):
    monkeypatch.setattr(
        connection,
        "packaged_backend_engines",
        lambda: frozenset({"gemini-cli", "codex-cli"}),
    )
    monkeypatch.setattr(
        connection.shutil,
        "which",
        lambda command: f"C:\\tools\\{command}.exe",
    )

    assert [row["engine"] for row in connection.choices(tmp_path)] == ["codex-cli"]
    with pytest.raises(connection.ConnectionError, match="BACKEND_RETIRED"):
        connection.backend_configuration("gemini-cli", "gemini-2.5-flash")


def test_first_run_audit_does_not_offer_gemini(monkeypatch):
    monkeypatch.setattr(
        onboarding.shutil,
        "which",
        lambda command: "gemini" if command == "gemini" else None,
    )
    monkeypatch.setattr(
        onboarding.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert onboarding.audit_environment() == (None, None)


def test_first_run_write_rejects_gemini_before_side_effects(tmp_path):
    with pytest.raises(ValueError, match="(?i)retired"):
        onboarding.write_config(tmp_path, "gemini-cli", {}, "en")

    assert not list(tmp_path.iterdir())


def test_published_samples_do_not_advertise_gemini():
    root = Path(__file__).resolve().parents[1]
    for relative in ("agents.json.sample", "examples/agents.json.samples"):
        path = root / relative
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        assert "gemini-cli" not in json.dumps(document)


def test_agent_creation_rejects_gemini_before_workspace_side_effect(tmp_path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        '{"global":{},"agents":[{"name":"anchor","is_active":true,'
        '"active_backend":"codex-cli","allowed_backends":'
        '[{"engine":"codex-cli"}]}]}',
        encoding="utf-8",
    )
    admin = ConfigAdmin(
        SimpleNamespace(
            config_path=config_path,
            workspaces_root=tmp_path / "workspaces",
        )
    )
    before = config_path.read_bytes()

    assert admin.add_agent_to_config(
        "legacy-new",
        {
            "active_backend": "gemini-cli",
            "allowed_backends": [{"engine": "gemini-cli"}],
        },
    ) is False
    assert config_path.read_bytes() == before
    assert not (tmp_path / "workspaces" / "legacy-new").exists()
