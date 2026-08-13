from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.gateway.context import write_gateway_context
from tools.registry import ToolRegistry

from .cleanup import CleanupGuard, UnsafeCleanupTarget
from .evidence import EvidenceCollector
from .scripted_provider import EXACT_FINAL_FRAGMENTS, EXACT_REASONING_FRAGMENTS, ScriptedProvider
from .step_state import SequentialStepState, StepProtocolError


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LAB_ROOT = ROOT / "workspaces" / "ajiao" / "her_test_lab"
RUN_ID_PATTERN = re.compile(r"her-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}")


def _runtime_python() -> Path:
    candidate = ROOT / ".venv" / "bin" / "python3"
    if candidate.is_file():
        # Keep the virtual-environment entrypoint path; resolving its symlink would
        # bypass pyvenv.cfg discovery and silently fall back to the system Python.
        return candidate.absolute()
    return Path(sys.executable).resolve()


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        temporary.chmod(mode)
    temporary.replace(path)


def _command(command: list[str], cwd: Path) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_file_baseline(path: Path) -> dict[str, Any]:
    """Record local operator state without requiring or copying its contents."""
    try:
        return {"present": True, "sha256": _sha256(path)}
    except FileNotFoundError:
        return {"present": False, "sha256": None}


def _mcp_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _parse_mcp_frames(payload: bytes) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(payload):
        boundary = payload.find(b"\r\n\r\n", cursor)
        if boundary < 0:
            raise ValueError("MCP response ended inside headers")
        header = payload[cursor:boundary].decode("ascii")
        length_line = next((line for line in header.splitlines() if line.lower().startswith("content-length:")), None)
        if length_line is None:
            raise ValueError("MCP response omitted Content-Length")
        length = int(length_line.split(":", 1)[1].strip())
        start = boundary + 4
        end = start + length
        frames.append(json.loads(payload[start:end]))
        cursor = end
    return frames


@dataclass(frozen=True)
class RunLayout:
    run_id: str
    root: Path
    workspace: Path
    config_home: Path
    evidence: Path
    scratch: Path
    step_state: Path
    gateway_context: Path


class HerDebugLab:
    def __init__(self, lab_root: Path = DEFAULT_LAB_ROOT):
        self.root = Path(lab_root)
        self.runs_root = self.root / "runs"

    def initialize(self) -> None:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        marker = self.root / "LAB_ROOT.json"
        expected = {"schema_version": 1, "purpose": "isolated HER certification lab", "runs_root": str(self.runs_root.resolve())}
        if marker.exists():
            current = _json(marker)
            if current.get("purpose") != expected["purpose"]:
                raise RuntimeError("existing directory is not a HER certification lab")
        else:
            _write_json(marker, expected)

    def create_run(self, run_id: str | None = None, *, target_steps: int = 3) -> RunLayout:
        self.initialize()
        if run_id is None:
            run_id = f"her-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run id does not match the exact HER lab format")
        run_root = self.runs_root / run_id
        if run_root.exists():
            raise FileExistsError(f"run directory is non-reusable: {run_root}")
        workspace = run_root / "workspace"
        config_home = run_root / "config"
        evidence = run_root / "evidence"
        scratch = run_root / "scratch"
        for path in (workspace, config_home, evidence, scratch):
            path.mkdir(parents=True)
        layout = RunLayout(
            run_id=run_id,
            root=run_root.resolve(),
            workspace=workspace.resolve(),
            config_home=config_home.resolve(),
            evidence=evidence.resolve(),
            scratch=scratch.resolve(),
            step_state=(run_root / "step_state.json").resolve(),
            gateway_context=(config_home / "hashi_gateway_context.json").resolve(),
        )
        _write_json(
            layout.root / "cleanup_manifest.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "run_root": str(layout.root),
                "disposable_paths": ["scratch", "workspace/disposable"],
                "retained_paths": ["evidence", "baseline.json", "cleanup_manifest.json", "step_state.json"],
            },
        )
        (layout.workspace / "disposable").mkdir()
        (layout.workspace / "probe.txt").write_text("HER isolated tool gateway probe\n", encoding="utf-8")
        (layout.workspace / "unicode_fixture.txt").write_text("中文 English 🌙\n\tleading  repeated  trailing \n", encoding="utf-8")
        _command(["git", "init", "-q"], layout.workspace)
        _command(["git", "config", "user.name", "HER Debug Lab"], layout.workspace)
        _command(["git", "config", "user.email", "her-debug@invalid.local"], layout.workspace)
        _command(["git", "add", "probe.txt", "unicode_fixture.txt"], layout.workspace)
        _command(["git", "commit", "-q", "-m", "seed isolated HER fixture"], layout.workspace)

        registry = ToolRegistry(
            ["file_read", "file_write", "bash"],
            layout.root,
            layout.workspace,
            {},
            max_loops=max(130, target_steps + 10),
            audit_context={"agent_name": "ajiao", "safety_mode": "isolated_danger_full_access", "run_id": run_id},
        )
        write_gateway_context(registry, layout.gateway_context)
        SequentialStepState.create(layout.step_state, target_steps=target_steps, seed=run_id)
        _write_json(
            layout.config_home / "settings.json",
            {
                "mcpServers": {
                    "hashi-tools": {
                        "command": str(_runtime_python()),
                        "args": ["-m", "tools.gateway.mcp_stdio", "--context", str(layout.gateway_context)],
                        "env": {"PYTHONPATH": str(ROOT)},
                        "required": True,
                        "toolCallTimeoutMs": 120000,
                    },
                    "her-step-lab": {
                        "command": str(_runtime_python()),
                        "args": ["-m", "tools.her_debug.mcp_step_server", "--state", str(layout.step_state)],
                        "env": {"PYTHONPATH": str(ROOT)},
                        "required": True,
                        "toolCallTimeoutMs": 120000,
                    },
                }
            },
        )
        baseline = self.capture_baseline(layout)
        _write_json(layout.root / "baseline.json", baseline)
        EvidenceCollector(layout.evidence).write_json("lab_created.json", {"run_id": run_id, "baseline": baseline})
        return layout

    def capture_baseline(self, layout: RunLayout) -> dict[str, Any]:
        manifest = _json(ROOT / "hashi_assets" / "her" / "manifest.json")
        binary = ROOT / "hashi_assets" / "her" / manifest["binaries"]["linux-x86_64"]["path"]
        hashi_commit = _command(["git", "rev-parse", "HEAD"], ROOT)
        hashi_dirty = bool(_command(["git", "status", "--porcelain"], ROOT))
        configured_source = str(os.environ.get("HASHI_HER_SOURCE_ROOT") or "").strip()
        source_root = Path(configured_source).expanduser() if configured_source else None
        source_checkout = bool(source_root and (source_root / ".git").exists())
        source_commit = (
            _command(["git", "rev-parse", "HEAD"], source_root)
            if source_checkout and source_root is not None
            else manifest.get("source_commit")
        )
        source_dirty = (
            bool(_command(["git", "status", "--porcelain"], source_root))
            if source_checkout and source_root is not None
            else None
        )
        ajiao_state = _optional_file_baseline(ROOT / "workspaces" / "ajiao" / "state.json")
        ajiao_preferences = _optional_file_baseline(
            ROOT / "workspaces" / "ajiao" / "state" / "runtime_preferences.json"
        )
        config_path = ROOT / "agents.json"
        config = _json(config_path) if config_path.is_file() else {}
        provider_rows = {}
        for name, row in (config.get("global", {}).get("claw_providers", {}).get("providers", {}) or {}).items():
            provider_rows[name] = {"base_url": row.get("base_url"), "status": row.get("status")}
        binary_hash = _sha256(binary)
        candidate_material = json.dumps(
            {"hashi_commit": hashi_commit, "source_commit": source_commit, "package_sha256": binary_hash},
            sort_keys=True,
        ).encode("utf-8")
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "run_id": layout.run_id,
            "hashi_commit": hashi_commit,
            "hashi_dirty": hashi_dirty,
            "python_version": sys.version.split()[0],
            "her_version": manifest.get("version"),
            "her_source_commit": source_commit,
            "her_source_dirty": source_dirty,
            "package_sha256": binary_hash,
            "manifest_package_sha256": manifest["binaries"]["linux-x86_64"]["sha256"],
            "candidate_hash": hashlib.sha256(candidate_material).hexdigest(),
            "providers": provider_rows,
            "agents_config_present": config_path.is_file(),
            "ajiao_baseline": {"state": ajiao_state, "runtime_preferences": ajiao_preferences},
            "permission_overlay": {
                "maximum": "danger-full-access",
                "route": "danger-full-access",
                "skip_permission_prompts": True,
                "access_root": str(layout.root),
                "external_mutation_tools": False,
            },
        }

    def self_test(self) -> dict[str, Any]:
        layout = self.create_run(target_steps=2)
        checks: dict[str, Any] = {}
        state = SequentialStepState(layout.step_state)
        first = state.expected_token()
        checks["step_1"] = state.accept(str(first))
        try:
            state.accept(str(first))
            checks["repeat_rejected"] = False
        except StepProtocolError:
            checks["repeat_rejected"] = True
        second = state.expected_token()
        checks["step_2"] = state.accept(str(second))

        guard = CleanupGuard(self.root)
        try:
            guard.validate_run_root(self.root)
            checks["broad_delete_rejected"] = False
        except UnsafeCleanupTarget:
            checks["broad_delete_rejected"] = True
        disposable = layout.workspace / "disposable" / "temporary.txt"
        disposable.write_text("temporary", encoding="utf-8")
        checks["deleted"] = guard.delete_disposable(layout.root, ["workspace/disposable"])
        checks["evidence_retained"] = layout.evidence.is_dir()
        checks["gateway_context_mode"] = oct(layout.gateway_context.stat().st_mode & 0o777)
        settings = _json(layout.config_home / "settings.json")
        mcp_checks: dict[str, Any] = {}
        request_bytes = b"".join(
            (
                _mcp_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "her-debug", "version": "1"}},
                    }
                ),
                _mcp_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            )
        )
        for server_name, server in settings["mcpServers"].items():
            process_env = dict(os.environ)
            process_env.update({str(key): str(value) for key, value in (server.get("env") or {}).items()})
            completed = subprocess.run(
                [str(server["command"]), *[str(item) for item in server.get("args", [])]],
                input=request_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                timeout=15,
                check=False,
            )
            frames = _parse_mcp_frames(completed.stdout) if completed.stdout else []
            tools = [
                item.get("name")
                for item in (frames[-1].get("result", {}).get("tools", []) if frames else [])
                if isinstance(item, dict)
            ]
            mcp_checks[server_name] = {
                "returncode": completed.returncode,
                "response_count": len(frames),
                "tools": tools,
                "stderr_empty": not bool(completed.stderr.strip()),
            }
        checks["mcp_servers"] = mcp_checks
        collector = EvidenceCollector(layout.evidence, forbidden_values=["HER_DEBUG_FORBIDDEN_SELF_TEST"])
        collector.write_json("self_test.json", checks)
        scan = collector.scan()
        ok = all(
            (
                checks["repeat_rejected"],
                checks["broad_delete_rejected"],
                checks["evidence_retained"],
                checks["gateway_context_mode"] == "0o600",
                set(checks["mcp_servers"]) == {"hashi-tools", "her-step-lab"},
                all(item["returncode"] == 0 and item["response_count"] == 2 and item["tools"] for item in checks["mcp_servers"].values()),
                scan["ok"],
            )
        )
        collector.finalize(verdict="PASS" if ok else "FAIL", checks=checks)
        return {"ok": ok, "run_id": layout.run_id, "run_root": str(layout.root), "checks": checks, "scan": scan}

    def run_scenario(
        self,
        scenario: str,
        *,
        binary: Path | None = None,
        target_steps: int = 3,
        timeout_seconds: int = 45,
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        layout = self.create_run(target_steps=target_steps)
        manifest = _json(ROOT / "hashi_assets" / "her" / "manifest.json")
        if binary is None:
            binary = ROOT / "hashi_assets" / "her" / manifest["binaries"]["linux-x86_64"]["path"]
        binary = Path(binary).resolve()
        if not binary.is_file():
            raise FileNotFoundError(f"HER candidate binary not found: {binary}")
        expected_hash = manifest["binaries"]["linux-x86_64"]["sha256"]
        actual_hash = _sha256(binary)
        if actual_hash != expected_hash:
            raise RuntimeError("HER candidate binary does not match the active manifest")

        private_canary = f"HER_DEBUG_PRIVATE_CANARY_{layout.run_id}"
        collector = EvidenceCollector(layout.evidence, forbidden_values=[private_canary])
        home = layout.root / "home"
        home.mkdir()
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "CLAW_CONFIG_HOME": str(layout.config_home),
            "OPENAI_API_KEY": "local-fixture-key",
            "NO_COLOR": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONPATH": str(ROOT),
        }
        if max_iterations is not None:
            environment["CLAW_MAX_TOOL_ITERATIONS"] = str(max_iterations)
        command = [
            str(binary),
            "--model",
            "openai/deepseek-v4-flash",
            "--permission-mode",
            "danger-full-access",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "prompt",
            f"Execute the local scripted certification scenario. Private marker: {private_canary}",
        ]
        started_at = datetime.now(timezone.utc)
        timed_out = False
        provider_scenario = "sequential_steps" if scenario == "iteration_ceiling" else scenario
        with ScriptedProvider(
            scenario=provider_scenario,
            step_state=(
                SequentialStepState(layout.step_state)
                if scenario in {"sequential_steps", "iteration_ceiling"}
                else None
            ),
            delay_seconds=1.5 if scenario == "delayed_response_once" else 0.0,
        ) as provider:
            environment["OPENAI_BASE_URL"] = provider.base_url
            if scenario == "delayed_response_once":
                environment["CLAW_API_REQUEST_TIMEOUT"] = "1"
            try:
                completed = subprocess.run(
                    command,
                    cwd=layout.workspace,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout_seconds,
                    check=False,
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                returncode = None
                stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            requests = provider.sanitized_requests()
            expected_disconnects = provider.expected_disconnects

        events: list[dict[str, Any]] = []
        invalid_jsonl_lines = 0
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_jsonl_lines += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                invalid_jsonl_lines += 1
        kinds = [str(event.get("kind")) for event in events]
        terminals = [event for event in events if event.get("kind") == "run_finished"]
        terminal = terminals[-1] if terminals else {}
        step_payload = SequentialStepState(layout.step_state).load()
        step_events = step_payload.get("events", [])
        ordered_steps = [item.get("step") for item in step_events if isinstance(item, dict)]
        token_hashes = [item.get("token_sha256") for item in step_events if isinstance(item, dict)]
        persistent_http_error = re.fullmatch(r"http_\d+", scenario) is not None
        error_scenarios = {"malformed_sse", "truncated_sse"}
        expects_error = persistent_http_error or scenario in error_scenarios
        exit_ok = returncode not in (None, 0) if expects_error else returncode == 0
        completion_ok = (
            terminal.get("completion_status") == "error"
            if expects_error
            else terminal.get("completion_status") in {"completed", "incomplete"}
        )
        sequential_ok = scenario != "sequential_steps" or all(
            (
                int(step_payload["accepted_steps"]) == target_steps,
                len(step_events) == target_steps,
                ordered_steps == list(range(1, target_steps + 1)),
                len(set(token_hashes)) == target_steps,
                kinds.count("tool_call") == target_steps,
                kinds.count("tool_start") == target_steps,
                kinds.count("tool_end") == target_steps,
            )
        )
        thinking_text = "".join(str(event.get("text", "")) for event in events if event.get("kind") == "thinking_delta")
        assistant_text = "".join(str(event.get("text", "")) for event in events if event.get("kind") == "assistant_delta")
        exact_stream_ok = scenario != "exact_stream" or (
            thinking_text == "".join(EXACT_REASONING_FRAGMENTS)
            and assistant_text == "".join(EXACT_FINAL_FRAGMENTS)
            and terminal.get("message") == "".join(EXACT_FINAL_FRAGMENTS)
        )
        finalization_ok = scenario != "thinking_then_final" or (
            len(requests) == 2
            and terminal.get("completion_status") == "completed"
            and terminal.get("message") == "VISIBLE_FINAL_OK"
        )
        no_final_ok = scenario != "repeated_thinking_only" or (
            len(requests) == 2
            and terminal.get("completion_status") == "incomplete"
            and terminal.get("stop_reason") == "no_final_text"
        )
        protocol_error_ok = scenario not in {"malformed_sse", "truncated_sse"} or (
            terminal.get("error_kind") == "stream_protocol_error"
            and terminal.get("last_safe_event") == "run_started"
            and terminal.get("message") == "Provider stream protocol failed after the last safe event."
        )
        auth_error_ok = scenario not in {"http_401", "http_403"} or terminal.get("error_kind") == "api_auth_error"
        rate_limit_ok = scenario != "http_429" or terminal.get("error_kind") == "api_rate_limit_error"
        delayed_response_ok = scenario != "delayed_response_once" or (
            len(requests) == 2 and expected_disconnects == 1 and terminal.get("completion_status") == "completed"
        )
        iteration_ceiling_ok = scenario != "iteration_ceiling" or (
            max_iterations is not None
            and terminal.get("completion_status") == "incomplete"
            and terminal.get("stop_reason") == "max_iterations"
            and terminal.get("iterations") == max_iterations
            and len(requests) == max_iterations
            and int(step_payload["accepted_steps"]) == max_iterations - 1
            and ordered_steps == list(range(1, max_iterations))
            and len(set(token_hashes)) == max_iterations - 1
            and kinds.count("tool_call") == max_iterations - 1
            and kinds.count("tool_start") == max_iterations - 1
            and kinds.count("tool_end") == max_iterations - 1
        )
        checks = {
            "scenario": scenario,
            "binary_sha256": actual_hash,
            "configured_max_iterations": max_iterations,
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_seconds": round((datetime.now(timezone.utc) - started_at).total_seconds(), 3),
            "event_kinds": kinds,
            "run_started_count": kinds.count("run_started"),
            "run_finished_count": len(terminals),
            "terminal_is_last": bool(kinds) and kinds[-1] == "run_finished",
            "completion_status": terminal.get("completion_status"),
            "stop_reason": terminal.get("stop_reason"),
            "iterations": terminal.get("iterations"),
            "error_kind": terminal.get("error_kind"),
            "last_safe_event": terminal.get("last_safe_event"),
            "invalid_jsonl_lines": invalid_jsonl_lines,
            "provider_request_count": len(requests),
            "fixture_expected_disconnects": expected_disconnects,
            "thinking_text_exact": exact_stream_ok,
            "assistant_text": assistant_text,
            "private_canary_absent": private_canary not in stdout and private_canary not in stderr,
            "sequential_steps": {
                "accepted": step_payload["accepted_steps"],
                "target": step_payload["target_steps"],
                "event_count": len(step_events),
                "ordered_steps": ordered_steps,
                "unique_token_hashes": len(set(token_hashes)),
            },
        }
        passed = all(
            (
                not timed_out,
                exit_ok,
                kinds.count("run_started") == 1,
                len(terminals) == 1,
                checks["terminal_is_last"],
                invalid_jsonl_lines == 0,
                completion_ok,
                checks["private_canary_absent"],
                sequential_ok,
                exact_stream_ok,
                finalization_ok,
                no_final_ok,
                protocol_error_ok,
                auth_error_ok,
                rate_limit_ok,
                delayed_response_ok,
                iteration_ceiling_ok,
            )
        )
        collector.write_text("stdout.jsonl", stdout)
        collector.write_text("stderr.txt", stderr)
        collector.write_json("provider_requests.json", requests)
        collector.write_json("checks.json", checks)
        collector.finalize(verdict="PASS" if passed else "FAIL", checks=checks)
        return {
            "ok": passed,
            "run_id": layout.run_id,
            "run_root": str(layout.root),
            "checks": checks,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and exercise the isolated HER certification lab")
    parser.add_argument("command", choices=("create", "self-test", "run"))
    parser.add_argument("--lab-root", type=Path, default=DEFAULT_LAB_ROOT)
    parser.add_argument("--target-steps", type=int, default=3)
    parser.add_argument("--scenario", default="exact_stream")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--max-iterations", type=int)
    args = parser.parse_args(argv)
    lab = HerDebugLab(args.lab_root)
    try:
        if args.command == "create":
            layout = lab.create_run(target_steps=args.target_steps)
            result = {"ok": True, "run_id": layout.run_id, "run_root": str(layout.root)}
        elif args.command == "self-test":
            result = lab.self_test()
        else:
            result = lab.run_scenario(
                args.scenario,
                binary=args.binary,
                target_steps=args.target_steps,
                timeout_seconds=args.timeout_seconds,
                max_iterations=args.max_iterations,
            )
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
