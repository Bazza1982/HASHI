from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import remote_token
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


def update(text: str = "", *, reply_to: int | None = None, user_id: int = 7, chat_id: int = 11):
    message = SimpleNamespace(
        text=text,
        message_id=500,
        reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to is not None else None,
        delete=_async_noop,
    )
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
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


def test_inline_token_is_refused_so_it_never_enters_command_history(tmp_path: Path):
    seed(tmp_path, {})
    runtime = FakeRuntime(tmp_path)
    run(cmd_remote(runtime, update(), args("token", "set", TOKEN)))
    assert TOKEN not in "\n".join(runtime.replies)
    assert "not accepted" in runtime.replies[0].lower() or "history" in runtime.replies[0].lower()
    assert getattr(runtime, remote_command.PENDING_ATTRIBUTE, None) is None
    assert "hashi_remote_shared_token" not in read_config_json(tmp_path / "secrets.json")


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
