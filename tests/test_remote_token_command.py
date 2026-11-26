from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import remote_token
from orchestrator.admin_local_testing import (
    execute_local_command,
    try_execute_slash_command_text,
)
from orchestrator.command_interaction_bridge import (
    build_frontend_command_invocation,
    dispatch_command_interaction,
)
from orchestrator.command_registry import load_runtime_commands
from orchestrator.command_specs import COMMAND_SPECS
from orchestrator.commands import remote as remote_command
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.runtime_remote import cmd_remote

TOKEN = "AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf0123"
NEXT = "ZzYyXx9876543210-ZzYyXx9876543210-ZzYyXx9876543210-ZzYyXx9876"


def seed(root: Path, payload: dict) -> Path:
    secrets = root / "secrets.json"
    write_config_json(secrets, payload, private_full_control_sids=())
    return secrets


class FakeRuntime:
    def __init__(self, root: Path, *, authorized: bool = True):
        self.global_config = SimpleNamespace(
            secrets_path=root / "secrets.json", instance_id="HASHI4", project_root=root
        )
        self._authorized = authorized
        self.replies: list[str] = []
        self.enqueued: list[str] = []
        self._remote_process = SimpleNamespace(pid=4321, returncode=None)

    def _is_authorized_user(self, user_id) -> bool:
        return self._authorized

    def _remote_config_snapshot(self) -> dict:
        # Nothing listens here: verification must answer "unavailable" rather
        # than pretend it proved anything.
        return {"root": self.global_config.secrets_path.parent, "port": 1, "use_tls": False}

    async def _reply_text(self, update, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=100 + len(self.replies))

    async def enqueue_request(self, *args, **kwargs):
        self.enqueued.append(str(args[1] if len(args) > 1 else args))
        return None


class IngressRuntime(FakeRuntime):
    """Enough of a runtime to drive the real Telegram ingress paths."""

    name = "kqs"

    def __init__(self, root: Path):
        super().__init__(root)
        self.workspace_dir = root / "workspace"
        self.workspace_dir.mkdir()
        self.global_config.authorized_id = 7
        self.logger = logging.getLogger("test.remote-token-ingress")
        from orchestrator.session_store import SessionStore

        self.session_store = SessionStore(
            root / "state" / "sessions.sqlite3", instance_id="HASHI4"
        )

    def _record_active_chat(self, _update):
        return None

    def _should_redirect_after_transfer(self):
        return False


def update(
    text: str = "",
    *,
    reply_to: int | None = None,
    user_id: int = 7,
    chat_id: int = 11,
    update_id: int = 900,
    on_delete=None,
):
    message = SimpleNamespace(
        text=text,
        message_id=500,
        date=datetime.now(timezone.utc),
        reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to is not None else None,
        delete=on_delete or _async_noop,
    )
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=None,
    )


async def _async_noop(*_args, **_kwargs):
    return None


def args(*values: str) -> SimpleNamespace:
    return SimpleNamespace(args=list(values))


def run(coroutine):
    return asyncio.run(coroutine)


# --- registration and dispatch ------------------------------------------------


def test_remote_is_registered_once_and_the_token_module_does_not_take_it_over():
    specs = [spec for spec in COMMAND_SPECS if spec.name == "remote"]
    assert len(specs) == 1
    assert specs[0].method_name == "cmd_remote"
    assert "token" in specs[0].guide.choices
    # The old duplicate silently replaced every lifecycle subcommand.
    assert not hasattr(remote_command, "COMMANDS")
    assert not [command for command in load_runtime_commands() if command.name == "remote"]


def test_token_is_reached_through_the_existing_remote_handler(tmp_path: Path):
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "status")))
    assert runtime.replies and "HASHI Shared Token" in runtime.replies[0]
    assert runtime.enqueued == []


def test_existing_remote_subcommands_still_dispatch(tmp_path: Path):
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    runtime = FakeRuntime(tmp_path)
    calls: list[str] = []

    async def fetch(path: str):
        calls.append(path)
        return {"peers": []}, None

    runtime._fetch_remote_json = fetch
    run(cmd_remote(runtime, update(), args("list")))
    assert "/peers" in calls
    assert runtime.replies and "HASHI Shared Token" not in runtime.replies[0]


def test_unknown_token_subcommand_shows_usage(tmp_path: Path):
    seed(tmp_path, {})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "banana")))
    assert "Usage" in runtime.replies[0] or "/remote token status" in runtime.replies[0]


# --- credential handling ------------------------------------------------------


@pytest.mark.parametrize(
    ("command_text", "command_args"),
    [
        (f"/remote token set {TOKEN}", ["token", "set", TOKEN]),
        (f"/remote token set={TOKEN}", ["token", f"set={TOKEN}"]),
        (f"/remote token set: {TOKEN}", ["token", "set:", TOKEN]),
    ],
)
@pytest.mark.asyncio
async def test_inline_token_is_rejected_before_audit_history_or_invocation(
    tmp_path: Path, monkeypatch, command_text, command_args
):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.slash_command_audit import default_audit_path

    seed(tmp_path, {})
    runtime = IngressRuntime(tmp_path)
    executed: list[str] = []

    async def handler(_update, _context):
        executed.append("ran")

    async def allow_channel(self, _update, *, source_channel):
        return True

    monkeypatch.setattr(FlexibleAgentRuntime, "_telegram_channel_allowed", allow_channel)
    wrapped = FlexibleAgentRuntime._wrap_cmd(runtime, "remote", handler)
    await wrapped(update(command_text), args(*command_args))

    # The handler is never reached, so nothing can write the credential.
    assert executed == []
    assert TOKEN not in "\n".join(runtime.replies)
    assert "not accepted" in runtime.replies[0].lower() or "history" in runtime.replies[0].lower()
    audit = default_audit_path(runtime.workspace_dir).read_text(encoding="utf-8")
    assert TOKEN not in audit
    assert "[redacted]" in audit
    assert "inline_token_rejected" in audit
    # Audit, session history, delayed-confirmation records and invocation
    # payloads all live under this root; none of them may hold the value.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
    assert runtime.enqueued == []
    assert "hashi_remote_shared_token" not in read_config_json(tmp_path / "secrets.json")


@pytest.mark.asyncio
async def test_pending_reply_is_consumed_before_the_freshness_gate(
    tmp_path: Path, monkeypatch
):
    from orchestrator import telegram_freshness_gate
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.ingress_confirmation import IngressConfirmationService

    seed(tmp_path, {})
    runtime = IngressRuntime(tmp_path)
    await cmd_remote(runtime, update(), args("token", "set"))
    pending = getattr(runtime, remote_command.PENDING_ATTRIBUTE)

    async def allow_channel(self, _update, *, source_channel):
        return True

    monkeypatch.setattr(FlexibleAgentRuntime, "_telegram_channel_allowed", allow_channel)
    gate_calls: list[str] = []

    async def fake_gate(_runtime, _update, **kwargs):
        gate_calls.append(str(kwargs.get("handler_kind")))
        return True

    monkeypatch.setattr(
        telegram_freshness_gate, "check_telegram_message_freshness", fake_gate
    )
    deleted: list[bool] = []

    async def on_delete():
        deleted.append(True)

    await FlexibleAgentRuntime.handle_message(
        runtime,
        update(TOKEN, reply_to=pending["prompt_message_id"], on_delete=on_delete),
        SimpleNamespace(),
    )

    # The gate, which would persist the raw update and its preview, never ran.
    assert gate_calls == []
    assert remote_token.effective_token(tmp_path, env={}) == ("secrets", TOKEN)
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is None
    assert deleted == [True]
    assert runtime.enqueued == []
    assert TOKEN not in "\n".join(runtime.replies)
    records = IngressConfirmationService(runtime.session_store).list_for_connector(
        agent_id=runtime.name, connector_id="telegram"
    )
    assert records == []
    assert (
        TOKEN.encode()
        not in (tmp_path / "state" / "sessions.sqlite3").read_bytes()
    )


@pytest.mark.asyncio
async def test_ordinary_messages_keep_the_freshness_gate_while_a_prompt_is_open(
    tmp_path: Path, monkeypatch
):
    from orchestrator import telegram_freshness_gate
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    runtime = IngressRuntime(tmp_path)
    await cmd_remote(runtime, update(), args("token", "set"))
    pending = dict(getattr(runtime, remote_command.PENDING_ATTRIBUTE))

    async def allow_channel(self, _update, *, source_channel):
        return True

    monkeypatch.setattr(FlexibleAgentRuntime, "_telegram_channel_allowed", allow_channel)
    gate_calls: list[str] = []

    async def fake_gate(_runtime, _update, **kwargs):
        gate_calls.append(str(kwargs.get("handler_kind")))
        return True  # stale/delayed: the ordinary message is intercepted

    monkeypatch.setattr(
        telegram_freshness_gate, "check_telegram_message_freshness", fake_gate
    )
    deleted: list[bool] = []

    async def on_delete():
        deleted.append(True)

    await FlexibleAgentRuntime.handle_message(
        runtime,
        update("how is the build going?", on_delete=on_delete),
        SimpleNamespace(),
    )

    assert gate_calls == ["text"]
    assert deleted == []
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE) == pending
    assert remote_token.effective_token(tmp_path, env={}) == ("secrets", TOKEN)
    assert runtime.enqueued == []


def test_prompt_reply_rotates_the_token_and_never_becomes_a_chat_message(tmp_path: Path):
    seed(tmp_path, {})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "set")))
    pending = getattr(runtime, remote_command.PENDING_ATTRIBUTE)
    assert pending["prompt_message_id"] == 101

    reply = update(TOKEN, reply_to=101)
    handled = run(remote_command.handle_pending_reply(runtime, reply))

    assert handled is True
    # Handled means the caller returns before history, queue and agent dispatch.
    assert runtime.enqueued == []
    assert TOKEN not in "\n".join(runtime.replies)
    assert remote_token.effective_token(tmp_path, env={}) == ("secrets", TOKEN)
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is None
    assert "saved" in runtime.replies[-1].lower()


def test_a_second_prompt_reply_rotates_again(tmp_path: Path):
    seed(tmp_path, {})
    runtime = FakeRuntime(tmp_path)
    for value in (TOKEN, NEXT):
        run(cmd_remote(runtime, update(), args("token", "set")))
        pending = getattr(runtime, remote_command.PENDING_ATTRIBUTE)
        assert run(remote_command.handle_pending_reply(runtime, update(value, reply_to=pending["prompt_message_id"]))) is True
    assert remote_token.effective_token(tmp_path, env={}) == ("secrets", NEXT)
    assert read_config_json(tmp_path / "secrets.json")["hashi_remote_shared_token"] == NEXT


def test_status_and_verify_never_render_the_value(tmp_path: Path):
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "status")))
    run(cmd_remote(runtime, update(), args("token", "verify")))
    transcript = "\n".join(runtime.replies)
    assert TOKEN not in transcript
    assert remote_token.mask_token(TOKEN) in transcript
    # Port 1 answers nothing, so the honest result is "no evidence".
    assert "Could not check" in transcript or "could not" in transcript.lower()


def test_clear_reports_the_result_and_survives_a_restart(tmp_path: Path):
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN, "other": "keep"})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "clear")))

    stored = read_config_json(tmp_path / "secrets.json")
    assert "hashi_remote_shared_token" not in stored
    assert stored["other"] == "keep"
    assert remote_token.load_status(tmp_path, env={}).source == "disabled"

    from orchestrator.workbench_auth import ensure_local_api_credential

    reprovisioned = ensure_local_api_credential(stored, profile="personal")
    assert "hashi_remote_shared_token" not in reprovisioned


# --- cancellation -------------------------------------------------------------


def test_cancel_command_clears_a_pending_prompt(tmp_path: Path):
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "set")))
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is not None

    run(cmd_remote(runtime, update(), args("token", "cancel")))
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is None
    assert remote_token.effective_token(tmp_path, env={}) == ("secrets", TOKEN)


def test_plain_cancel_reply_aborts_without_changing_the_credential(tmp_path: Path):
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "set")))
    pending = getattr(runtime, remote_command.PENDING_ATTRIBUTE)

    handled = run(remote_command.handle_pending_reply(runtime, update("取消", reply_to=pending["prompt_message_id"])))

    assert handled is True
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is None
    assert remote_token.effective_token(tmp_path, env={}) == ("secrets", TOKEN)
    assert "cancel" in runtime.replies[-1].lower()


def test_an_unrelated_message_is_not_eaten_by_an_open_prompt(tmp_path: Path):
    seed(tmp_path, {})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "set")))
    # No reply_to: this is an ordinary chat message, not an answer to the prompt.
    assert run(remote_command.handle_pending_reply(runtime, update("how is the build going?"))) is False
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is not None
    assert remote_token.effective_token(tmp_path, env={})[0] != "secrets"


def test_an_expired_prompt_is_refused(tmp_path: Path):
    seed(tmp_path, {})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "set")))
    pending = getattr(runtime, remote_command.PENDING_ATTRIBUTE)
    pending["expires_at"] = time.time() - 1

    handled = run(remote_command.handle_pending_reply(runtime, update(TOKEN, reply_to=pending["prompt_message_id"])))

    assert handled is True
    assert remote_token.effective_token(tmp_path, env={})[0] != "secrets"
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is None

# --- non-Telegram command admission ------------------------------------------

INLINE_TOKEN_COMMANDS = [
    f"/remote token set {TOKEN}",
    f"/remote token set={TOKEN}",
    f"/remote token set: {TOKEN}",
]


def _assert_token_absent(root: Path) -> None:
    needle = TOKEN.encode("utf-8")
    for path in root.rglob("*"):
        if path.is_file():
            assert needle not in path.read_bytes(), f"token persisted in {path}"


@pytest.mark.parametrize("command_text", INLINE_TOKEN_COMMANDS)
def test_local_command_admission_rejects_inline_token_before_audit(
    tmp_path: Path, command_text: str
):
    seed(tmp_path, {})
    runtime = IngressRuntime(tmp_path)

    result = run(
        execute_local_command(runtime, command_text, source_channel="workbench_api")
    )

    assert result["ok"] is False
    assert result["error_code"] == "inline_token_rejected"
    assert "[redacted]" in result["args"]
    assert TOKEN not in json.dumps(result)
    audit = (runtime.workspace_dir / "slash_command_audit.jsonl").read_text("utf-8")
    assert "inline_token_rejected" in audit
    assert "[redacted]" in audit
    assert TOKEN not in audit
    _assert_token_absent(tmp_path)
    assert remote_token.effective_token(tmp_path, env={})[0] != "secrets"


def test_worker_proxy_text_entry_rejects_before_dispatch(tmp_path: Path):
    seed(tmp_path, {})
    runtime = IngressRuntime(tmp_path)
    runtime.is_function_worker_proxy = True
    sent: list[str] = []

    async def execute_slash_command(text, **kwargs):
        sent.append(text)
        return {"ok": True}

    runtime.execute_slash_command = execute_slash_command

    result = run(
        try_execute_slash_command_text(
            runtime,
            INLINE_TOKEN_COMMANDS[0],
            source_channel="api_chat",
            session_metadata={"owner_id": "user:7"},
        )
    )

    assert result is not None and result["error_code"] == "inline_token_rejected"
    assert sent == []
    assert TOKEN not in json.dumps(result)
    _assert_token_absent(tmp_path)


@pytest.mark.parametrize(
    "arguments",
    [
        ["token", "set", TOKEN],
        ["token", f"set={TOKEN}"],
        ["token", "set:", TOKEN],
    ],
)
def test_typed_invocation_never_contains_the_inline_token(arguments: list[str]):
    invocation = build_frontend_command_invocation(
        {"version": 1, "op": "open", "client_id": "clientabcdefghijkl",
         "request_id": "requestabcdefghijkl", "ui_locale": "en",
         "command": "/remote token set"},
        {"session_id": "sessionabcdefghijkl", "instance_id": "HASHI1",
         "connection_binding": "connectionabcdefghijkl", "context_generation": 1},
        actor=7,
        command_name="remote",
        arguments=arguments,
    )

    assert "[redacted]" in invocation["arguments"]
    assert TOKEN not in json.dumps(invocation)


def test_typed_invocation_keeps_remote_lifecycle_arguments():
    invocation = build_frontend_command_invocation(
        {"version": 1, "op": "open", "client_id": "clientabcdefghijkl",
         "request_id": "requestabcdefghijkl", "ui_locale": "en",
         "command": "/remote token status"},
        {"session_id": "sessionabcdefghijkl", "instance_id": "HASHI1",
         "connection_binding": "connectionabcdefghijkl", "context_generation": 1},
        actor=7,
        command_name="remote",
        arguments=["token", "status"],
    )

    assert invocation["arguments"] == ["token", "status"]


@pytest.mark.parametrize("command_text", INLINE_TOKEN_COMMANDS)
def test_command_menu_open_rejects_before_invocation_persistence(
    tmp_path: Path, command_text: str
):
    seed(tmp_path, {})
    runtime = SimpleNamespace(
        name="agent",
        workspace_dir=tmp_path,
        global_config=SimpleNamespace(authorized_id=7, bridge_home=tmp_path),
        _is_authorized_user=lambda actor: actor == 7,
    )
    payload = {
        "version": 1,
        "op": "open",
        "client_id": "clientabcdefghijkl",
        "request_id": "requestabcdefghijkl",
        "ui_locale": "en",
        "command": command_text,
    }
    metadata = {
        "actor_id": 7,
        "session_id": "sessionabcdefghijkl",
        "connection_binding": "connectionabcdefghijkl",
        "instance_id": "HASHI1",
        "context_generation": 1,
    }

    result = run(dispatch_command_interaction(runtime, payload, metadata))

    assert result["ok"] is False
    assert result["error_code"] == "inline_token_rejected"
    assert TOKEN not in json.dumps(result)
    assert not runtime._command_interaction_store.menus
    _assert_token_absent(tmp_path)