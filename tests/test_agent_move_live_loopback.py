"""Cross-process Remote protocol regression using only disposable local instances."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from orchestrator.agent_move.package import create_agent_move_package
from orchestrator.agent_move.remote_client import (
    AgentMoveRemoteClient,
    AgentMoveRemoteError,
    connect_agent_move_receiver,
)
from orchestrator.agent_move.transport_crypto import ENVELOPE_SCHEME, encrypt_package_transport
from orchestrator.pcm import render_pcm_document
from orchestrator.session_store import SessionStore


_TOKEN = "fixture-transfer-token-only"
_SERVER = r"""
import sys
from pathlib import Path
import uvicorn
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.terminal.executor import TerminalExecutor
root=Path(sys.argv[1]); port=int(sys.argv[2])
app=create_app(
    {"instance_id":"HASHI2", "display_name":"Synthetic target", "remote_port":port},
    PairingManager(storage_dir=root/"pairing", lan_mode=True),
    TerminalExecutor(), hashi_root=str(root))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
"""


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _root(path: Path, instance: str, owner: str, *, with_agent: bool) -> Path:
    path.mkdir()
    agents = []
    if with_agent:
        workspace = path / "workspaces" / "zelda"
        workspace.mkdir(parents=True)
        (workspace / "agent.md").write_text(
            render_pcm_document(persona="Fixture", system="Fixture", memory="Fixture"),
            encoding="utf-8",
        )
        agents.append({
            "name": "zelda", "type": "flex", "workspace_dir": "workspaces/zelda",
            "active_backend": "codex-cli",
            "allowed_backends": [{"engine": "codex-cli", "model": "fixture"}],
            "is_active": True,
        })
    _json(path / "agents.json", {
        "global": {"instance_id": instance, "owner_scope_id": owner},
        "agents": agents,
    })
    _json(path / "tasks.json", {"version": 1, "heartbeats": [], "crons": [], "nudges": []})
    _json(path / "secrets.json", {"hashi_remote_shared_token": _TOKEN})
    return path


def _history(store: SessionStore, owner: str, agent: str, request: str) -> str:
    session = store.ensure_default_session(owner_id=owner, agent_id=agent)
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id=owner, agent_id=agent,
        request_id=request, text=request + " question", source="workbench",
        idempotency_key=request,
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id, success=True, assistant_text=request + " answer",
        assistant_source="fixture",
    )
    return session["session_id"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Receiver:
    def __init__(self, root: Path, port: int) -> None:
        self.root = root
        self.port = port
        self.process: subprocess.Popen | None = None
        self.error_log = root / "fixture-server.log"

    def start(self) -> None:
        with self.error_log.open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-c", _SERVER, str(self.root), str(self.port)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                cwd=Path(__file__).resolve().parents[1],
            )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("synthetic receiver exited: " + self.error_log.read_text(
                    encoding="utf-8", errors="replace"
                )[-4000:])
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=0.5
                ) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        raise RuntimeError("synthetic receiver did not start")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=8)
        self.process = None


@pytest.mark.parametrize("operation", ["clone", "move"])
def test_real_remote_process_maps_personal_history_after_receiver_restart(
    tmp_path, monkeypatch, operation,
):
    # Never read or write the invoking machine's real home, credentials or nonce store.
    home = tmp_path / "private-home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    for key in list(os.environ):
        if key.upper().startswith(("HASHI_", "BRIDGE_", "KASUMI_")):
            monkeypatch.delenv(key, raising=False)

    source = _root(tmp_path / "source", "HASHI1", "owner:source", with_agent=True)
    target = _root(tmp_path / "target", "HASHI2", "owner:target", with_agent=False)
    source_store = SessionStore(source / "state" / "sessions.sqlite3", instance_id="HASHI1")
    source_session = _history(source_store, "owner:source", "zelda", "source-history")
    original = source_store.messages(source_session, owner_id="owner:source")
    target_store = SessionStore(target / "state" / "sessions.sqlite3", instance_id="HASHI2")
    unrelated = _history(target_store, "owner:target", "other-agent", "unrelated")
    package_path = tmp_path / "synthetic.hashi-agent"
    target_agent = "zelda-copy" if operation == "clone" else "zelda"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation=operation,
        transfer_mode="workspace" if operation == "clone" else "identity_memory",
        history_mode="copy" if operation == "clone" else "move",
        include_telegram_secret=False,
    )

    port = _free_port()
    receiver = _Receiver(target, port)
    instances = {"HASHI2": {"host": "127.0.0.1", "remote_port": port}}
    try:
        receiver.start()
        with pytest.raises(AgentMoveRemoteError):
            connect_agent_move_receiver(
                instances, "HASHI2", source_instance="HASHI1", shared_token="wrong-fixture-token",
            )
        client = connect_agent_move_receiver(
            instances, "HASHI2", source_instance="HASHI1", shared_token=_TOKEN,
        )
        before_target = (target / "agents.json").read_bytes()
        wrong_instance = {"HASHI3": {"host": "127.0.0.1", "remote_port": port}}
        with pytest.raises(AgentMoveRemoteError):
            connect_agent_move_receiver(
                wrong_instance, "HASHI3", source_instance="HASHI1", shared_token=_TOKEN,
            )
        wrong_source = AgentMoveRemoteClient(
            base_url=client.base_url, target_instance="HASHI2", source_instance="HASHI3",
            shared_token=_TOKEN, capabilities=client.capabilities,
        )
        with pytest.raises(AgentMoveRemoteError):
            wrong_source.stage(package_path, operation=operation, target_agent_id=target_agent)

        raw = package_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        encrypted = bytearray(encrypt_package_transport(
            raw, shared_token=_TOKEN, source_instance="HASHI1",
            target_instance="HASHI2", package_sha256=digest,
        ))
        encrypted[-1] ^= 1
        with pytest.raises(AgentMoveRemoteError):
            client._request(
                "/agent-move/v1/stage", method="POST", timeout=30,
                payload={
                    "from_instance": "HASHI1", "encryption": ENVELOPE_SCHEME,
                    "package_b64": base64.b64encode(encrypted).decode("ascii"),
                    "sha256": digest, "operation": operation, "target_agent_id": target_agent,
                },
            )
        assert (target / "agents.json").read_bytes() == before_target
        staged = client.stage(package_path, operation=operation, target_agent_id=target_agent)
        assert staged["status"] == "staged"
        assert staged["package_id"] == package.package_id

        receiver.stop()
        receiver.start()
        client = connect_agent_move_receiver(
            instances, "HASHI2", source_instance="HASHI1", shared_token=_TOKEN,
        )
        assert client.status(package.package_id)["status"] == "staged"
        first = client.commit(package.package_id)
        second = client.commit(package.package_id)
        assert first["status"] == second["status"] == "committed_inactive"

        imported = target_store.list_sessions(
            owner_id="owner:target", agent_id=target_agent, include_archived=True,
        )
        assert len(imported) == 1
        assert [row["text"] for row in target_store.messages(
            imported[0]["session_id"], owner_id="owner:target",
        )] == ["source-history question", "source-history answer"]
        assert target_store.list_sessions(
            owner_id="owner:source", agent_id=target_agent, include_archived=True,
        ) == []
        later = _history(target_store, "owner:target", target_agent, "later")
        assert client.rollback(package.package_id)["status"] == "rolled_back"
        assert [row["text"] for row in target_store.messages(
            later, owner_id="owner:target",
        )] == ["later question", "later answer"]
        assert [row["text"] for row in target_store.messages(
            unrelated, owner_id="owner:target",
        )] == ["unrelated question", "unrelated answer"]
        assert source_store.messages(source_session, owner_id="owner:source") == original
    finally:
        receiver.stop()



def test_real_personal_move_with_empty_selected_history_keeps_old_owner(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    for key in list(os.environ):
        if key.upper().startswith(("HASHI_", "BRIDGE_", "KASUMI_")):
            monkeypatch.delenv(key, raising=False)

    source = _root(tmp_path / "source", "HASHI1", "", with_agent=True)
    target = _root(tmp_path / "target", "HASHI2", "owner:target", with_agent=False)
    source_store = SessionStore(source / "state" / "sessions.sqlite3", instance_id="HASHI1")
    original_session = _history(source_store, "user:7", "zelda", "legacy-only")
    original = source_store.messages(original_session, owner_id="user:7")
    path = tmp_path / "empty-selected.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", path, source_instance="HASHI1", operation="move",
        transfer_mode="identity_memory", history_mode="move", include_telegram_secret=False,
    )
    assert package.conversation_continuity["owner_id"] == "user:0"
    assert package.conversation_continuity["summary"]["eligible_message_count"] == 0
    assert any("other local conversation history" in warning for warning in package.manifest["warnings"])

    port = _free_port()
    receiver = _Receiver(target, port)
    try:
        receiver.start()
        client = connect_agent_move_receiver(
            {"HASHI2": {"host": "127.0.0.1", "remote_port": port}},
            "HASHI2", source_instance="HASHI1", shared_token=_TOKEN,
        )
        assert client.stage(path, operation="move", target_agent_id="zelda")["status"] == "staged"
        committed = client.commit(package.package_id)
        assert committed["conversation_continuity"]["imported_messages"] == 0
        target_store = SessionStore(target / "state" / "sessions.sqlite3", instance_id="HASHI2")
        assert target_store.list_sessions(owner_id="user:7", agent_id="zelda") == []
        assert source_store.messages(original_session, owner_id="user:7") == original
    finally:
        receiver.stop()
