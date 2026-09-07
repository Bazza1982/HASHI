"""PAO Functions: qualify the complete shared and Agent runtime release."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestrator.function_generation import probe_function_generation
from orchestrator.function_worker_supervisor import materialize_generation_artifact
from orchestrator.runtime_contract import RuntimeFingerprint
from orchestrator.pathing import build_bridge_paths


def qualify_release(payload: dict) -> dict:
    root = Path(payload["code_root"]).resolve()
    runtime = RuntimeFingerprint.from_mapping(payload["runtime"])
    generation = probe_function_generation(
        SimpleNamespace(
            paths=build_bridge_paths(root, payload["bridge_home"], canonical_home=True),
            runtime_fingerprint=runtime,
        )
    )
    artifact = materialize_generation_artifact(Path(payload["bridge_home"]), generation)
    return {
        "manifest": generation.manifest.to_dict(),
        "runtime": runtime.to_dict(),
        "generation_root": str(artifact),
        "entrypoint": payload["entrypoint"],
    }
