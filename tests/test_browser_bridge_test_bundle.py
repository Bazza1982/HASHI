from __future__ import annotations

import json
from pathlib import Path

from tools.browser_bridge_test_bundle import build_test_bundle


def _write_inputs(root: Path) -> None:
    state = root / "state"
    logs = root / "logs"
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    smoke_results = {
        "root_dir": str(root),
        "generated_at": "2026-04-25T00:00:00+00:00",
        "status": "manual_required",
        "results": [
            {"id": "launch_chrome", "status": "manual_required"},
            {"id": "healthcheck", "status": "passed"},
            {"id": "ping", "status": "passed"},
            {"id": "active_tab", "status": "passed"},
            {"id": "get_text", "status": "passed"},
            {"id": "screenshot", "status": "passed"},
        ],
    }
    (state / "smoke_results.json").write_text(json.dumps(smoke_results, indent=2) + "\n", encoding="utf-8")
    trace_events = [
        {"event": "server_started", "socket_path": "/tmp/harness.sock"},
        {"event": "request", "action": "ping"},
        {"event": "response", "action": "ping", "ok": True},
        {"event": "request", "action": "ping"},
        {"event": "response", "action": "ping", "ok": True},
        {"event": "request", "action": "active_tab"},
        {"event": "response", "action": "active_tab", "ok": True},
        {"event": "request", "action": "get_text"},
        {"event": "response", "action": "get_text", "ok": True},
        {"event": "request", "action": "screenshot"},
        {"event": "response", "action": "screenshot", "ok": True},
        {"event": "server_stopped", "socket_path": "/tmp/harness.sock"},
    ]
    (logs / "stub_bridge_trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in trace_events) + "\n",
        encoding="utf-8",
    )


def test_build_test_bundle(tmp_path: Path) -> None:
    _write_inputs(tmp_path)
    bundle_dir = tmp_path / "bundle"
    result = build_test_bundle(
        tmp_path,
        bundle_dir=bundle_dir,
        rollback_commit="HEAD",
        repo_root=Path("/home/lily/projects/hashi"),
        distro_name="Ubuntu-22.04",
    )

    assert result["bundle_dir"] == str(bundle_dir)
    assert (bundle_dir / "extension" / "manifest.json").exists()
    assert (bundle_dir / "state" / "maturity_report.json").exists()
    assert (bundle_dir / "state" / "handoff_summary.md").exists()
    install_ps1 = (bundle_dir / "install" / "INSTALL_HASHI_BROWSER_BRIDGE_TEST.ps1").read_text(encoding="utf-8")
    assert "Ubuntu-22.04" in install_ps1
    assert "/home/lily/projects/hashi" in install_ps1
    assert "cd /d %LOCALAPPDATA%" in install_ps1
    assert "python3 -m tools.browser_native_host" in install_ps1
    readme = (bundle_dir / "README.txt").read_text(encoding="utf-8")
    assert "pre_live_ready" in readme
    meta = json.loads((bundle_dir / "bundle_meta.json").read_text(encoding="utf-8"))
    assert meta["expected_extension_id"] == "jdeaedmoejdapldleofeggedgenogpka"
