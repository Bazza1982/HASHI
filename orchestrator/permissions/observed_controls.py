"""Actual installed-path observations; configuration alone never means L2."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json

from orchestrator.permissions.backend_capabilities import (
    ActivePath, BackendBinding, ControlFinding, ServiceObservation, project_controls, CONTROLS,
)
from orchestrator.permissions.models import PermissionLevel
from orchestrator.permissions.windows_capabilities import python_runtime_capabilities


def execution_report(execution):
    execution.channel._current()
    factory, binding, policy = execution.factory, execution.binding, execution.policy
    supervisor = execution.channel.registry._permission_gate._supervisor
    definitions = execution.channel.tool_definitions()
    names = tuple(sorted({item["function"]["name"] for item in definitions}))
    supported = {name for name in names if supervisor.supports(name)}
    special = {"request_access", "ask_user", "get_user_answer"}
    inventory = [{"tool": name, "available": name in supported or name in special}
                 for name in names]
    digest = sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
    observed = factory.authority.observed_time()
    runner = factory.runner
    host = (getattr(runner, "_prepared", {}).get(binding.context.execution_epoch)
            if runner is not None else None)
    native = bool(isinstance(host, dict) and host.get("lpac") is True
                  and python_runtime_capabilities(host) and host.get("elevated") is False
                  and host.get("atomic_job") is True and host.get("parent_job_count") == 1)
    level = policy.settings.requested_level
    layer = "L1" if level is PermissionLevel.L1 else "L2"
    ready = level is PermissionLevel.L1 or native
    states = {
        "files": ("enforced" if ready and supported & {"file_read", "file_write", "file_search", "file_list", "apply_patch"}
                  else "unavailable" if ready else "unknown"),
        "scripts": "enforced" if ready and "run_script" in supported else "unavailable",
        "network": "enforced" if ready and supported & {"web_fetch", "http_request"} else "unavailable",
        "credentials": "enforced" if ready and "http_request" in supported and supervisor.credential_names else "unavailable",
        "devices": "unavailable",
        # No setting can attest that a browser/desktop holding human credentials
        # is isolated from co-located unmanaged agents. L1 keeps its trust model.
        "approval": "enforced" if level is PermissionLevel.L1 else "partial",
        "cleanup": "enforced" if ready and not execution._closed else "unknown",
    }
    paths, findings = [], []
    for name in CONTROLS:
        path = "installed:" + name
        paths.append(ActivePath(path, (name,)))
        state = states[name]
        reason = ("approval_isolation_unverified" if name == "approval" and layer == "L2"
                  else "unsupported_profile" if state == "unavailable"
                  else "native_host_unverified" if state == "unknown" else "profile_ready")
        dependencies = (("installed:approval", "approval"),) if layer == "L2" and name not in {"approval", "devices"} else ()
        findings.append(ControlFinding(path, name, state, layer if state in {"enforced", "partial"} else None,
                                       reason, dependencies))
    actual_binding = BackendBinding(binding.context.instance_id, binding.context.backend_id,
                                    factory.helper.entry_digest, "installed-native", factory.helper.executable_digest,
                                    factory.authority._service_generation, str(policy.revision) + ":" + digest)
    observation = ServiceObservation(actual_binding, observed, observed + 5, not execution._closed, tuple(findings))
    report = project_controls(level, actual_binding, paths, inventory_complete=True, observation=observation, now=observed)
    inference = execution._inference or {}
    return {
        "binding": asdict(actual_binding), "requested_level": level.value,
        "observed_at": observed, "expires_at": observed + 5,
        "controls": {name: asdict(value) for name, value in report.controls},
        "inventory": inventory,
        "analysis": {"privacy_level": inference.get("privacy_level"),
                     "models": [{"engine": item["engine"], "model": item["model"]}
                                for item in inference.get("profiles", [])]},
    }
