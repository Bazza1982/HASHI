"""Connector health is re-rendered when degradation flips, and only then."""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.startup_manager import StartupManager


class _Handle:
    def __init__(self) -> None:
        self.telegram_connected = True
        self.metadata: dict[str, object] = {}


def _build_kernel(renders: list[dict]) -> tuple[SimpleNamespace, dict]:
    ingress: dict[str, dict] = {}
    handles = {"alpha": _Handle(), "beta": _Handle()}

    def snapshot(name: str) -> dict:
        return dict(ingress.get(name) or {
            "configured": True, "running": False, "connected": False,
        })

    kernel = SimpleNamespace(
        startup_status={
            "agent_order": ["alpha", "beta"],
            "agent_states": {"alpha": "local", "beta": "local"},
            "agent_reasons": {},
            "services_ready": True,
            "ready": False,
            "issues": [],
        },
        function_workers=SimpleNamespace(
            telegram_ingress_snapshot=snapshot, _telegram_ingress=ingress,
        ),
        _runtime_map=lambda: dict(handles),
        _report_startup=lambda: renders.append(dict(kernel.startup_status)),
    )
    return kernel, ingress


def _connect(ingress: dict, name: str, connected: bool) -> None:
    ingress[name] = {"configured": True, "running": True, "connected": connected}


def test_startup_summary_renders_on_first_state_and_on_degradation_flips() -> None:
    renders: list[dict] = []
    kernel, ingress = _build_kernel(renders)
    manager = StartupManager(kernel, None)

    # Every connector is down: startup degrades and the summary renders once.
    _connect(ingress, "alpha", False)
    _connect(ingress, "beta", False)
    manager.reconcile_connector_status()
    assert len(renders) == 1
    assert kernel.startup_status["degraded"] is True
    unavailable = [issue["details"]["agents"] for issue in kernel.startup_status["issues"]
                   if issue["code"] == "agent_telegram_unavailable"]
    assert unavailable == [["alpha", "beta"]]

    # One agent recovers while startup is still degraded: no redraw.
    _connect(ingress, "alpha", True)
    manager.reconcile_connector_status()
    assert len(renders) == 1

    # Full recovery flips degradation, so the summary has to be rendered again.
    _connect(ingress, "beta", True)
    manager.reconcile_connector_status()
    assert len(renders) == 2
    assert kernel.startup_status["degraded"] is False
    assert kernel.startup_status["issues"] == []

    # A fresh failure flips degradation back and must render again.
    _connect(ingress, "beta", False)
    manager.reconcile_connector_status()
    assert len(renders) == 3
    assert kernel.startup_status["degraded"] is True

    # Unpublished reconciliations only update state; they never redraw.
    _connect(ingress, "beta", True)
    manager.reconcile_connector_status(publish=False)
    assert len(renders) == 3
    assert kernel.startup_status["degraded"] is False
