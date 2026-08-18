from __future__ import annotations

import json
from pathlib import Path

from orchestrator.onboarding_gate import run_onboarding_gate
from orchestrator.pathing import build_bridge_paths


def test_existing_agents_with_utf8_bom_skip_onboarding(tmp_path: Path, monkeypatch) -> None:
    agents_path = tmp_path / "agents.json"
    payload = {"global": {}, "agents": [{"name": "existing-agent"}]}
    agents_path.write_text(json.dumps(payload), encoding="utf-8-sig")
    paths = build_bridge_paths(tmp_path, bridge_home=tmp_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("onboarding must not run for an existing BOM config")

    monkeypatch.setattr("orchestrator.onboarding_gate.subprocess.run", fail_if_called)

    assert run_onboarding_gate(paths, tmp_path) is False


def test_shell_launcher_gate_accepts_utf8_bom() -> None:
    launcher = Path(__file__).resolve().parents[1] / "bin" / "bridge-u.sh"

    assert "open(sys.argv[1], encoding='utf-8-sig')" in launcher.read_text(encoding="utf-8")
