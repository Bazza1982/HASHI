"""Zero-dependency rescue shell for HASHI.

This module is the last line of defence behind the TUI: it imports nothing but
the Python standard library, so it still works when ``textual``/``rich`` are
missing, a virtual environment is half-built, or the download was interrupted.

It is used in two ways:

* ``python tui.py --rescue`` (or automatically) when the TUI packages are
  unavailable - it prints the diagnosis and takes interactive commands;
* inside a healthy TUI through ``/doctor`` and ``/logs``, which call
  :func:`diagnose` and :func:`logs_tail` directly.

Everything here reports what it actually observed. A failed probe is reported
as a failure with the reason, never as "fine".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

RESCUE_BANNER = "HASHI rescue shell (standard library only)"
DEFAULT_PORT = 18800

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"


def _read_json(path: Path) -> tuple[dict, str]:
    """Return ``(document, error)``; never raises."""

    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return {}, f"unreadable: {exc.strerror or exc}"
    except UnicodeDecodeError as exc:
        # A binary or mis-encoded config must be reported, never fatal: this
        # function runs inside the rescue shell, which exists for exactly this.
        return {}, f"not valid UTF-8 text: {exc}"
    try:
        document = json.loads(text)
    except ValueError as exc:
        return {}, f"invalid JSON: {exc}"
    if not isinstance(document, dict):
        return {}, "top level is not a JSON object"
    return document, ""


def instance_identity(home: Path) -> tuple[str, int, str]:
    """Return ``(instance_id, workbench_port, error)`` from agents.json."""

    document, error = _read_json(Path(home) / "agents.json")
    if error:
        return "HASHI", DEFAULT_PORT, error
    global_cfg = document.get("global") if isinstance(document.get("global"), dict) else {}
    instance_id = str(global_cfg.get("instance_id") or "HASHI").strip().upper()
    try:
        port = int(global_cfg.get("workbench_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return instance_id, DEFAULT_PORT, "workbench_port is not a number"
    return instance_id, port, ""


def _module_available(name: str) -> bool:
    try:
        __import__(name)
    except Exception:
        return False
    return True


def core_runtime(root: Path) -> tuple[list[str] | None, str]:
    """Return the installer-qualified Core runtime command, or why it is unavailable.

    Imported lazily: this module must stay standard-library only, and the
    selector lives in the repository tree.
    """

    root = Path(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from scripts.hashi_instance_cli import _select_core_runtime
    except Exception as exc:
        return None, f"the Core runtime selector is unavailable ({type(exc).__name__}: {exc})"
    try:
        command, _functions = _select_core_runtime(root, with_functions=True)
    except Exception as exc:
        return None, f"no installer-qualified Core runtime is available ({type(exc).__name__}: {exc})"
    if not isinstance(command, list) or not command:
        return None, "the Core runtime selector returned no command"
    return [str(part) for part in command], ""


def _contract_status(root: Path, executable: str | None = None) -> tuple[str, str]:
    """Run the server dependency contract check under the Core interpreter.

    The TUI interpreter proves nothing about the server: checking the contract
    with it reports ordinary TUI/server drift as a server fault. ``executable``
    is the installer-qualified Core interpreter.
    """

    script = Path(root) / "scripts" / "check_runtime_contract.py"
    if not script.is_file():
        return CHECK_WARN, "scripts/check_runtime_contract.py is missing"
    if not executable:
        return CHECK_FAIL, "the Core interpreter is unknown, so the server contract cannot be checked"
    try:
        completed = subprocess.run(
            [str(executable), "-I", "-B", str(script), "--code-root", str(root)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CHECK_WARN, f"could not run the check: {exc}"
    if completed.returncode == 0:
        return CHECK_OK, "Core/Functions dependencies match the approved generation"
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    summary = detail[0] if detail else "no detail reported"
    return CHECK_FAIL, summary


def _health(
    port: int,
    timeout: float = 3.0,
    expected_instance: str | None = None,
) -> tuple[str, str]:
    """Probe ``/api/health`` and confirm the answer belongs to this instance."""

    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.URLError as exc:
        return CHECK_FAIL, f"{url} unreachable: {exc.reason}"
    except (ValueError, OSError) as exc:
        return CHECK_WARN, f"{url} returned an unusable response: {exc}"
    if isinstance(payload, dict) and payload.get("ok"):
        instance = str(payload.get("instance_id") or "").strip().upper()
        if expected_instance:
            expected = str(expected_instance).strip().upper()
            if not instance:
                return CHECK_FAIL, f"{url} responded without an instance identity"
            if instance != expected:
                return CHECK_FAIL, (
                    f"{url} belongs to instance {instance}, not {expected}; "
                    "a different instance is answering on this port"
                )
        return CHECK_OK, f"{url} responded ok (instance {instance or 'unknown'})"
    return CHECK_WARN, f"{url} responded without ok=true"


def diagnose(root: Path, home: Path) -> list[dict]:
    """Return an ordered list of ``{label, status, detail}`` health checks."""

    root = Path(root)
    home = Path(home)
    checks: list[dict] = []

    def add(label: str, status: str, detail: str) -> None:
        checks.append({"label": label, "status": status, "detail": detail})

    add("python", CHECK_OK, f"TUI interpreter {sys.version.split()[0]} at {sys.executable}")

    missing = [name for name in ("textual", "rich", "aiohttp") if not _module_available(name)]
    if missing:
        add("tui packages", CHECK_FAIL, "missing: " + ", ".join(missing))
    else:
        add("tui packages", CHECK_OK, "textual, rich, aiohttp are importable")

    # The TUI interpreter and the Core runtime are separate contracts: a TUI
    # package missing here says nothing about the server, and vice versa.
    core_command, core_error = core_runtime(root)
    if core_command:
        add("server runtime", CHECK_OK, "qualified Core runtime: " + " ".join(core_command))
    else:
        add("server runtime", CHECK_FAIL, core_error)

    status, detail = _contract_status(root, core_command[0] if core_command else None)
    add("server dependency contract", status, detail)

    agents_path = home / "agents.json"
    if not agents_path.is_file():
        add("agents.json", CHECK_FAIL, f"missing at {agents_path}")
    else:
        _doc, error = _read_json(agents_path)
        if error:
            add("agents.json", CHECK_FAIL, error)
        else:
            instance_id, port, port_error = instance_identity(home)
            if port_error:
                add("agents.json", CHECK_WARN, f"{instance_id}: {port_error}")
            else:
                add("agents.json", CHECK_OK, f"instance {instance_id}, workbench port {port}")

    secrets_path = home / "secrets.json"
    if not secrets_path.is_file():
        add("secrets.json", CHECK_WARN, f"missing at {secrets_path} (agents that need credentials will fail)")
    else:
        _doc, error = _read_json(secrets_path)
        add("secrets.json", CHECK_FAIL if error else CHECK_OK, error or "present and parseable")

    instance_id, port, port_error = instance_identity(home)
    status, detail = _health(port, expected_instance=None if port_error else instance_id)
    add("local backend", status, detail)

    log_files = sorted(
        (Path(root) / "windows" / "logs").glob("*.log"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
    )
    if log_files:
        newest = log_files[-1]
        add("logs", CHECK_OK, f"{len(log_files)} launcher log(s); newest {newest.name}")
    else:
        add("logs", CHECK_WARN, "no launcher logs yet under windows/logs")

    return checks


def format_checks(checks: list[dict]) -> str:
    """Render :func:`diagnose` output as short human lines."""

    marks = {CHECK_OK: "[ ok ]", CHECK_WARN: "[warn]", CHECK_FAIL: "[fail]"}
    lines = []
    for check in checks:
        lines.append(f"{marks.get(check['status'], '[?   ]')} {check['label']}: {check['detail']}")
    failures = sum(1 for check in checks if check["status"] == CHECK_FAIL)
    warnings = sum(1 for check in checks if check["status"] == CHECK_WARN)
    lines.append("")
    if failures:
        lines.append(f"{failures} failure(s), {warnings} warning(s).")
    elif warnings:
        lines.append(f"No failures, {warnings} warning(s).")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def log_candidates(root: Path, home: Path) -> list[Path]:
    """Log files worth showing in rescue mode, newest last."""

    candidates: list[Path] = []
    candidates.extend(sorted((Path(root) / "windows" / "logs").glob("*.log")))
    mac_logs = Path(root) / "mac" / "logs"
    if mac_logs.is_dir():
        candidates.extend(sorted(mac_logs.glob("*.log")))
    for name in ("bridge.log", "core_restart.log"):
        candidate = Path(home) / "logs" / name
        if candidate.is_file():
            candidates.append(candidate)
    candidates.extend(sorted((Path(home) / "logs").glob("core_restart_*.log")))
    seen: set[Path] = set()
    unique = []
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        unique.append(candidate)
    unique.sort(key=lambda path: path.stat().st_mtime)
    return unique


def logs_tail(root: Path, home: Path, lines: int = 40) -> str:
    """Return the tail of the most relevant logs, or a plain explanation."""

    candidates = log_candidates(root, home)
    if not candidates:
        return "No launcher or bridge logs were found."
    blocks = []
    for path in candidates[-3:]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            blocks.append(f"== {path} (unreadable: {exc.strerror or exc})")
            continue
        tail = text[-lines:]
        blocks.append("== " + str(path))
        blocks.extend(tail)
    return "\n".join(blocks)


def start_backend(root: Path, home: Path, timeout: float = 40.0) -> str:
    """Start Core through the installer-qualified launcher and report what happened.

    Core is never started with the TUI interpreter: that interpreter is chosen
    for textual/rich/aiohttp and is routinely a different Python from the one
    the runtime policy allows for Core. ``run_qualified_core.py`` re-selects the
    qualified runtime, so the launch cannot bypass the Core runtime contract.
    """

    root = Path(root)
    home = Path(home)
    script = root / "scripts" / "run_qualified_core.py"
    if not script.is_file():
        return (
            "Cannot start the backend: scripts/run_qualified_core.py is missing. "
            "Core must be started through the installer-qualified launcher."
        )
    command, command_error = core_runtime(root)
    if command is None:
        return f"Cannot start the backend: {command_error}. The TUI interpreter is not used for Core."

    log_dir = home / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Cannot create the log directory {log_dir}: {exc.strerror or exc}"
    log_path = log_dir / "rescue_start.log"

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root)
    argv = [*command, str(script), "--bridge-home", str(home)]
    try:
        with log_path.open("ab") as log_handle:
            subprocess.Popen(
                argv,
                cwd=str(root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=environment,
                creationflags=creationflags,
            )
    except OSError as exc:
        return f"Cannot start the backend: {exc.strerror or exc}"

    expected_instance, port, identity_error = instance_identity(home)
    if identity_error:
        return (
            "The launcher was started, but this installation's identity is unreadable "
            f"({identity_error}), so the health check cannot prove which instance answered. "
            f"See {log_path} and /logs."
        )
    waited = 0.0
    while waited < timeout:
        status, detail = _health(port, timeout=2.0, expected_instance=expected_instance)
        if status == CHECK_OK:
            return f"Backend responded after {int(waited)}s. {detail}"
        waited += 2
        _sleep(2)
    return (
        f"Backend did not answer within {int(timeout)}s. "
        f"See {log_path} and /logs for the reason."
    )


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def setup_hint(root: Path, home: Path) -> str:
    """Explain the offline setup path without running anything."""

    lines = [
        "Offline setup",
        f"  installation: {root}",
        f"  instance home: {home}",
        "",
        " 1. Copy agents.sample.json to agents.json and fix any syntax error.",
        "    /doctor reports the exact JSON error and line.",
        " 2. Copy secrets.sample.json to secrets.json and fill in the provider key.",
        " 3. Run /start. The rescue shell waits for /api/health and reports the truth.",
        " 4. Reinstall the TUI packages if /doctor reports them missing:",
        "    python -m pip install textual rich aiohttp",
    ]
    return "\n".join(lines)


_COMMANDS = {
    "doctor": "run local environment, port, log and dependency checks",
    "logs": "show the tail of the newest launcher and bridge logs",
    "start": "start the Core bridge for this installation",
    "setup": "offline configuration and first-run guidance",
    "help": "list these commands",
    "quit": "leave the rescue shell",
}


def run(root: Path, home: Path, reason: str = "") -> int:
    """Interactive rescue loop. Returns a process exit code."""

    root, home = Path(root), Path(home)
    instance_id, port, identity_error = instance_identity(home)
    print("=" * 62)
    print(f"  {RESCUE_BANNER}")
    print(f"  instance: {instance_id}   workbench port: {port}")
    print(f"  python:   {sys.executable}")
    print("=" * 62)
    if reason:
        print(f"  TUI could not start: {reason}")
        print("  You are in the rescue shell. Your data and configuration are untouched.")
        print("=" * 62)
    if identity_error:
        print(f"  agents.json problem: {identity_error}")
    print("  Type /help for commands. Everything here works offline.")

    while True:
        try:
            raw = input("rescue> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        command = raw.replace("\ufeff", "").strip().lstrip("/").casefold()
        if not command:
            continue
        if command in {"quit", "exit", "q"}:
            return 0
        if command == "help":
            for name, description in _COMMANDS.items():
                print(f"  /{name:8s} {description}")
            continue
        if command == "doctor":
            print(format_checks(diagnose(root, home)))
            continue
        if command == "logs":
            print(logs_tail(root, home))
            continue
        if command in {"start", "restart"}:
            print(start_backend(root, home))
            continue
        if command == "setup":
            print(setup_hint(root, home))
            continue
        print(f"Unknown command: {command}. Type /help.")
# HASHI-Fingerprint: hfp_18a115c987dc454eb5d2613ad21d5744
