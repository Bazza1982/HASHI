"""Real Windows pipe peer identity, message boundaries and listener ownership."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest

from orchestrator.permissions.windows_ipc import (
    PipePeerPolicy, WindowsPeer, WindowsPipeListener, connect_pipe, current_process_user_sid,
)


def test_peer_roles_do_not_accept_claimed_user_or_process_fields():
    policy = PipePeerPolicy("S-1-5-21-1-2-3-1001", "S-1-15-2-100", process_id=4, require_lpac=True)
    policy.verify(WindowsPeer(4, policy.user_sid, policy.appcontainer_sid, False, True))
    for peer in (
        WindowsPeer(4, policy.user_sid, None, False, False),
        WindowsPeer(8, policy.user_sid, policy.appcontainer_sid, False, True),
        WindowsPeer(4, policy.user_sid, policy.appcontainer_sid, True, True),
        WindowsPeer(4, policy.user_sid, policy.appcontainer_sid, False, False),
        WindowsPeer(4, "S-1-5-21-1-2-3-1002", policy.appcontainer_sid, False, True),
    ):
        with pytest.raises(PermissionError):
            policy.verify(peer)


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows named-pipe kernel")
def test_native_pipe_uses_os_pid_and_sid_and_can_accept_after_idle_timeout():
    sid = current_process_user_sid()
    endpoint = r"\\.\pipe\LOCAL\HASHI.Permissions.test-" + uuid4().hex
    listener = WindowsPipeListener(endpoint, service_sid=sid,
                                   peer_policy=PipePeerPolicy(sid, process_id=os.getpid()))
    try:
        assert listener.accept(timeout_s=0.01) is None

        def exchange():
            accepted = listener.accept(timeout_s=3)
            assert accepted is not None
            pipe, peer, hello = accepted
            try:
                assert peer.process_id == os.getpid() and peer.user_sid == sid
                assert hello["process_id"] != peer.process_id
                pipe.send({"ok": True})
            finally:
                pipe.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(exchange)
            client = connect_pipe(endpoint, timeout_s=3)
            try:
                client.send({"type": "hashi.permission-hello.v1", "process_id": os.getpid() + 100})
                assert client.receive(timeout_s=3) == {"ok": True}
            finally:
                client.close()
            result.result(timeout=5)
        assert listener.accept(timeout_s=0.01) is None
    finally:
        listener.close()
