"""Explicit foreground Runner entrypoint; import/build never starts a process."""
from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4
from pathlib import Path
import signal
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.config_json import read_config_json
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.capsule import qualify_capsule
from orchestrator.permissions.ipc_server import PermissionEndpoint
from orchestrator.permissions.runner import RestrictedHerRunner
from orchestrator.permissions.windows_ipc import PipePeerPolicy, WindowsPipeListener, current_process_user_sid
from orchestrator.permissions.windows_security import InstallationLock, require_private_path
from orchestrator.permissions.wire import PermissionServiceRouter


def read_capsule(raw, *, trusted_sids):
    if not isinstance(raw, dict) or set(raw) != {"manifest", "sha256"}:
        raise ValueError("a pinned sealed runtime capsule is required")
    capsule = qualify_capsule(raw["manifest"], raw["sha256"], trusted_sids=trusted_sids)
    if (Path(sys.executable).resolve() != capsule.executable.resolve()
            or tuple(capsule.manifest["python_version"]) != tuple(sys.version_info[:3])):
        raise PermissionDenied("the service is not running the installed isolated interpreter")
    return capsule


def build_runner(path):
    source = Path(path)
    raw = read_config_json(source)
    required = {
        "version", "instance_id", "runner_sid", "safety_sid", "worker_sid",
        "private_root", "transport_key", "endpoint", "capsule",
    }
    if (not isinstance(raw, dict) or not required.issubset(raw)
            or set(raw) - required - {"script_result_key", "script_limits"} or raw["version"] != 1):
        raise ValueError("invalid private Runner installation")
    sid = current_process_user_sid()
    if sid != raw["runner_sid"]:
        raise PermissionDenied("the configured Runner account is not active")
    private = Path(raw["private_root"])
    require_private_path(source, trusted_sids=(sid,))
    require_private_path(private, trusted_sids=(sid,))
    key_path = Path(raw["transport_key"])
    require_private_path(key_path, trusted_sids=(sid,))
    transport_material = key_path.read_bytes()
    key = Ed25519PrivateKey.from_private_bytes(transport_material)
    capsule = read_capsule(raw["capsule"], trusted_sids=(sid, raw["safety_sid"]))
    lock = InstallationLock(private)
    try:
        script_results = None
        limits = raw.get("script_limits", {})
        if (not isinstance(limits, dict) or set(limits) - {"memory_mb", "scratch_mb"}
                or any(type(value) is not int for value in limits.values())):
            raise ValueError("invalid installed script limits")
        if raw.get("script_result_key") is not None:
            from orchestrator.permissions.result_store import EncryptedResultStore

            result_key = Path(raw["script_result_key"])
            require_private_path(result_key, trusted_sids=(sid,))
            result_material = result_key.read_bytes()
            if result_key.resolve() == key_path.resolve() or result_material == transport_material:
                raise ValueError("script encryption and Runner transport require separate keys")
            script_results = EncryptedResultStore(private / "script-results.sqlite", key=result_material)
        elif limits:
            raise ValueError("script execution needs its separate result key")
        runner = RestrictedHerRunner(
            runner_sid=sid, safety_sid=raw["safety_sid"], worker_sid=raw["worker_sid"],
            private_root=private, executable=capsule.executable, entry=capsule.host_entry,
            executable_digest=capsule.file_digest(capsule.executable),
            entry_digest=capsule.file_digest(capsule.host_entry), environment=capsule.environment,
            capsule_roots=(capsule.root,), instance_id=raw["instance_id"], capsule=capsule,
            script_results=script_results, script_memory_bytes=limits.get("memory_mb", 1024) * 1024**2,
            script_scratch_bytes=limits.get("scratch_mb", 64) * 1024**2,
        )
        endpoint = PermissionEndpoint(
            WindowsPipeListener(raw["endpoint"], service_sid=sid, peer_policy=PipePeerPolicy(raw["safety_sid"])),
            PermissionServiceRouter(runner, methods=runner.METHODS), signing_key=key,
            service_generation=uuid4().hex,
        )
        return runner, endpoint, lock
    except BaseException:
        lock.close()
        raise


async def serve(path):
    runner, endpoint, lock = build_runner(path)
    loop, stopped = asyncio.get_running_loop(), asyncio.Event()
    previous = {}
    for kind in (signal.SIGINT, signal.SIGTERM):
        previous[kind] = signal.getsignal(kind)
        signal.signal(kind, lambda *args: loop.call_soon_threadsafe(stopped.set))
    try:
        await asyncio.to_thread(runner.records.recover_all)
        await endpoint.start()
        await stopped.wait()
    finally:
        # Do not release the installation lock if a child Job still has an
        # unknown outcome; a second Runner must not adopt it as clean.
        errors = await asyncio.gather(endpoint.close(), runner.close(), return_exceptions=True)
        if any(isinstance(error, BaseException) for error in errors):
            raise OSError("Runner shutdown remains unverified")
        lock.close()
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the explicitly installed HASHI permission Runner")
    parser.add_argument("installation", type=Path)
    asyncio.run(serve(parser.parse_args(argv).installation))


if __name__ == "__main__":
    main()
