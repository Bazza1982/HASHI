from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_mode, ui_language
from orchestrator.config import (
    DEFAULT_AGENT_MODE,
    RETIRED_AGENT_MODES,
    SESSION_MODE_BACKENDS,
    SUPPORTED_AGENT_MODES,
    default_agent_mode_for_backend,
)


class _Backend:
    def __init__(self, *, supports_sessions: bool) -> None:
        self.capabilities = SimpleNamespace(supports_sessions=supports_sessions)
        self.session_mode_calls: list[bool] = []

    def set_session_mode(self, enabled: bool) -> None:
        self.session_mode_calls.append(enabled)


class _Manager:
    def __init__(self, mode: str, backend: _Backend) -> None:
        self.agent_mode = mode
        self.current_backend = backend
        self.save_calls = 0

    def _save_state(self) -> None:
        self.save_calls += 1


class _Runtime:
    def __init__(
        self,
        tmp_path,
        *,
        mode: str,
        engine: str,
        supports_sessions: bool,
    ) -> None:
        self.workspace_dir = tmp_path
        self.config = SimpleNamespace(active_backend=engine)
        self.backend_manager = _Manager(
            mode,
            _Backend(supports_sessions=supports_sessions),
        )
        self.replies: list[tuple[str, dict]] = []

    @staticmethod
    def _is_authorized_user(user_id: int) -> bool:
        return user_id == 7

    async def _reply_text(self, _update, text: str, **kwargs) -> None:
        self.replies.append((text, kwargs))


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=7))


class _Query:
    def __init__(self) -> None:
        self.edits: list[tuple[str, dict]] = []
        self.answers: list[tuple[str | None, dict]] = []

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append((text, kwargs))


def test_working_mode_product_surface_is_exact() -> None:
    assert DEFAULT_AGENT_MODE == "fixed"
    assert SUPPORTED_AGENT_MODES == frozenset({"fixed", "flex"})
    assert RETIRED_AGENT_MODES == frozenset({"wrapper", "audit", "dual-brain"})
    assert SESSION_MODE_BACKENDS == frozenset(
        {"claude-cli", "codex-cli", "grok-cli", "her-v2"}
    )

    keyboard = runtime_mode.mode_keyboard("fixed")
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert callbacks == ["tgl:mode:fixed", "tgl:mode:flex"]


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("claude-cli", "fixed"),
        ("codex-cli", "fixed"),
        ("grok-cli", "fixed"),
        ("her-v2", "fixed"),
        ("her", "fixed"),
        ("gemini-cli", "flex"),
        ("ollama", "flex"),
        ("xai-api", "flex"),
        (None, "flex"),
    ],
)
def test_default_working_mode_follows_backend_capability_class(
    engine,
    expected,
) -> None:
    assert default_agent_mode_for_backend(engine) == expected


@pytest.mark.asyncio
async def test_typed_mode_transitions_persist_and_update_session_behavior(
    tmp_path,
) -> None:
    runtime = _Runtime(
        tmp_path,
        mode="flex",
        engine="codex-cli",
        supports_sessions=True,
    )

    await runtime_mode.cmd_mode(
        runtime,
        _update(),
        SimpleNamespace(args=["fixed"]),
    )
    assert runtime.backend_manager.agent_mode == "fixed"
    assert runtime.backend_manager.save_calls == 1
    assert runtime.backend_manager.current_backend.session_mode_calls == [True]

    await runtime_mode.cmd_mode(
        runtime,
        _update(),
        SimpleNamespace(args=["flex"]),
    )
    assert runtime.backend_manager.agent_mode == "flex"
    assert runtime.backend_manager.save_calls == 2
    assert runtime.backend_manager.current_backend.session_mode_calls == [True, False]


@pytest.mark.asyncio
async def test_typed_fixed_rejects_stateless_backend_without_mutation(tmp_path) -> None:
    runtime = _Runtime(
        tmp_path,
        mode="flex",
        engine="gemini-cli",
        supports_sessions=False,
    )

    await runtime_mode.cmd_mode(
        runtime,
        _update(),
        SimpleNamespace(args=["fixed"]),
    )

    assert runtime.backend_manager.agent_mode == "flex"
    assert runtime.backend_manager.save_calls == 0
    assert runtime.backend_manager.current_backend.session_mode_calls == []
    assert runtime.replies[-1][0] == ui_language.tr(
        "mode.fixed.requires_session",
        backend="gemini-cli",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retired_mode",
    ["wrapper", "audit", "dual-brain", "dualbrain", "brain"],
)
async def test_typed_retired_modes_are_non_mutating_notices(
    tmp_path,
    retired_mode,
) -> None:
    runtime = _Runtime(
        tmp_path,
        mode="fixed",
        engine="codex-cli",
        supports_sessions=True,
    )

    await runtime_mode.cmd_mode(
        runtime,
        _update(),
        SimpleNamespace(args=[retired_mode]),
    )

    assert runtime.backend_manager.agent_mode == "fixed"
    assert runtime.backend_manager.save_calls == 0
    assert runtime.backend_manager.current_backend.session_mode_calls == []
    assert runtime.replies[-1][0] == ui_language.tr("mode.retired")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "target", "expected_session_mode"),
    [("flex", "fixed", True), ("fixed", "flex", False)],
)
async def test_callback_mode_transitions_share_typed_command_contract(
    tmp_path,
    start,
    target,
    expected_session_mode,
) -> None:
    runtime = _Runtime(
        tmp_path,
        mode=start,
        engine="codex-cli",
        supports_sessions=True,
    )
    query = _Query()

    await runtime_mode.callback_mode_toggle(runtime, query, target)

    assert runtime.backend_manager.agent_mode == target
    assert runtime.backend_manager.save_calls == 1
    assert runtime.backend_manager.current_backend.session_mode_calls == [
        expected_session_mode
    ]
    assert len(query.edits) == 1
    assert query.answers[-1][1].get("show_alert") is not True


@pytest.mark.asyncio
async def test_callback_fixed_rejects_stateless_backend_without_mutation(
    tmp_path,
) -> None:
    runtime = _Runtime(
        tmp_path,
        mode="flex",
        engine="gemini-cli",
        supports_sessions=False,
    )
    query = _Query()

    await runtime_mode.callback_mode_toggle(runtime, query, "fixed")

    assert runtime.backend_manager.agent_mode == "flex"
    assert runtime.backend_manager.save_calls == 0
    assert runtime.backend_manager.current_backend.session_mode_calls == []
    assert query.edits == []
    assert query.answers[-1][1] == {"show_alert": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("retired_mode", ["wrapper", "audit", "dual-brain"])
async def test_retired_mode_callbacks_are_non_mutating_alerts(
    tmp_path,
    retired_mode,
) -> None:
    runtime = _Runtime(
        tmp_path,
        mode="fixed",
        engine="codex-cli",
        supports_sessions=True,
    )
    query = _Query()

    await runtime_mode.callback_mode_toggle(runtime, query, retired_mode)

    assert runtime.backend_manager.agent_mode == "fixed"
    assert runtime.backend_manager.save_calls == 0
    assert query.edits == []
    assert query.answers == [
        (ui_language.tr("mode.retired"), {"show_alert": True})
    ]
