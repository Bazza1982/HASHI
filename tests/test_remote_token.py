from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from orchestrator import remote_token
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.workbench_auth import (
    REMOTE_SHARED_TOKEN_DISABLED_KEY,
    ensure_local_api_credential,
)

TOKEN = "AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf0123456789_-AbCdEf0123"


def seed(root: Path, payload: dict) -> Path:
    secrets = root / "secrets.json"
    write_config_json(secrets, payload, private_full_control_sids=())
    return secrets


def test_token_validation_trims_safe_pastes_and_rejects_whitespace() -> None:
    assert remote_token.token_issue(f"  {TOKEN}\n") == ""
    assert remote_token.token_issue("") == "missing"
    assert remote_token.token_issue(None) == "missing"
    assert remote_token.token_issue("short") == "too_short"
    assert remote_token.token_issue("ab cd" + "x" * 20) == "contains_whitespace_or_control"
    assert remote_token.token_issue("x" * 513) == "too_long"


def test_fingerprint_and_mask_never_expose_the_middle() -> None:
    masked = remote_token.mask_token(TOKEN)
    assert masked.endswith(TOKEN[-4:])
    assert TOKEN[4:40] not in masked
    fingerprint = remote_token.token_fingerprint(TOKEN)
    assert len(fingerprint) == remote_token.FINGERPRINT_LENGTH
    assert all(character in "0123456789abcdef" for character in fingerprint)
    assert fingerprint != remote_token.token_fingerprint(TOKEN + "x")


def test_replace_writes_the_token_and_reports_only_a_mask(tmp_path: Path) -> None:
    seed(tmp_path, {"other_key": "keep-me"})
    before = read_config_json(tmp_path / "secrets.json").revision

    status = remote_token.replace_token(tmp_path, f"  {TOKEN}  ", private_sids=())

    assert status.configured is True
    assert status.source == "secrets"
    assert status.masked == remote_token.mask_token(TOKEN)
    assert status.fingerprint == remote_token.token_fingerprint(TOKEN)
    assert status.revision and status.revision != before
    stored = read_config_json(tmp_path / "secrets.json")
    assert stored["hashi_remote_shared_token"] == TOKEN
    assert stored["other_key"] == "keep-me"
    assert REMOTE_SHARED_TOKEN_DISABLED_KEY not in stored
    assert TOKEN not in json.dumps(status.as_dict())


def test_clear_sets_the_disabled_marker_and_survives_startup(tmp_path: Path) -> None:
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN, "other_key": "keep-me"})

    status = remote_token.clear_token(tmp_path, private_sids=())

    assert status.configured is False
    assert status.source == "disabled"
    assert status.disabled is True
    stored = read_config_json(tmp_path / "secrets.json")
    assert "hashi_remote_shared_token" not in stored
    assert stored[REMOTE_SHARED_TOKEN_DISABLED_KEY] is True
    assert stored["other_key"] == "keep-me"

    # Startup provisioning must not recreate what the owner removed.
    reprovisioned = ensure_local_api_credential(stored, profile="personal")
    assert "hashi_remote_shared_token" not in reprovisioned
    assert reprovisioned[REMOTE_SHARED_TOKEN_DISABLED_KEY] is True


def test_environment_override_blocks_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(tmp_path, {})
    monkeypatch.setenv("HASHI_REMOTE_SHARED_TOKEN", TOKEN)

    with pytest.raises(remote_token.RemoteTokenError) as replace_error:
        remote_token.replace_token(tmp_path, TOKEN, private_sids=())
    assert replace_error.value.error_code == "remote_token_environment_locked"

    with pytest.raises(remote_token.RemoteTokenError) as clear_error:
        remote_token.clear_token(tmp_path, private_sids=())
    assert clear_error.value.error_code == "remote_token_environment_locked"

    status = remote_token.load_status(tmp_path, env={"HASHI_REMOTE_SHARED_TOKEN": TOKEN})
    assert status.source == "environment"
    assert status.environment_locked is True


def test_a_changed_file_is_rejected_instead_of_overwritten(tmp_path: Path) -> None:
    seed(tmp_path, {"hashi_remote_shared_token": "first-token-0123456789"})
    revision = read_config_json(tmp_path / "secrets.json").revision
    seed(tmp_path, {"hashi_remote_shared_token": "second-token-0123456789"})

    with pytest.raises(remote_token.RemoteTokenError) as error:
        remote_token.replace_token(tmp_path, TOKEN, expected_revision=revision, private_sids=())
    assert error.value.error_code == "remote_token_revision_conflict"
    assert read_config_json(tmp_path / "secrets.json")["hashi_remote_shared_token"] == "second-token-0123456789"


def test_invalid_input_writes_nothing(tmp_path: Path) -> None:
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    before = read_config_json(tmp_path / "secrets.json").revision

    with pytest.raises(remote_token.RemoteTokenError) as error:
        remote_token.replace_token(tmp_path, "has space inside", private_sids=())
    assert error.value.error_code == "remote_token_invalid"
    assert read_config_json(tmp_path / "secrets.json").revision == before


def test_remote_generation_is_read_from_the_loopback_health_endpoint() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server contract
            payload = {"credential": {
                "configured": True, "generation": 7,
                "last_reload_at": 1_700_000_000, "configuration_state": "ready",
            }}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep the test output clean
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        credential = remote_token.fetch_remote_credential(server.server_address[1], timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert credential["reachable"] is True
    assert credential["configured"] is True
    assert credential["generation"] == 7


def test_status_reports_an_unreachable_remote_instead_of_guessing(tmp_path: Path) -> None:
    seed(tmp_path, {"hashi_remote_shared_token": TOKEN})
    status = remote_token.load_status(
        tmp_path,
        env={},
        remote_credential={"reachable": False, "reason": "unreachable"},
    )
    assert status.configured is True
    assert status.remote["reachable"] is False
# HASHI-Fingerprint: hfp_ce26cb18bc0d480eb43babfdbb204363
