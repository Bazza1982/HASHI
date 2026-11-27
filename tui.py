#!/usr/bin/env python3
"""HASHI TUI - Terminal-first interface for onboarding and daily use.

Usage:
    python tui.py
    python tui.py --rescue      # force the stdlib rescue shell

Wraps main.py as a subprocess. Split-panel terminal UI:
  - Upper panel: real-time logs from bridge
  - Lower panel: chat with agents via Workbench API

TUI-only commands:
  /to <name>     Switch active agent
  /to all        Broadcast to all agents
  /agents        List available agents
  /instance      List trusted local/LAN HASHI instances
  /instance <id> Switch through authenticated Hashi Remote
  /instance current  Return to the launch instance
  /sidepanel     Open the read-only information panel
  /sidepanel off Close the information panel
  /doctor        Local environment, port, log and dependency checks
  /logs          Tail of the newest launcher / bridge logs
  /log           Pause/resume log scrolling
  /quit          Graceful shutdown
  All other /commands are forwarded to the active agent.

The TUI is the rescue hatch for a broken installation, so this entry point is
fail-open: a missing TUI package, an unreadable instance configuration or a
crashing app never leaves the operator with a closed window and no diagnosis.
It falls through to the standard-library rescue shell instead.
"""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

# Ensure the project root is on sys.path so `tui.*` and `orchestrator.*` imports work
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_PORT  # noqa: E402

# The documented fallback identity, identical to the value
# ``tui.instances.load_launch_instance`` has always applied.
DEFAULT_LAUNCH_INSTANCE_ID = "HASHI"


def _get_bridge_home() -> Path:
    configured = str(os.environ.get("BRIDGE_HOME") or "").strip()
    return Path(configured).resolve() if configured else Path(_project_root)


def _instances():
    """Import the (dependency-light) instance helpers on demand."""

    from tui import instances

    return instances


def _get_workbench_urls() -> tuple[str, list[str], str]:
    """Resolve this repository's identity and local Workbench candidates."""
    bridge_home = _get_bridge_home()
    endpoint_setting = str(os.environ.get("HASHI_LOCAL_ENDPOINT_FILE") or "").strip()
    if endpoint_setting:
        helpers = _instances()
        instance_id, port = helpers.load_local_endpoint(bridge_home, Path(endpoint_setting))
        endpoint_url = helpers.local_workbench_urls(port)[0]
        configured = str(os.environ.get("HASHI_WORKBENCH_URL") or "").strip().rstrip("/")
        if configured and configured != endpoint_url:
            raise RuntimeError(
                "HASHI_WORKBENCH_URL does not match the verified local endpoint"
            )
        return instance_id, [endpoint_url], endpoint_url

    instance_id, port = _launch_identity(bridge_home)
    urls = _instances().local_workbench_urls(port)
    configured = str(os.environ.get("HASHI_WORKBENCH_URL") or "").strip().rstrip("/")
    if configured:
        urls = [configured, *(url for url in urls if url != configured)]
    return instance_id, urls, urls[0]


def _get_workbench_url() -> str:
    """Compatibility accessor for callers that only need the primary URL."""
    return _get_workbench_urls()[2]


def _launch_identity(bridge_home: Path) -> tuple[str, int]:
    """Return this installation's identity, or raise when it cannot be trusted.

    The defaults are the existing legal ones (``HASHI`` on
    ``DEFAULT_WORKBENCH_PORT``, the same values ``tui.instances`` has always
    applied), so an installation without ``agents.json`` - a first install -
    and every older configuration that predates these fields still start.

    A *declared* but unusable value is a different matter: the configuration is
    broken, and guessing around it could attach the window to another
    installation that happens to own the port. That raises, and ``_launch``
    opens the offline rescue shell instead. The same happens when an explicit
    ``HASHI_LOCAL_ENDPOINT_FILE`` fails verification - the TUI never promotes a
    failed identity check into a guessed online session.
    """

    path = Path(bridge_home) / "agents.json"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return DEFAULT_LAUNCH_INSTANCE_ID, DEFAULT_WORKBENCH_PORT
    except OSError as exc:
        raise RuntimeError(f"agents.json is unreadable at {path}: {exc.strerror or exc}") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:  # includes UnicodeDecodeError and JSON syntax errors
        raise RuntimeError(f"agents.json is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RuntimeError("agents.json is not a JSON object")
    global_cfg = document.get("global") if isinstance(document.get("global"), dict) else {}
    instance_id = str(global_cfg.get("instance_id") or DEFAULT_LAUNCH_INSTANCE_ID).strip().upper()
    declared_port = global_cfg.get("workbench_port")
    if declared_port is None or declared_port == "":
        return instance_id, DEFAULT_WORKBENCH_PORT
    try:
        port = int(declared_port)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("agents.json declares a non-numeric global.workbench_port") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"agents.json declares an invalid global.workbench_port ({port})")
    return instance_id, port


def _rescue(reason: str) -> int:
    """Enter the standard-library rescue shell instead of exiting."""

    try:
        from tui.rescue_cli import run as run_rescue
    except Exception:  # pragma: no cover - only if the checkout is damaged
        print("TUI could not start: " + reason, file=sys.stderr)
        print("The rescue shell is unavailable too; the installation is incomplete.", file=sys.stderr)
        return 78
    return run_rescue(Path(_project_root), _get_bridge_home(), reason)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True, description="HASHI terminal UI")
    parser.add_argument(
        "--rescue",
        action="store_true",
        help="start the standard-library rescue shell without the full TUI",
    )
    return parser.parse_args(argv)


def _launch(*, onboarding_mode: bool = False) -> int:
    """Start the TUI for this verified instance, or fall through to rescue.

    Shared by ``tui.py`` and ``tui_onboarding.py`` so both entry points answer a
    missing package, an unverified identity and a crashing app the same way.
    """

    try:
        from tui.app import HASHITuiApp
    except Exception as exc:
        return _rescue(
            f"the TUI packages or modules are unavailable ({type(exc).__name__}: {exc})"
        )

    # No agents.json at all is a first install, not a broken one: the
    # configuration wizard is the entry point and needs no existing instance.
    if not (Path(_get_bridge_home()) / "agents.json").is_file():
        onboarding_mode = True

    try:
        instance_id, workbench_urls, primary_url = _get_workbench_urls()
    except Exception as exc:
        return _rescue(
            "this installation's identity could not be verified "
            f"({type(exc).__name__}: {exc}); refusing to connect to a guessed instance"
        )

    try:
        app = HASHITuiApp(
            workbench_url=primary_url,
            onboarding_mode=onboarding_mode,
            workbench_urls=workbench_urls,
            bridge_home=_get_bridge_home(),
            code_root=Path(_project_root),
            launch_instance_id=instance_id,
        )
        app.run()
    except Exception as exc:
        traceback.print_exc()
        return _rescue(f"the TUI stopped unexpectedly ({type(exc).__name__}: {exc})")
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.rescue:
        return _rescue("requested with --rescue")
    return _launch()


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_035db0e31d074026802af5ce88bb48c4
