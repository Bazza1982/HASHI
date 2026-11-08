"""Real permission RPC rejections retain safe, non-granting presentation facts."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.commands import safety
from orchestrator.permissions import ipc_rpc
from orchestrator.permissions.ipc_rpc import PermissionPipeClient
from orchestrator.permissions.wire import PermissionRemoteError, PermissionServiceRouter
from tests.test_permission_decision_commands import control as control


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
async def test_actual_control_rpc_authentication_and_presentation(control, monkeypatch, authenticated):
    _, _, authority, _, _, request, subject, session, _, controller = control
    key = Ed25519PrivateKey.generate()
    router = PermissionServiceRouter(controller, methods=("issue_session_actions",))

    class Loopback:
        def __init__(self):
            self.frames = []
            self.closed = False

        def send(self, frame):
            if frame["type"] == "hashi.permission-hello.v1":
                self.frames.append(ipc_rpc.welcome(
                    frame, signing_key=key, service_generation="test", endpoint="private-test",
                ))
            else:
                class ServerPipe:
                    def send(_, reply):
                        self.frames.append(reply)
                asyncio.run(ipc_rpc.serve_request(ServerPipe(), router, frame))

        def receive(self, **kwargs):
            return self.frames.pop(0)

        def close(self):
            self.closed = True

    pipe = Loopback()
    monkeypatch.setattr(ipc_rpc, "connect_pipe", lambda _: pipe)
    client = PermissionPipeClient("private-test", key.public_key(), methods=("issue_session_actions",))
    arguments = {
        "subject": subject, "request_id": request.request_id, "revision": request.revision,
        "session_token": session.token if authenticated else "", "presentation_id": "original-ui",
    }
    if authenticated:
        actions = await asyncio.to_thread(client.call, "issue_session_actions", arguments)
        assert actions and all(action.request_id == request.request_id for action in actions)
    else:
        with pytest.raises(PermissionRemoteError) as raised:
            await asyncio.to_thread(client.call, "issue_session_actions", arguments)
        assert raised.value.code == "SessionDecisionDenied"
        failure = safety._failure(SimpleNamespace(), raised.value, phase="presentation")
        assert failure.reason == "scope_unavailable"
        assert not failure.unconfirmed
        assert session.token not in failure.render()
    with sqlite3.connect(authority.path) as db:
        assert db.execute("SELECT count(*) FROM approval_decisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM execution_claims").fetchone()[0] == 0
        count = db.execute("SELECT count(*) FROM decision_actions").fetchone()[0]
        assert (count > 0) is authenticated
    assert pipe.closed


@pytest.mark.parametrize(("error", "mutation_started", "reason"), [
    (PermissionRemoteError("SessionDecisionDenied"), False, "scope_unavailable"),
    (PermissionRemoteError("PermissionDenied"), False, "scope_unavailable"),
    (PermissionRemoteError("RevisionConflict"), False, "state_changed"),
    (PermissionRemoteError("permission_service_unavailable"), False, "service_unavailable"),
    (PermissionRemoteError("private-token-path"), False, "service_unavailable"),
    (PermissionRemoteError("SessionDecisionDenied"), True, "io_error"),
    (PermissionRemoteError("permission_service_unavailable"), True, "io_error"),
    (OSError("private-token-path"), False, "io_error"),
])
def test_presentation_error_classification_never_confirms_an_uncertain_mutation(
    error, mutation_started, reason,
):
    failure = safety._failure(
        SimpleNamespace(), error, phase="presentation", mutation_started=mutation_started,
    )
    assert failure.reason == reason
    assert failure.unconfirmed is mutation_started
    assert "private-token-path" not in failure.render()
    assert "private-token-path" not in str(failure.diagnostic())
# HASHI-Fingerprint: hfp_ecab956768484ca7b8d4c5040130e23c
