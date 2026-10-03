"""PAO Functions: qualify the complete shared and Agent runtime release."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from orchestrator.function_generation import (
    UncommittedFunctionSourceError,
    probe_function_generation,
)
from orchestrator.function_worker_supervisor import (
    load_bootable_generation_cache,
    materialize_generation_artifact,
    persist_qualified_generation_cache,
)
from orchestrator.runtime_contract import RuntimeFingerprint
from orchestrator.pathing import build_bridge_paths

logger = logging.getLogger("BridgeU.Orchestrator")


def qualify_release(payload: dict) -> dict:
    started_at = datetime.now().astimezone().isoformat()
    started = time.perf_counter()
    phases_ms: dict[str, float] = {}

    def record_phase(name: str, elapsed_ms: float) -> None:
        phases_ms[name] = round(elapsed_ms, 1)

    def timed(name: str, function, *args, **kwargs):
        phase_started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            record_phase(name, (time.perf_counter() - phase_started) * 1000)

    root = Path(payload["code_root"]).resolve()
    bridge_home = Path(payload["bridge_home"]).resolve()
    runtime = RuntimeFingerprint.from_mapping(payload["runtime"])
    paths = timed(
        "build_paths", build_bridge_paths, root, bridge_home, canonical_home=True
    )
    adoption = {"status": "qualified", "reason_code": None}
    try:
        generation = timed(
            "generation_qualification",
            probe_function_generation,
            SimpleNamespace(
                paths=paths,
                runtime_fingerprint=runtime,
            ),
            timing_callback=record_phase,
        )
        artifact = timed(
            "materialize_artifact",
            materialize_generation_artifact,
            bridge_home,
            generation,
        )
        try:
            timed(
                "persist_cache",
                persist_qualified_generation_cache,
                bridge_home,
                generation,
                artifact,
            )
        except Exception as exc:
            logger.warning(
                "Function startup succeeded but its recovery cache could not be saved: "
                "%s: %s",
                type(exc).__name__,
                exc,
            )
    except Exception as candidate_error:
        cached = timed(
            "load_fallback_cache",
            load_bootable_generation_cache,
            bridge_home,
            root,
            runtime,
        )
        if cached is None:
            raise
        generation, artifact = cached
        adoption = {
            "status": "fallback",
            "reason_code": (
                "source_uncommitted"
                if isinstance(candidate_error, UncommittedFunctionSourceError)
                else "qualification_failed"
            ),
        }
        logger.warning(
            "New Function generation was rejected; continuing startup with the last "
            "verified generation: %s: %s",
            type(candidate_error).__name__,
            candidate_error,
        )
    return {
        "manifest": generation.manifest.to_dict(),
        "runtime": runtime.to_dict(),
        "generation_root": str(artifact),
        "entrypoint": payload["entrypoint"],
        "adoption": adoption,
        "qualification_timing": {
            "started_at": started_at,
            "ended_at": datetime.now().astimezone().isoformat(),
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
            "phases_ms": phases_ms,
        },
    }
