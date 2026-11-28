"""Iron rule: the Worker startup path must not import enterprise or Agent Move.

``orchestrator.flexible_agent_runtime`` is imported by every Worker, including
personal-profile Workers that never touch the enterprise subsystem and never
move an agent.  The enterprise package is a lazy re-export shim and every
product caller now imports the concrete module it needs, so a personal Worker
startup must not resolve any enterprise submodule (beyond the profile module
``orchestrator/config.py`` validates on load) nor any ``agent_move`` module.

Anything that reads a name off ``orchestrator.enterprise`` at runtime instead of
importing its module also drops out of the Functions generation manifest, which
is how an enabled enterprise scheduler lease silently lost its store.  The
manifest test below pins the modules the production lease path imports.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from orchestrator.enterprise.channel_gate import EnterpriseChannelGate
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

ROOT = Path(__file__).resolve().parents[1]

ALLOWED_ENTERPRISE_ON_STARTUP = {
    "orchestrator.enterprise",
    "orchestrator.enterprise.profile",
}

_PROBE = """
import json, sys
from types import SimpleNamespace

import orchestrator.flexible_agent_runtime as runtime

runtime_obj = object.__new__(runtime.FlexibleAgentRuntime)
runtime_obj.global_config = SimpleNamespace(deployment_profile="personal")
gate = runtime_obj._build_channel_gate()
result = gate.check_ingress("telegram", actor_id=1, user_id="1", agent_id="probe")

enterprise = sorted(
    name for name in sys.modules
    if name == "orchestrator.enterprise" or name.startswith("orchestrator.enterprise.")
)
agent_move = sorted(
    name for name in sys.modules
    if name == "orchestrator.agent_move" or name.startswith("orchestrator.agent_move.")
)
print(json.dumps({
    "allowed": result.allowed,
    "reason": result.reason,
    "enterprise": enterprise,
    "agent_move": agent_move,
}))
"""


def _probe() -> dict:
    completed = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_personal_worker_startup_skips_enterprise_and_agent_move():
    report = _probe()
    leaked_enterprise = [
        name for name in report["enterprise"] if name not in ALLOWED_ENTERPRISE_ON_STARTUP
    ]
    assert leaked_enterprise == [], (
        "personal Worker startup imported enterprise modules: " f"{leaked_enterprise}"
    )
    assert report["agent_move"] == [], (
        "personal Worker startup imported Agent Move: " f"{report['agent_move']}"
    )


def test_personal_channel_gate_allows_ingress():
    report = _probe()
    assert report["allowed"] is True
    assert report["reason"] == "personal_profile"


def test_governed_profile_still_builds_the_enterprise_gate(tmp_path):
    runtime_obj = object.__new__(FlexibleAgentRuntime)
    runtime_obj.global_config = SimpleNamespace(
        deployment_profile="enterprise",
        organization_id="ORG-001",
        bridge_home=tmp_path,
    )

    gate = runtime_obj._build_channel_gate()

    assert isinstance(gate, EnterpriseChannelGate)
    assert gate.governed is True
    assert gate.org_id == "ORG-001"
    assert gate.audit_writer.enabled is True


def test_generation_manifest_keeps_the_enterprise_lease_modules():
    from orchestrator.function_generation import (
        FUNCTION_GENERATION_ENTRYPOINTS,
        build_source_manifest,
    )
    from orchestrator.manager_registry import FUNCTION_MANAGER_SPECS

    manifest = build_source_manifest(
        [
            *FUNCTION_GENERATION_ENTRYPOINTS,
            *(spec.module for spec in FUNCTION_MANAGER_SPECS),
        ],
        code_root=ROOT,
    )

    names = set(manifest.module_names)
    for required in (
        "orchestrator.enterprise.leases",
        "orchestrator.enterprise.kubernetes_leases",
        "orchestrator.enterprise.identity",
    ):
        assert required in names, f"{required} is missing from the Functions generation"
# HASHI-Fingerprint: hfp_f51d1f6e2ebb4a34b9c93d6014d12c36
