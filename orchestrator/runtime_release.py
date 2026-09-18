"""PAO Functions: qualify the complete shared and Agent runtime release."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from orchestrator.function_generation import probe_function_generation
from orchestrator.function_worker_supervisor import (
    load_bootable_generation_cache,
    materialize_generation_artifact,
    persist_qualified_generation_cache,
)
from orchestrator.runtime_contract import RuntimeFingerprint
from orchestrator.pathing import build_bridge_paths

logger = logging.getLogger("BridgeU.Orchestrator")


def qualify_release(payload: dict) -> dict:
    root = Path(payload["code_root"]).resolve()
    bridge_home = Path(payload["bridge_home"]).resolve()
    runtime = RuntimeFingerprint.from_mapping(payload["runtime"])
    paths = build_bridge_paths(root, bridge_home, canonical_home=True)
    try:
        generation = probe_function_generation(
            SimpleNamespace(
                paths=paths,
                runtime_fingerprint=runtime,
            )
        )
        artifact = materialize_generation_artifact(bridge_home, generation)
        try:
            persist_qualified_generation_cache(bridge_home, generation, artifact)
        except Exception as exc:
            logger.warning(
                "Function startup succeeded but its recovery cache could not be saved: "
                "%s: %s",
                type(exc).__name__,
                exc,
            )
    except Exception as candidate_error:
        cached = load_bootable_generation_cache(bridge_home, root, runtime)
        if cached is None:
            raise
        generation, artifact = cached
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
    }
