"""HASHI process entry. Product code runs only in verified child processes."""

from __future__ import annotations

# ruff: noqa: E402 -- enforce the runtime before other project imports.
import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path
from uuid import uuid4

CODE_ROOT = Path(__file__).resolve().parent
# Isolated script execution omits its directory; admit only this entry's root.
sys.path.insert(0, str(CODE_ROOT))

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE, RuntimeContractError, bind_process_runtime,
    enforce_runtime_contract, load_core_runtime_selection,
)

from orchestrator.instance_lock import InstanceLock
from orchestrator.kernel_process import (
    KernelRuntime,
    write_record,
    instance_runtime_dir,
    canonical_instance_home,
)


def main(argv=None) -> int:
    try:
        enforce_runtime_contract(CODE_ROOT, role=CORE_HOST_ROLE)
        selection = load_core_runtime_selection(CODE_ROOT)
        bind_process_runtime(selection.core, code_root=CODE_ROOT)
        runtime = enforce_runtime_contract(CODE_ROOT, role=CORE_HOST_ROLE)
    except RuntimeContractError as exc:
        print(f"HASHI Core runtime rejected: {exc}", file=sys.stderr, flush=True)
        return 78
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-home")
    parser.add_argument(
        "--replace-functions",
        action="store_true",
        help="Explicitly replace shared Functions after draining all active work",
    )
    args, function_args = parser.parse_known_args(argv)
    bridge_home = canonical_instance_home(CODE_ROOT, args.bridge_home)
    state_dir = instance_runtime_dir(bridge_home)
    if args.replace_functions:
        status = json.loads((state_dir / "kernel.json").read_text(encoding="utf-8"))
        if status.get("phase") != "active":
            raise RuntimeError("Core is not ready for a shared Function replacement")
        request_id = uuid4().hex
        write_record(
            state_dir / "kernel-requests" / (request_id + ".json"), {"id": request_id}
        )
        print(
            f"Shared Function replacement requested: {request_id}. Check its receipt for completion.",
            flush=True,
        )
        return 0
    lock = InstanceLock(state_dir / "process.lock", pid_path=state_dir / "process.pid")
    lock.acquire()
    try:

        async def run():
            kernel = KernelRuntime(
                CODE_ROOT,
                bridge_home,
                runtime,
                functions_runtime=selection.functions,
                arguments={"argv": function_args},
            )
            loop = asyncio.get_running_loop()
            for name in ("SIGINT", "SIGTERM", "SIGHUP"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    try:
                        loop.add_signal_handler(sig, kernel.stop_event.set)
                    except (NotImplementedError, RuntimeError):
                        pass
            await kernel.run()

        asyncio.run(run())
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_6ff292b0ac43480fa8a69f0a8ee2629a
