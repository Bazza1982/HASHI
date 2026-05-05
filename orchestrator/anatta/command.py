from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


class AnattaStatusCommand:
    def __init__(self, runtime: Any, options: dict[str, Any] | None = None):
        self.runtime = runtime
        self.options = options or {}

    async def execute(self, *, args: list[str], update: Any | None = None, context: Any | None = None) -> str:
        root = Path(self.runtime.global_config.project_root)
        script = root / "tools" / "anatta_diagnostics.py"
        cmd = [
            sys.executable,
            str(script),
            "--workspace",
            str(self.runtime.workspace_dir),
        ]
        probe_parts = list(args or [])
        if probe_parts and probe_parts[0].lower() == "full":
            cmd.append("--full")
            probe_parts = probe_parts[1:]
        if probe_parts:
            cmd.extend(["--probe", " ".join(probe_parts)])

        env = dict(os.environ)
        current_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(root) if not current_pythonpath else f"{root}{os.pathsep}{current_pythonpath}"
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        text = stdout.decode("utf-8", errors="replace").strip()
        error = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return f"Anatta diagnostics failed ({proc.returncode}): {error or text or 'no output'}"
        if error:
            return f"{text}\n\nDiagnostics warning:\n{error}".strip()
        return text


def build_workspace_command(*, runtime: Any, options: dict[str, Any] | None = None) -> AnattaStatusCommand:
    return AnattaStatusCommand(runtime, options)
