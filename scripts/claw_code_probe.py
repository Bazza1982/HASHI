#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.claw_cli import (
    ClawError,
    OLLAMA_DUMMY_API_KEY,
    find_claw_binary,
    run_claw_doctor,
    run_claw_state,
    run_claw_status,
    run_claw_task,
    run_claw_version,
)


def _emit(payload: dict, *, pretty: bool) -> None:
    print(json.dumps(payload, indent=2 if pretty else None, sort_keys=pretty))


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _provider_env(*, config_path: Path, secrets_path: Path, provider_name: str) -> tuple[dict[str, str], dict]:
    raw = _load_json(config_path)
    claw_cfg = raw.get("global", {}).get("claw_providers", {})
    providers = claw_cfg.get("providers") or {}
    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        raise KeyError(f"Claw provider is not configured: {provider_name}")

    base_url = str(provider.get("base_url") or "").strip()
    if not base_url:
        raise KeyError(f"Claw provider {provider_name} has no base_url")

    secret_name = provider.get("secret")
    api_key = None
    if secret_name:
        secrets = _load_json(secrets_path) if secrets_path.exists() else {}
        api_key = secrets.get(str(secret_name))
        if not api_key:
            raise KeyError(f"Claw provider {provider_name} requires missing secret: {secret_name}")
    else:
        api_key = provider.get("dummy_api_key") or OLLAMA_DUMMY_API_KEY

    env = {key: value for key, value in os.environ.items() if key in {"HOME", "PATH", "TMPDIR", "TEMP", "USER"}}
    env["OPENAI_BASE_URL"] = base_url
    if api_key:
        env["OPENAI_API_KEY"] = str(api_key)
    return env, provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe a configured Claw Code binary.")
    parser.add_argument("--binary", help="Path to the claw executable. Falls back to CLAW_BINARY/CLAW_BIN/PATH.")
    parser.add_argument("--cwd", default=".", help="Workspace directory to run diagnostics in.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout in seconds per command.")
    parser.add_argument("--config", default=str(ROOT / "agents.json"), help="HASHI agents.json path for provider probes.")
    parser.add_argument("--secrets", default=str(ROOT / "secrets.json"), help="HASHI secrets.json path for provider probes.")
    parser.add_argument("--provider", help="Run a provider-aware smoke test using global.claw_providers.")
    parser.add_argument("--model", help="Model to use with --provider.")
    parser.add_argument("--prompt", default="Reply exactly: HASHI_CLAW_SMOKE_OK", help="Prompt for --provider smoke.")
    parser.add_argument(
        "--check",
        choices=("version", "doctor", "status", "state", "all"),
        default="all",
        help="Diagnostic command to run.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        binary = find_claw_binary(args.binary)
        if args.provider:
            if not args.model:
                raise ValueError("--model is required with --provider")
            env, provider = _provider_env(
                config_path=Path(args.config).expanduser().resolve(),
                secrets_path=Path(args.secrets).expanduser().resolve(),
                provider_name=args.provider,
            )
            result = run_claw_task(
                cwd,
                args.prompt,
                args.model,
                permission_mode="read-only",
                binary_path=binary,
                env=env,
                timeout_s=args.timeout,
            )
            marker = "HASHI_CLAW_SMOKE_OK" in result.text
            _emit(
                {
                    "ok": marker,
                    "binary": str(binary),
                    "cwd": str(cwd),
                    "provider": args.provider,
                    "provider_status": provider.get("status", "stable"),
                    "base_url": provider.get("base_url"),
                    "model": result.model,
                    "message": result.text,
                    "duration_ms": result.duration_ms,
                    "iterations": result.iterations,
                    "tool_uses": len(result.tool_uses),
                    "marker_found": marker,
                },
                pretty=args.pretty,
            )
            return 0 if marker else 1

        checks = ["version", "doctor", "status", "state"] if args.check == "all" else [args.check]
        results = {}
        ok = True
        for check in checks:
            try:
                if check == "version":
                    data = run_claw_version(cwd, binary_path=binary, timeout_s=args.timeout)
                elif check == "doctor":
                    data = run_claw_doctor(cwd, binary_path=binary, timeout_s=args.timeout)
                elif check == "status":
                    data = run_claw_status(cwd, binary_path=binary, timeout_s=args.timeout)
                elif check == "state":
                    data = run_claw_state(cwd, binary_path=binary, timeout_s=args.timeout)
                else:
                    raise AssertionError(f"unhandled check: {check}")
                results[check] = {"ok": True, "data": data}
            except ClawError as exc:
                ok = False
                results[check] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
        _emit({"ok": ok, "binary": str(binary), "cwd": str(cwd), "checks": results}, pretty=args.pretty)
        return 0 if ok else 1
    except (KeyError, ValueError) as exc:
        _emit({"ok": False, "error_type": type(exc).__name__, "error": str(exc), "cwd": str(cwd)}, pretty=args.pretty)
        return 1
    except ClawError as exc:
        _emit({"ok": False, "error_type": type(exc).__name__, "error": str(exc), "cwd": str(cwd)}, pretty=args.pretty)
        return 1


if __name__ == "__main__":
    sys.exit(main())
