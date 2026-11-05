"""Product-neutral spawned-process entry and artifact import boundary."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.kernel_import_guard import candidate_import_guard
from orchestrator.kernel_artifact import GenerationModuleFinder, verify_artifact
from orchestrator.runtime_contract import (
    RuntimeFingerprint,
    compare_runtime_fingerprints,
    enforce_runtime_contract,
)


def run_generation_process(connection, bootstrap: dict) -> None:
    try:
        if bootstrap.get("process_group") and os.name != "nt" and os.getsid(0) != os.getpid():
            os.setsid()
        code_root = Path(bootstrap["code_root"]).resolve()
        artifact = Path(bootstrap["generation_root"]).resolve()
        runtime = enforce_runtime_contract(code_root)
        compare_runtime_fingerprints(
            RuntimeFingerprint.from_mapping(bootstrap["runtime"]), runtime
        )
        verify_artifact(artifact, bootstrap["manifest"])
        os.environ.update(
            {
                "BRIDGE_HOME": bootstrap["bridge_home"],
                "HASHI_SOURCE_ROOT": str(code_root),
                "HASHI_FUNCTION_GENERATION_ROOT": str(artifact),
                "HASHI_FUNCTION_WORKER": "1",
                "HASHI_FUNCTION_WORKER_AGENT": str(bootstrap.get("agent_name") or ""),
            }
        )
        # No product imports may happen before the manifest finder is installed.
        sys.meta_path.insert(
            0,
            GenerationModuleFinder(
                generation_root=artifact,
                code_root=code_root,
                manifest=bootstrap["manifest"],
            ),
        )
        module, separator, attribute = bootstrap["entrypoint"].partition(":")
        if not separator or module not in {
            entry["module"] for entry in bootstrap["manifest"]["entries"]
        }:
            raise ValueError("entrypoint is outside the qualified generation")
        with candidate_import_guard():
            entry = getattr(importlib.import_module(module), attribute)
        asyncio.run(entry(connection, bootstrap))
    finally:
        connection.close()


def run_function_worker_process(connection, bootstrap: dict) -> None:
    run_generation_process(connection, bootstrap)


QUALIFICATION_RESULT_PREFIX = "HASHI_RUNTIME_RELEASE="


def qualify_payload(payload: dict) -> dict:
    code_root = Path(payload["code_root"]).resolve()
    runtime = enforce_runtime_contract(code_root)
    compare_runtime_fingerprints(RuntimeFingerprint.from_mapping(payload["runtime"]), runtime)
    os.environ["HASHI_SOURCE_ROOT"] = str(code_root)
    # Product qualification is the same import boundary as a prepared child.
    profile = json.loads((code_root / "runtime-entry.json").read_text(encoding="utf-8"))
    if payload["qualifier"] != profile["qualifier"] or payload["entrypoint"] != profile["entrypoint"]:
        raise ValueError("qualification entrypoint mismatch")
    module, separator, attribute = payload["qualifier"].partition(":")
    if not separator:
        raise ValueError("invalid qualification entrypoint")
    with candidate_import_guard():
        qualifier = getattr(importlib.import_module(module), attribute)
    result = qualifier(payload)
    return result


def qualify_process() -> None:
    result = qualify_payload(json.loads(sys.stdin.read()))
    print(QUALIFICATION_RESULT_PREFIX + json.dumps(result), flush=True)


def run_private_call(connection, bootstrap, purpose, nonce):
    from orchestrator.function_worker_protocol import encode_envelope
    try:
        if purpose == "qualification":
            result = qualify_payload(bootstrap)
        elif purpose == "candidate-probe":
            profile = json.loads((Path(bootstrap["code_root"]) / "runtime-entry.json").read_text(encoding="utf-8"))
            declaration = bootstrap["probe_entrypoint"]
            if declaration != profile["candidate_probe"]:
                raise ValueError("candidate probe entrypoint mismatch")
            module, separator, attribute = declaration.partition(":")
            if not separator:
                raise ValueError("invalid candidate probe entrypoint")
            with candidate_import_guard():
                probe = getattr(importlib.import_module(module), attribute)
            result = probe(bootstrap)
        else:
            raise ValueError("unknown protected call")
        response = {"version": 1, "kind": "response", "id": nonce, "ok": True, "result": result}
    except Exception as exc:
        response = {"version": 1, "kind": "response", "id": nonce, "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)}}
    try:
        connection.send_bytes(encode_envelope(response))
    finally:
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--ipc-handle":
        from orchestrator.runtime_process import receive_private_bootstrap
        channel, bootstrap, purpose, nonce = receive_private_bootstrap(int(sys.argv[2]))
        if purpose == "generation":
            run_generation_process(channel, bootstrap)
        else:
            run_private_call(channel, bootstrap, purpose, nonce)
    else:
        qualify_process()
# HASHI-Fingerprint: hfp_c6a0b80f5e2245c3b83fabd39a38fbaf
