#!/usr/bin/env python3
"""Run one dual-brain turn: preflight -> right-brain -> after-action."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dual_brain_common import BackendContext, load_config, load_json, resolve_backend, write_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _call_right_brain(context: BackendContext, prompt: str, cwd: Path, timeout_s: int) -> str:
    if context.backend == "codex-cli":
        argv = [
            context.command,
            "exec",
            "--json",
            "--model",
            context.model,
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-",
        ]
        result = subprocess.run(argv, input=prompt, text=True, capture_output=True, cwd=str(cwd), timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"codex-cli failed: {result.stderr.strip()}")
        latest = ""
        for line in (result.stdout or "").splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = evt.get("item") or {}
            if evt.get("type") == "item.completed" and item.get("type") == "agent_message":
                latest = str(item.get("text") or "")
        if not latest:
            raise RuntimeError("codex-cli produced no agent_message")
        return latest

    if context.backend == "claude-cli":
        argv = [context.command, "--model", context.model, "--print"]
        result = subprocess.run(argv, input=prompt, text=True, capture_output=True, cwd=str(cwd), timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"claude-cli failed: {result.stderr.strip()}")
        return result.stdout or ""

    if context.backend == "gemini-cli":
        argv = [context.command, "--model", context.model, "--prompt", prompt]
        result = subprocess.run(argv, text=True, capture_output=True, cwd=str(cwd), timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"gemini-cli failed: {result.stderr.strip()}")
        return result.stdout or ""

    raise RuntimeError(f"unsupported backend: {context.backend}")


def _add_text_arg(
    argv: list[str],
    *,
    flag: str,
    file_flag: str,
    text: str,
    temp_dir: Path,
    inline_max_chars: int,
) -> Path | None:
    if len(text) <= inline_max_chars:
        argv.extend([flag, text])
        return None
    temp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(temp_dir),
        prefix="dual_brain_",
        suffix=".txt",
    )
    with tmp:
        tmp.write(text)
    argv.extend([file_flag, tmp.name])
    return Path(tmp.name)


def _run_preflight(
    script: Path,
    hashi_root: Path,
    config: Path,
    agent: str,
    prompt: str,
    timeout_s: int,
    temp_dir: Path,
    inline_max_chars: int,
) -> None:
    argv = [
        sys.executable,
        str(script),
        "--hashi-root",
        str(hashi_root),
        "--config",
        str(config),
        "--agent",
        agent,
        "preflight",
    ]
    tmp = _add_text_arg(
        argv,
        flag="--prompt",
        file_flag="--prompt-file",
        text=prompt,
        temp_dir=temp_dir,
        inline_max_chars=inline_max_chars,
    )
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"preflight failed: {result.stderr.strip()}\n{result.stdout.strip()}")
        print(result.stdout, end="")
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


def _run_after_action(
    script: Path,
    hashi_root: Path,
    config: Path,
    agent: str,
    prompt: str,
    result_text: str,
    timeout_s: int,
    temp_dir: Path,
    inline_max_chars: int,
) -> None:
    argv = [
        sys.executable,
        str(script),
        "--hashi-root",
        str(hashi_root),
        "--config",
        str(config),
        "--agent",
        agent,
        "after-action",
    ]
    temp_files = [
        _add_text_arg(
            argv,
            flag="--prompt",
            file_flag="--prompt-file",
            text=prompt,
            temp_dir=temp_dir,
            inline_max_chars=inline_max_chars,
        ),
        _add_text_arg(
            argv,
            flag="--result",
            file_flag="--result-file",
            text=result_text,
            temp_dir=temp_dir,
            inline_max_chars=inline_max_chars,
        ),
    ]
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"after-action failed: {result.stderr.strip()}\n{result.stdout.strip()}")
        print(result.stdout, end="")
    finally:
        for tmp in temp_files:
            if tmp and tmp.exists():
                tmp.unlink()


def _pid_is_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    except (ValueError, TypeError):
        return False
    return True


def _create_turn_lock(lock_path: Path, payload: dict[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return
    except FileExistsError:
        existing = load_json(lock_path, {})
        pid = existing.get("pid")
        started_at = existing.get("started_at", "unknown")
        if _pid_is_alive(pid):
            raise RuntimeError(f"active lock by pid={pid} started={started_at}")
        print(f"[run_dual_brain_turn] stale_lock_removed={lock_path} pid={pid} started={started_at}")
        lock_path.unlink()
    with lock_path.open("x", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one dual-brain turn sidecar")
    parser.add_argument("--hashi-root", default="/home/lily/projects/hashi")
    parser.add_argument("--config", default="/home/lily/projects/hashi/private/dual_brain_config.json")
    parser.add_argument("--agent", default="lily")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--check", action="store_true", help="diagnose only, no mutating actions")
    parser.add_argument("--right-brain-timeout-s", type=int, default=600)
    parser.add_argument("--left-brain-timeout-s", type=int, default=180)
    args = parser.parse_args()

    root = Path(args.hashi_root).resolve()
    config = Path(args.config).resolve()
    cfg = load_config(config)
    dual_script = root / "scripts/dual_brain_context.py"
    temp_dir = root / "tmp" / "dual_brain"
    prompt_inline_max_chars = int(cfg.get("prompt_inline_max_chars", 4000))

    print("[run_dual_brain_turn] mode=" + ("check" if args.check else "run"))
    print(f"[run_dual_brain_turn] hashi_root={root}")
    print(f"[run_dual_brain_turn] config_path={config}")
    print(f"[run_dual_brain_turn] dual_brain_script={dual_script}")
    print(f"[run_dual_brain_turn] agent={args.agent}")

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not prompt.strip():
        print("[run_dual_brain_turn] success=false", file=sys.stderr)
        print("[run_dual_brain_turn] error=empty prompt", file=sys.stderr)
        return 1

    right_ctx = resolve_backend(root, args.agent, cfg, role="right_brain")
    print(f"[run_dual_brain_turn] selected_backend={right_ctx.backend}")
    print(f"[run_dual_brain_turn] selected_model={right_ctx.model}")
    print(f"[run_dual_brain_turn] backend_command={right_ctx.command}")
    print(f"[run_dual_brain_turn] backend_source={right_ctx.source}")
    print(f"[run_dual_brain_turn] prompt_inline_max_chars={prompt_inline_max_chars}")

    workspace = root / "workspaces" / args.agent
    continuity_dir = workspace / "continuity"
    lock_path = continuity_dir / ".turn_active.lock"
    pending_dir = continuity_dir / "pending_updates"
    artifacts_dir = workspace / "memory" / "left_brain_artifacts"
    run_artifact = artifacts_dir / "run_dual_brain_turn_latest.json"

    print(f"[run_dual_brain_turn] lock_path={lock_path}")
    print(f"[run_dual_brain_turn] pending_dir={pending_dir}")

    if args.check:
        diag = subprocess.run(
            [
                sys.executable,
                str(dual_script),
                "--hashi-root",
                str(root),
                "--config",
                str(config),
                "--agent",
                args.agent,
                "diagnose",
            ],
            text=True,
            capture_output=True,
            timeout=60,
        )
        if diag.returncode != 0:
            print(diag.stdout, end="")
            print(diag.stderr, file=sys.stderr, end="")
            return 1
        print(diag.stdout, end="")
        print("[run_dual_brain_turn] check_only=true")
        print("[run_dual_brain_turn] success=true")
        return 0

    request_id = datetime.now(timezone.utc).strftime("req_%Y%m%dT%H%M%S%fZ")
    continuity_dir.mkdir(parents=True, exist_ok=True)
    pending_dir.mkdir(parents=True, exist_ok=True)

    lock_payload = {
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "request_id": request_id,
        "agent_id": args.agent,
        "backend": right_ctx.backend,
        "model": right_ctx.model,
    }

    right_brain_result = ""
    fyi_text = ""
    lock_acquired = False
    try:
        _create_turn_lock(lock_path, lock_payload)
        lock_acquired = True
        print(f"[run_dual_brain_turn] lock_created={lock_path}")

        print("[run_dual_brain_turn] stage=left_brain_preflight")
        _run_preflight(
            dual_script,
            root,
            config,
            args.agent,
            prompt,
            args.left_brain_timeout_s,
            temp_dir,
            prompt_inline_max_chars,
        )

        fyi_path = workspace / "memory" / "left_brain_artifacts" / "left_brain_fyi_latest.md"
        if not fyi_path.exists():
            raise RuntimeError(f"missing FYI file: {fyi_path}")
        fyi_text = fyi_path.read_text(encoding="utf-8")

        right_input = (
            f"{fyi_text}\n\n"
            "--- ORIGINAL USER PROMPT (MUST BE FOLLOWED) ---\n"
            f"{prompt}\n"
        )

        print("[run_dual_brain_turn] stage=right_brain_execution")
        right_brain_result = _call_right_brain(
            right_ctx,
            right_input,
            root,
            args.right_brain_timeout_s,
        )

        print("[run_dual_brain_turn] stage=left_brain_after_action")
        _run_after_action(
            dual_script,
            root,
            config,
            args.agent,
            prompt,
            right_brain_result,
            args.left_brain_timeout_s,
            temp_dir,
            prompt_inline_max_chars,
        )

        write_json(
            run_artifact,
            {
                "ts": _now_iso(),
                "request_id": request_id,
                "agent": args.agent,
                "backend": right_ctx.backend,
                "model": right_ctx.model,
                "prompt": prompt,
                "fyi_path": str(workspace / "memory" / "left_brain_artifacts" / "left_brain_fyi_latest.md"),
                "result_excerpt": right_brain_result[:3000],
                "success": True,
            },
        )

        print("[run_dual_brain_turn] success=true")
        print(f"[run_dual_brain_turn] run_artifact={run_artifact}")
        print("[run_dual_brain_turn] right_brain_result_start")
        print(right_brain_result)
        print("[run_dual_brain_turn] right_brain_result_end")
        return 0
    except Exception as exc:
        pending_file = pending_dir / f"{request_id}.pending.json"
        write_json(
            pending_file,
            {
                "ts": _now_iso(),
                "request_id": request_id,
                "agent": args.agent,
                "error": str(exc),
                "prompt": prompt,
                "right_brain_result_excerpt": right_brain_result[:3000],
                "fyi_excerpt": fyi_text[:3000],
            },
        )
        if right_brain_result:
            print("[run_dual_brain_turn] right_brain_result_start")
            print(right_brain_result)
            print("[run_dual_brain_turn] right_brain_result_end")
            print("[run_dual_brain_turn] success=true")
            print(f"[run_dual_brain_turn] warning=after_action_or_artifact_failed: {exc}", file=sys.stderr)
            print(f"[run_dual_brain_turn] pending_update={pending_file}", file=sys.stderr)
            return 0
        print("[run_dual_brain_turn] success=false", file=sys.stderr)
        print(f"[run_dual_brain_turn] error={exc}", file=sys.stderr)
        print(f"[run_dual_brain_turn] pending_update={pending_file}", file=sys.stderr)
        return 1
    finally:
        if lock_acquired and lock_path.exists():
            lock_path.unlink()
            print(f"[run_dual_brain_turn] lock_removed={lock_path}")


if __name__ == "__main__":
    raise SystemExit(main())
