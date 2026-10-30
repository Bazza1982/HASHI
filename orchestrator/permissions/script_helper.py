"""Fixed Safety helper bridge; user Python is sent only to the LPAC Runner."""
from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from orchestrator.permissions.ipc_rpc import PermissionPipeClient
from orchestrator.permissions.windows_capabilities import python_runtime_capabilities
from orchestrator.permissions.script_protocol import validate
from orchestrator.permissions.wire import decode
from orchestrator.permissions.windows_file_binding import BoundWindowsRead, _Windows


METHODS = frozenset({"prepare_script", "run_script", "stop_script", "script_result"})


class ScriptNotStarted(ValueError):
    """The helper sent no request which could start user Python."""


def run(plan, handles):
    required = {"arguments", "cwd", "descriptor", "binding", "operation_id", "runner", "parent_job_name",
                "input_indices", "result"}
    if not isinstance(plan, dict) or set(plan) != required:
        raise ValueError("invalid fixed script-helper plan")
    arguments = validate(plan["arguments"])
    descriptor = plan["runner"]
    if (not isinstance(descriptor, dict) or set(descriptor) != {"endpoint", "public_key", "root_job_name", "runner_sid"}
            or not isinstance(plan["input_indices"], list)
            or len(plan["input_indices"]) != len(arguments["inputs"])):
        raise ValueError("invalid installed script Runner/input binding")
    if any(type(index) is not int or not 0 <= index < len(handles) for index in plan["input_indices"]):
        raise ValueError("invalid script input handle index")
    client = PermissionPipeClient(
        descriptor["endpoint"], Ed25519PublicKey.from_public_bytes(base64.b64decode(
            descriptor["public_key"], validate=True,
        )), methods=METHODS,
    )
    binding, operation_id = decode(plan["binding"]), plan["operation_id"]
    api, bounds, snapshots, total = _Windows(), [], [], 0
    run_sent = False
    activity_serial = 0

    def activity(event):
        nonlocal activity_serial
        # A fixed path derived from the supervisor-owned result path. No code,
        # stdout or credential enters activity records.
        metadata = getattr(event, "metadata", None)
        if not isinstance(metadata, dict):
            return
        allowed = {key: metadata[key] for key in ("state", "liveness", "progress", "counters") if key in metadata}
        activity_serial += 1
        allowed["activity_serial"] = activity_serial
        path = Path(plan["result"]).with_name("progress.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(allowed, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    try:
        for handle in handles:
            bounds.append(BoundWindowsRead(api, [handle]))
        for item, index in zip(arguments["inputs"], plan["input_indices"]):
            data = bounds[index].read(max_bytes=4 * 1024 * 1024)
            total += len(data)
            if total > 4 * 1024 * 1024:
                raise ValueError("script snapshots exceed their aggregate bound")
            snapshots.append({"name": item["name"], "data": base64.b64encode(data).decode("ascii"),
                              "sha256": sha256(data).hexdigest()})
        staged = {"arguments": arguments, "snapshots": snapshots, "cwd": plan["cwd"],
                  "descriptor": plan["descriptor"]}
        observation = client.call("prepare_script", {
            "binding": binding, "operation_id": operation_id, "parent_job_name": plan["parent_job_name"],
            "root_job_name": descriptor["root_job_name"], "plan": staged,
        })
        if (not isinstance(observation, dict) or observation.get("user_sid") != descriptor["runner_sid"]
                or observation.get("lpac") is not True or observation.get("elevated") is not False
                or observation.get("atomic_job") is not True or not python_runtime_capabilities(observation)
                or observation.get("parent_job_count") != 2 or not observation.get("appcontainer_sid")):
            raise ValueError("script process has no matching native qualification")
        run_sent = True
        result = client.call("run_script", {"binding": binding, "operation_id": operation_id}, on_event=activity)
        if (not isinstance(result, dict) or set(result) != {"outcome", "payload", "result_digest"}
                or result["outcome"] not in {"succeeded", "failed"} or not isinstance(result["payload"], str)
                or len(result["payload"].encode("utf-8")) > 900000
                or sha256(result["payload"].encode("utf-8")).hexdigest() != result["result_digest"]):
            raise ValueError("script result does not match its durable observation")
        return {"outcome": result["outcome"], "data": base64.b64encode(result["payload"].encode("utf-8")).decode("ascii")}
    except Exception as error:
        if not run_sent:
            raise ScriptNotStarted("the isolated script could not be prepared") from error
        raise
    finally:
        # Even a prepare RPC with a lost reply may own a suspended process.
        # Native parent Jobs independently stop it if this helper is killed.
        errors = []
        try:
            stopped = client.call("stop_script", {"binding": binding, "operation_id": operation_id})
            if not isinstance(stopped, dict) or stopped.get("stopped") is not True:
                raise OSError("script resource retirement remains unverified")
        except Exception as error:
            errors.append(error)
        for bound in bounds:
            try:
                bound.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise OSError("script helper resource retirement remains unverified") from errors[0]
