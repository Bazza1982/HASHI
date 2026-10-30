from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager

from orchestrator import config_json, telegram_credential
from tests.test_workbench_telegram_credential import NEW_TOKEN, _server


def test_concurrent_user_grant_and_token_save_do_not_deadlock(tmp_path, monkeypatch):
    async def verify(_token):
        return {"id": 123, "username": "safe_bot"}

    server = _server(tmp_path, verifier=verify)
    credentials = server.telegram_credential_service
    access = server.telegram_access_service
    credential_revision = credentials.status("lily")["credential_revision"]
    access_revision = access.status("lily")["revision"]
    config_locked = threading.Event()
    release_config = threading.Event()
    access_started = threading.Event()
    errors = []
    results = {}

    @contextmanager
    def pause_config_lock(path):
        with config_json.file_write_lock(path):
            config_locked.set()
            if not release_config.wait(timeout=5):
                raise TimeoutError("test did not release the config lock")
            yield

    monkeypatch.setattr(telegram_credential, "file_write_lock", pause_config_lock)

    def save_token():
        try:
            results["credential"] = asyncio.run(credentials.replace(
                "lily", token=NEW_TOKEN, expected_revision=credential_revision,
                replace_existing=True,
            ))
        except Exception as exc:
            errors.append(exc)

    def add_user():
        try:
            access_started.set()
            results["access"] = access.change(
                "lily", expected_revision=access_revision,
                scope="agent", action="add", user_id="200",
            )
        except Exception as exc:
            errors.append(exc)

    token_thread = threading.Thread(target=save_token, daemon=True)
    user_thread = threading.Thread(target=add_user, daemon=True)
    token_thread.start()
    assert config_locked.wait(timeout=5)
    user_thread.start()
    assert access_started.wait(timeout=5)
    time.sleep(0.2)
    release_config.set()
    token_thread.join(timeout=8)
    user_thread.join(timeout=8)

    assert not token_thread.is_alive() and not user_thread.is_alive()
    assert errors == []
    assert results["credential"]["configured"] is True
    assert results["access"]["agent_user_ids"] == ["200"]
    assert config_json.read_config_json(tmp_path / "secrets.json")["lily.telegram"] == NEW_TOKEN
