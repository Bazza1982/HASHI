"""
Deterministic step handler used for smoke tests and packaging verification.
"""

from __future__ import annotations

import json
from pathlib import Path


class DeterministicStepHandler:
    """
    Create predictable local artifacts without invoking an external model CLI.

    This is intentionally narrow in scope: it exists to make package, CLI, and
    fixture smoke tests runnable in clean environments.
    """

    def __init__(self, *, runs_root: str | Path = "flow/runs") -> None:
        self.runs_root = Path(runs_root)

    def execute(
        self,
        agent_id: str,
        task_message: dict,
        agent_md_path: str,
        timeout_seconds: int = 600,
        backend: str = "claude-cli",
        model: str = "",
    ) -> dict:
        del agent_md_path, timeout_seconds, backend, model

        payload = task_message.get("payload", {})
        run_id = task_message["run_id"]
        step_id = payload["step_id"]
        worker_dir = self.runs_root / run_id / "deterministic-workers" / agent_id
        worker_dir.mkdir(parents=True, exist_ok=True)

        produced: dict[str, str] = {}
        input_artifacts = payload.get("input_artifacts", {})
        output_spec = payload.get("output_spec", [])

        for artifact in output_spec:
            key = artifact["key"]
            relative_path = artifact["path"]
            output_path = worker_dir / relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_artifact(
                output_path,
                step_id=step_id,
                artifact_key=key,
                input_artifacts=input_artifacts,
                params=payload.get("params", {}),
            )
            produced[key] = str(output_path)

        return {
            "status": "completed",
            "artifacts_produced": produced,
            "summary": f"deterministic smoke output for {step_id}",
        }

    def _write_artifact(
        self,
        output_path: Path,
        *,
        step_id: str,
        artifact_key: str,
        input_artifacts: dict,
        params: dict,
    ) -> None:
        suffix = output_path.suffix.lower()
        if suffix == ".json":
            payload = {
                "step_id": step_id,
                "artifact_key": artifact_key,
                "input_artifacts": input_artifacts,
                "params": params,
                "mode": "deterministic-smoke",
            }
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return

        lines = [
            f"step_id: {step_id}",
            f"artifact_key: {artifact_key}",
            "mode: deterministic-smoke",
        ]
        if input_artifacts:
            lines.append("input_artifacts:")
            for key, value in sorted(input_artifacts.items()):
                lines.append(f"- {key}: {value}")
        if params:
            lines.append("params:")
            for key, value in sorted(params.items()):
                lines.append(f"- {key}: {value}")
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
