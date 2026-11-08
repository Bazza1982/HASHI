"""Real scheduling/threads on the offline loop; OS isolation is verified separately."""
import asyncio
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest


def test_offline_loop_runs_timers_thread_wakeup_and_cancellation_without_sockets(monkeypatch):
    from orchestrator.permissions.offline_asyncio import new_event_loop

    def forbidden(*args, **kwargs):
        pytest.fail("the offline loop attempted to create a socket")

    monkeypatch.setattr(socket, "socketpair", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    loop = new_event_loop()

    async def work():
        assert await asyncio.to_thread(lambda: "real-thread") == "real-thread"
        ready = asyncio.Event()
        thread = threading.Thread(target=lambda: loop.call_soon_threadsafe(ready.set))
        thread.start()
        await asyncio.wait_for(ready.wait(), 1)
        thread.join()
        await asyncio.sleep(.01)
        pending = asyncio.create_task(asyncio.sleep(100))
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(NotImplementedError):
            await loop.create_connection(None, "127.0.0.1", 9)
        return "complete"

    try:
        assert loop.run_until_complete(work()) == "complete"
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows asyncio import path")
def test_fixed_bootstrap_avoids_the_denied_overlapped_import_in_a_fresh_process(tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    program = """
import sys, importlib.abc
sys.path.insert(0, ROOT)
from orchestrator.permissions import offline_asyncio
# Arrange the independently observed platform boundary; this is not an OS test.
offline_asyncio._require_lpac = lambda: None
class RejectOverlapped(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == '_overlapped':
            raise PermissionError(10013, 'native LPAC has no network capability')
sys.meta_path.insert(0, RejectOverlapped())
offline_asyncio.install()
import asyncio
async def work():
    await asyncio.sleep(.01)
    return await asyncio.to_thread(lambda: 42)
assert asyncio.run(work()) == 42
assert '_overlapped' not in sys.modules
assert asyncio.get_event_loop_policy()._hashi_profile == offline_asyncio.PROFILE_ID
print('offline bootstrap complete')
""".replace("ROOT", repr(root))
    result = subprocess.run([sys.executable, "-I", "-S", "-c", program],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline bootstrap complete"
# HASHI-Fingerprint: hfp_138ca02df7024c5bbb86a8741eebeac9
