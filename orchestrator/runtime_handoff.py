"""PAO owner of durable shared-process handoff and recovery metadata."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.kernel_process import instance_runtime_dir, write_record
from orchestrator.runtime_contract import compare_runtime_fingerprints
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint


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
    result = {
        "agents": list(agents),
        "manually_stopped_agents": sorted(
            getattr(getattr(app, "agent_lifecycle", None), "manually_stopped_agents", ())
        ),
        "agent_generations": agents,
        "generations": generations,
        "telegram_offsets": {
            name: getattr(ingress, "acknowledged_offset", ingress.offset)
            for name, ingress in app.function_workers._telegram_ingress.items()
        },
        "telegram_bot_offsets": {
            name: {"offset": getattr(ingress, "acknowledged_offset", ingress.offset), "bot_fingerprint": fingerprint}
            for name, ingress in app.function_workers._telegram_ingress.items()
            if (fingerprint := telegram_bot_fingerprint(getattr(ingress, "token", None)))
        },
        "services": [
            name
            for name in ("workbench_api", "api_gateway")
            if getattr(app, name, None) is not None
        ],
    }
    global_config = getattr(app, "global_cfg", None)
    profile = str(getattr(global_config, "deployment_profile", "personal") or "personal")
    owner = str(getattr(global_config, "owner_scope_id", "") or "").strip()
    if global_config is not None and profile == "personal" and owner:
        from orchestrator.personal_data_owner import read_personal_data_binding

        api_config = getattr(getattr(app, "workbench_api", None), "global_config", None)
        if api_config is not None:
            from orchestrator.session_store import SessionStore

            api_profile = str(getattr(api_config, "deployment_profile", "personal") or "personal")
            if api_profile != profile or SessionStore.owner_id_for(api_config) != owner:
                raise ValueError("runtime and live session API data owners disagree")
        binding = read_personal_data_binding(app.paths.bridge_home)
        if binding is not None and binding[0] != owner:
            raise ValueError("runtime personal data binding differs from its persisted binding")
        result["personal_data_binding"] = {
            "version":1,
            "data_owner_id":owner,
            "legacy_owner_known":binding[1] if binding is not None else False,
        }
    return result


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


def checkpoint_offset(
    bridge_home, agent: str, offset: int | None, *, bot_fingerprint: str | None = None
) -> None:
    path = instance_runtime_dir(bridge_home) / "telegram-offsets.json"
    offsets = json.loads(path.read_text()) if path.exists() else {}
    offsets[agent] = offset
    write_record(path, offsets)
    if bot_fingerprint:
        # Keep the legacy numeric projection readable by rollback generations.
        # New generations restore this atomic identity/offset pair instead of
        # interpreting a cursor from a different Bot as an acknowledgement.
        identities_path = path.with_name("telegram-bot-offsets.json")
        identities = json.loads(identities_path.read_text()) if identities_path.exists() else {}
        identities[agent] = {"offset": offset, "bot_fingerprint": bot_fingerprint}
        write_record(identities_path, identities)


def _later_offset(left, right):
    values = [value for value in (left, right) if type(value) is int]
    return max(values) if values else None


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
        for name, offset in json.loads(offset_path.read_text()).items():
            prior = handoff["telegram_offsets"].get(name)
            handoff["telegram_offsets"][name] = _later_offset(prior, offset)
    identities_path = root / "telegram-bot-offsets.json"
    if identities_path.exists():
        pairs = handoff.setdefault("telegram_bot_offsets", {})
        for name, saved in json.loads(identities_path.read_text()).items():
            prior = pairs.get(name) or {}
            if prior.get("bot_fingerprint") == saved.get("bot_fingerprint"):
                saved = {**saved, "offset": _later_offset(prior.get("offset"), saved.get("offset"))}
            pairs[name] = saved
            handoff["telegram_offsets"][name] = saved.get("offset")
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
