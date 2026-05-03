"""Lily-owned CLI backend client for wiki classification."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import WikiConfig


Runner = Callable[..., subprocess.CompletedProcess[str]]


class BackendPolicyError(RuntimeError):
    """Raised when the wiki pipeline would violate backend policy."""


@dataclass(frozen=True)
class BackendContext:
    backend: str
    model: str
    command: str


@dataclass(frozen=True)
class BackendCallResult:
    text: str
    backend: str
    model: str
    returncode: int


def resolve_backend_context(config: WikiConfig) -> BackendContext:
    state = _read_json(config.hashi_root / "workspaces/lily/state.json", {})
    backend = str(state.get("active_backend") or "").strip()
    model = str(state.get("active_model") or "").strip()
    if not backend or not model:
        raise BackendPolicyError("Cannot resolve Lily active_backend/active_model from state.json")
    if backend not in config.approved_cli_backends:
        raise BackendPolicyError(
            f"Wiki pipeline only allows Lily-owned CLI/local backends; got {backend}"
        )
    command = _command_for_backend(config, backend)
    return BackendContext(backend=backend, model=model, command=command)


def call_lily_cli_backend(
    prompt: str,
    config: WikiConfig,
    *,
    runner: Runner = subprocess.run,
    timeout_s: int = 120,
) -> BackendCallResult:
    context = resolve_backend_context(config)
    argv = _argv_for_context(context, prompt)
    result = runner(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=str(config.hashi_root),
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"{context.backend} failed with code {result.returncode}: {stderr}")
    return BackendCallResult(
        text=result.stdout or "",
        backend=context.backend,
        model=context.model,
        returncode=result.returncode,
    )


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _command_for_backend(config: WikiConfig, backend: str) -> str:
    agents = _read_json(config.hashi_root / "agents.json", {})
    global_cfg = agents.get("global") or {}
    if backend == "claude-cli":
        return str(global_cfg.get("claude_cmd") or os.environ.get("CLAUDE_BIN") or "claude")
    if backend == "gemini-cli":
        return str(global_cfg.get("gemini_cmd") or os.environ.get("GEMINI_BIN") or "gemini")
    raise BackendPolicyError(f"Unsupported CLI backend for wiki pipeline: {backend}")


def _argv_for_context(context: BackendContext, prompt: str) -> list[str]:
    if context.backend == "claude-cli":
        return [context.command, "--model", context.model, "--print", prompt]
    if context.backend == "gemini-cli":
        return [context.command, "--model", context.model, "--prompt", prompt]
    raise BackendPolicyError(f"Unsupported CLI backend for wiki pipeline: {context.backend}")
