"""PAO owner of durable shared-process handoff and recovery metadata."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.kernel_process import instance_runtime_dir, write_record
from orchestrator.runtime_contract import compare_runtime_fingerprints


def snapshot(app) -> dict:
    generations = {}
    agents = {}
    for handle in app.runtimes:
        client = handle.client
        generation_id = client.generation_id
        agents[handle.name] = generation_id
        generations[generation_id] = {
            "generation": client.generation.to_dict(),
            "root": str(client.generation_root),
        }
    return {
        "agents": list(agents),
        "agent_generations": agents,
        "generations": generations,
        "telegram_offsets": {
            name: ingress.offset
            for name, ingress in app.function_workers._telegram_ingress.items()
        },
        "services": [
            name
            for name in ("workbench_api", "api_gateway")
            if getattr(app, name, None) is not None
        ],
    }


def persist(app) -> None:
    if (
        not getattr(app, "_shared_committed", False)
        or getattr(app, "_handoff_draining", False)
        or getattr(app, "is_stopping", False)
    ):
        return
    write_record(
        instance_runtime_dir(app.paths.bridge_home) / "function-topology.json",
        {
            "schema": 1,
            "shared_generation_id": app.shared_generation_id,
            "handoff": snapshot(app),
        },
    )


def checkpoint_offset(bridge_home, agent: str, offset: int) -> None:
    path = instance_runtime_dir(bridge_home) / "telegram-offsets.json"
    offsets = json.loads(path.read_text()) if path.exists() else {}
    offsets[agent] = offset
    write_record(path, offsets)


def load(app) -> dict:
    root = instance_runtime_dir(app.paths.bridge_home)
    record = json.loads((root / "function-topology.json").read_text())
    if (
        record.get("schema") != 1
        or record.get("shared_generation_id") != app.shared_generation_id
    ):
        raise ValueError(
            "shared recovery checkpoint does not match the committed generation"
        )
    handoff = record["handoff"]
    offset_path = root / "telegram-offsets.json"
    if offset_path.exists():
        handoff["telegram_offsets"].update(json.loads(offset_path.read_text()))
    return handoff


def agent_artifacts(app, handoff: dict) -> dict:
    from orchestrator.function_worker_supervisor import (
        generation_from_dict,
        verify_generation_artifact,
    )

    result = {}
    for name in handoff["agents"]:
        generation_id = handoff["agent_generations"][name]
        stored = handoff["generations"][generation_id]
        generation = generation_from_dict(stored["generation"])
        root = Path(stored["root"]).resolve()
        expected_root = (
            app.paths.bridge_home
            / "state"
            / "function_generations"
            / generation_id.removeprefix("sha256:")
        ).resolve()
        if root != expected_root or generation.manifest.generation_id != generation_id:
            raise ValueError(
                "Agent recovery artifact is outside its instance or generation"
            )
        compare_runtime_fingerprints(
            app.runtime_fingerprint, generation.receipt.runtime
        )
        verify_generation_artifact(root, generation)
        result[name] = (generation, root)
    return result
