from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import remote_token
from orchestrator.commands import remote as remote_command_module
from orchestrator.config_json import read_config_json, write_config_json

TOKEN = "AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf0123"


class FakeRuntime:
    def __init__(self, root: Path, authorized: bool = True) -> None:
        self.global_config = SimpleNamespace(secrets_path=str(root / "secrets.json"))
        self.authorized = authorized
        self.replies: list[str] = []
        self.next_message_id = 900

    def _is_authorized_user(self, _user_id) -> bool:
        return self.authorized

    async def _reply_text(self, _update, text, **_kwargs):
        self.replies.append(text)
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)


def update(user_id: int = 7, chat_id: int = 11, text: str = "", reply_to: int | None = None):
    message = SimpleNamespace(
        text=text,
        reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to is not None else None,
        delete=_delete,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
    )


async def _delete():
    return None


@pytest.fixture(autouse=True)
def no_real_remote(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        remote_command_module.remote_token,
        "fetch_remote_credential",
        lambda _port, **_kwargs: {"reachable": False, "reason": "unreachable"},
    )


def test_status_reports_a_mask_and_an_unreachable_remote(tmp_path: Path) -> None:
    write_config_json(tmp_path / "secrets.json", {"hashi_remote_shared_token": TOKEN}, private_full_control_sids=())
    runtime = FakeRuntime(tmp_path)
    import asyncio

    asyncio.run(remote_command_module.remote_command(runtime, update(), SimpleNamespace(args=["status"])))

    reply = runtime.replies[-1]
    assert "••••" + TOKEN[-4:] in reply
    assert TOKEN not in reply
    assert "unavailable" in reply


def test_unauthorized_users_are_rejected(tmp_path: Path) -> None:
    write_config_json(tmp_path / "secrets.json", {}, private_full_control_sids=())
    runtime = FakeRuntime(tmp_path, authorized=False)
    import asyncio

    asyncio.run(remote_command_module.remote_command(runtime, update(), SimpleNamespace(args=["status"])))
    assert "owner" in runtime.replies[-1]


def test_set_without_a_value_arms_a_reply_prompt(tmp_path: Path) -> None:
    write_config_json(tmp_path / "secrets.json", {}, private_full_control_sids=())
    runtime = FakeRuntime(tmp_path)
    import asyncio

    asyncio.run(remote_command_module.remote_command(runtime, update(), SimpleNamespace(args=["set"])))
    pending = getattr(runtime, remote_command_module.PENDING_ATTRIBUTE)
    assert pending["prompt_message_id"] == runtime.next_message_id
    assert pending["expires_at"] > time.time()


def test_the_reply_prompt_consumes_the_token_once(tmp_path: Path) -> None:
    write_config_json(tmp_path / "secrets.json", {}, private_full_control_sids=())
    runtime = FakeRuntime(tmp_path)
    import asyncio

    setattr(runtime, remote_command_module.PENDING_ATTRIBUTE, {
        "prompt_message_id": 42, "user_id": 7, "chat_id": 11, "expires_at": time.time() + 60,
    })
    handled = asyncio.run(remote_command_module.handle_pending_reply(runtime, update(text=TOKEN, reply_to=42)))

    assert handled is True
    assert read_config_json(tmp_path / "secrets.json")["hashi_remote_shared_token"] == TOKEN
    assert not hasattr(runtime, remote_command_module.PENDING_ATTRIBUTE)
    assert TOKEN not in "\n".join(runtime.replies)


def test_an_expired_prompt_does_not_write(tmp_path: Path) -> None:
    write_config_json(tmp_path / "secrets.json", {}, private_full_control_sids=())
    runtime = FakeRuntime(tmp_path)
    import asyncio

    setattr(runtime, remote_command_module.PENDING_ATTRIBUTE, {
        "prompt_message_id": 42, "user_id": 7, "chat_id": 11, "expires_at": time.time() - 1,
    })
    handled = asyncio.run(remote_command_module.handle_pending_reply(runtime, update(text=TOKEN, reply_to=42)))

    assert handled is True
    assert "hashi_remote_shared_token" not in read_config_json(tmp_path / "secrets.json")
    assert "expired" in runtime.replies[-1]


def test_unrelated_replies_are_left_for_the_agent(tmp_path: Path) -> None:
    write_config_json(tmp_path / "secrets.json", {}, private_full_control_sids=())
    runtime = FakeRuntime(tmp_path)
    import asyncio

    setattr(runtime, remote_command_module.PENDING_ATTRIBUTE, {
        "prompt_message_id": 42, "user_id": 7, "chat_id": 11, "expires_at": time.time() + 60,
    })
    handled = asyncio.run(remote_command_module.handle_pending_reply(runtime, update(text=TOKEN, reply_to=None)))
    assert handled is False
# HASHI-Fingerprint: hfp_914ec44b5acb48e6bee5c30cf4312347
