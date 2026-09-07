"""
Nagare subprocess-backed step handler.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nagare.logging.events import RunEventLogger
from nagare.paths import resolve_relative_path, validate_path_component


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubprocessStepHandler:
    """
    本地 worker 任务调度器。
    每次调用 dispatch() 都会：
    1. 将任务写入 worker inbox
    2. 以子进程方式运行 claude CLI（--print 模式，非交互）
    3. 解析 JSON 输出，写入 outbox
    4. 返回结构化结果

    Worker 的 AGENT.md 作为 system prompt 传入 claude CLI。
    """

    def __init__(
        self,
        run_id: str,
        workers_base: Optional[Path] = None,
        event_logger: RunEventLogger | None = None,
        repo_root: str | Path | None = None,
        runs_root: str | Path = "flow/runs",
    ):
        self.run_id = run_id
        self.repo_root = Path(repo_root or Path.cwd())
        self.runs_root = Path(runs_root)
        self.workers_base = workers_base or (self.runs_root / run_id / "workers")
        self.logger = logging.getLogger(f"nagare.dispatcher.{run_id}")
        self.event_logger = event_logger
        self._correlation_lock = threading.Lock()
        self._step_id_by_task: dict[str, str] = {}

    def execute(self, agent_id: str, task_message: dict, agent_md_path: str,
                backend: str = "claude-cli", model: str = "") -> dict:
        """
        调度任务给指定 worker。

        Args:
            agent_id: worker 标识符
            task_message: 符合 Nagare StepHandler 协议的任务消息
            agent_md_path: worker AGENT.md 文件路径（相对于 hashi root）
            backend: 后端类型 ("claude-cli" 或 "codex-cli")
            model: 模型标识符（如 "claude-opus-4-6", "gpt-5.4"）

        Returns:
            {"status": "completed"|"failed", "artifacts_produced": {}, "summary": "", ...}
        """
        agent_id = validate_path_component(agent_id, label="agent_id")
        task_id = validate_path_component(
            task_message.get("task_id", f"task-{agent_id}-{int(time.time())}"),
            label="task_id",
        )
        step_id = validate_path_component(
            task_message.get("payload", {}).get("step_id"),
            label="step_id",
        )
        with self._correlation_lock:
            self._step_id_by_task[task_id] = step_id
        worker_dir = self.workers_base / agent_id
        inbox_dir = worker_dir / "inbox"
        outbox_dir = worker_dir / "outbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        outbox_dir.mkdir(parents=True, exist_ok=True)

        # 写入 inbox 任务文件
        inbox_file = inbox_dir / f"{task_id}.json"
        inbox_file.write_text(
            json.dumps(task_message, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        self.logger.info(f"[Dispatch] {agent_id} ← {task_id} (inbox written)")

        # 解析 AGENT.md system prompt
        agent_md_full = Path(agent_md_path)
        if not agent_md_full.is_absolute():
            agent_md_full = self.repo_root / agent_md_full
        system_prompt = self._load_agent_md(agent_md_full)

        # 构建发给 worker 的完整 prompt
        user_prompt = self._build_worker_prompt(task_message, worker_dir)

        # 运行 CLI（根据 backend 选择 claude 或 codex）
        result = self._run_cli(
            agent_id=agent_id,
            task_id=task_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            worker_dir=worker_dir,
            backend=backend,
            model=model,
        )

        # 写入 outbox 结果文件
        outbox_file = outbox_dir / f"{task_id}_result.json"
        outbox_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        self.logger.info(f"[Dispatch] {agent_id} → {task_id} status={result['status']}")

        return result

    dispatch = execute

    # =========================================================================
    # 内部方法
    # =========================================================================

    def _load_agent_md(self, agent_md_path: Path) -> str:
        if agent_md_path.exists():
            return agent_md_path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Worker AGENT.md does not exist: {agent_md_path}")

    def _build_worker_prompt(self, task_message: dict, worker_dir: Path) -> str:
        """将 task_assign 消息转为 worker 可执行的 prompt"""
        payload = task_message.get("payload", {})
        prompt = payload.get("prompt", "")
        input_artifacts = payload.get("input_artifacts", {})
        params = payload.get("params", {})

        lines = [
            "# Task Assignment",
            "",
            f"**Task ID**: {task_message.get('task_id', 'unknown')}",
            f"**Workflow**: {task_message.get('workflow_id', 'unknown')}",
            f"**Run**: {task_message.get('run_id', 'unknown')}",
            "",
            "## Your Task",
            "",
            prompt,
        ]

        if input_artifacts:
            lines += ["", "## Input Artifacts", ""]
            for key, path in input_artifacts.items():
                lines.append(f"- **{key}**: `{path}`")

        if params:
            lines += ["", "## Parameters", ""]
            for k, v in params.items():
                lines.append(f"- **{k}**: {v}")

        lines += [
            "",
            "## CRITICAL: Output File Instructions",
            "",
            f"Your working directory is: `{worker_dir}`",
            "",
            "**You MUST physically create output files using your Write tool or Bash tool.**",
            "Do NOT just describe file contents in your response — actually write the files to disk.",
            "",
            "Example of CORRECT behavior:",
            "1. Use the Write tool to write content to a file, e.g.:",
            f"   Write file: `{worker_dir}/output.json`",
            "2. Verify the file exists with `ls` if needed.",
            "3. Then output the result JSON below.",
            "",
            "**After writing all files**, output a JSON result block as the LAST thing in your response:",
            "```json",
            json.dumps({
                "status": "completed",
                "artifacts_produced": {"<key>": "<relative_filename_only>"},
                "summary": "<brief summary>",
                "quality_notes": ""
            }, ensure_ascii=False, indent=2),
            "```",
            "",
            "Use relative filenames (not absolute paths) in artifacts_produced.",
            "If something fails, use `\"status\": \"failed\"` with `\"error_message\"` and `\"suggested_fix\"`.",
        ]

        return "\n".join(lines)

    def _run_cli(self, agent_id: str, task_id: str, system_prompt: str,
                 user_prompt: str, worker_dir: Path,
                 backend: str = "claude-cli", model: str = "") -> dict:
        """调用 CLI 执行任务，根据 backend 选择 claude 或 codex，解析结果"""
        log_dir = worker_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_id}.log"
        started_at = time.time()
        if self.event_logger:
            with self._correlation_lock:
                step_id = self._step_id_by_task.get(task_id, task_id)
            self.event_logger.emit(
                "handler.invoke.started",
                component="engine.dispatcher",
                request_id=task_id,
                step_id=step_id,
                message=f"Invoking worker handler for {agent_id}",
                data={"agent_id": agent_id, "backend": backend, "model": model},
            )

        # 根据 backend 构建命令
        if backend == "codex-cli":
            # Codex CLI（OpenAI）— 使用 exec 子命令非交互运行
            cmd = ["codex", "exec"]
            if model:
                cmd.extend(["--model", model])
            cmd.append("--full-auto")
            # codex exec 用 prompt 作为最后一个参数，合并 system + user prompt
            full_prompt = f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nTASK:\n{user_prompt}"
            cmd.append(full_prompt)
        elif backend == "claude-cli":
            # Claude CLI（默认）
            cmd = ["claude", "--print", "--dangerously-skip-permissions"]
            if model:
                cmd.extend(["--model", model])
            cmd.extend(["--system-prompt", system_prompt, user_prompt])
        else:
            result = {
                "status": "failed",
                "error_type": "unsupported_backend",
                "error_message": (
                    f"Unsupported subprocess backend '{backend}'. "
                    "Supported values are 'claude-cli' and 'codex-cli'."
                ),
                "ts": utc_now(),
            }
            self._emit_handler_result(agent_id, task_id, backend, model, started_at, result)
            return result

        self.logger.info(f"[{backend}] 启动 worker {agent_id}, task={task_id}, model={model}")

        # 移除嵌套会话检测变量（允许在 Claude Code 会话内启动子进程）
        child_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        child_env["CLAUDE_FLOW_WORKER"] = agent_id
        child_env["CLAUDE_FLOW_RUN"] = self.run_id

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(worker_dir),
                env=child_env,
            )
            cancelled = False
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if (self.runs_root / self.run_id / "_stop").exists():
                        cancelled = True
                        try:
                            process.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            stdout, stderr = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            stdout, stderr = process.communicate()
                        break

            stdout = stdout or ""
            stderr = stderr or ""

            # 记录日志
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n")

            if cancelled:
                result = {
                    "status": "failed",
                    "error_type": "cancelled",
                    "error_message": "Worker stopped by the workflow _stop signal",
                    "raw_output": stdout[:2000],
                    "ts": utc_now(),
                }
                self._emit_handler_result(agent_id, task_id, backend, model, started_at, result)
                return result

            if process.returncode != 0:
                result = {
                    "status": "failed",
                    "error_type": "cli_error",
                    "error_message": f"{backend} exited with code {process.returncode}: {stderr[:500]}",
                    "suggested_fix": f"检查 {backend} 是否安装，以及 model 是否可访问",
                    "raw_output": stdout[:2000],
                    "ts": utc_now(),
                }
                self._emit_handler_result(agent_id, task_id, backend, model, started_at, result)
                return result

            # 提取 JSON 结果块（最后一个 ```json ... ``` 块）
            parsed = self._extract_json_result(stdout)
            if isinstance(parsed, dict):
                status = parsed.get("status")
                if status not in {"completed", "failed", "recovered", "unrecoverable"}:
                    result = {
                        "status": "failed",
                        "error_type": "invalid_worker_output",
                        "error_message": (
                            "Worker JSON must declare status as completed, failed, "
                            "recovered, or unrecoverable"
                        ),
                        "raw_output": stdout[:2000],
                        "ts": utc_now(),
                    }
                    self._emit_handler_result(
                        agent_id, task_id, backend, model, started_at, result
                    )
                    return result
                artifacts = parsed.get("artifacts_produced", {})
                if not isinstance(artifacts, dict):
                    result = {
                        "status": "failed",
                        "error_type": "invalid_worker_output",
                        "error_message": "Worker artifacts_produced must be a mapping",
                        "raw_output": stdout[:2000],
                        "ts": utc_now(),
                    }
                    self._emit_handler_result(
                        agent_id, task_id, backend, model, started_at, result
                    )
                    return result
                parsed["ts"] = utc_now()
                parsed["raw_output_preview"] = stdout[:500]
                # 展开 artifacts 相对路径为绝对路径（支持单文件和文件列表）
                try:
                    resolved = {
                        key: self._resolve_artifact_value(worker_dir, key, value)
                        for key, value in artifacts.items()
                    }
                except ValueError as exc:
                    result = {
                        "status": "failed",
                        "error_type": "invalid_worker_output",
                        "error_message": str(exc),
                        "raw_output": stdout[:2000],
                        "ts": utc_now(),
                    }
                    self._emit_handler_result(
                        agent_id, task_id, backend, model, started_at, result
                    )
                    return result
                parsed["artifacts_produced"] = resolved
                self._emit_handler_result(agent_id, task_id, backend, model, started_at, parsed)
                return parsed
            else:
                self.logger.warning("[%s] %s 未输出结构化 JSON 结果", backend, agent_id)
                result = {
                    "status": "failed",
                    "error_type": "invalid_worker_output",
                    "error_message": "Worker did not return a structured JSON result",
                    "raw_output": stdout[:2000],
                    "ts": utc_now(),
                }
                self._emit_handler_result(agent_id, task_id, backend, model, started_at, result)
                return result

        except FileNotFoundError as exc:
            result = {
                "status": "failed",
                "error_type": "cli_not_found",
                "error_message": str(exc),
                "suggested_fix": f"检查 {backend} 可执行文件与 worker AGENT.md 路径",
                "ts": utc_now(),
            }
            self._emit_handler_result(agent_id, task_id, backend, model, started_at, result)
            return result

        except Exception as e:
            result = {
                "status": "failed",
                "error_type": "unexpected",
                "error_message": str(e),
                "suggested_fix": "检查 worker 配置和环境",
                "ts": utc_now(),
            }
            self._emit_handler_result(agent_id, task_id, backend, model, started_at, result)
            return result

    @staticmethod
    def _resolve_artifact_value(worker_dir: Path, key: object, value: object):
        """Resolve worker-reported files without accepting workspace escapes."""
        label = f"artifact '{key}' path"
        if isinstance(value, list):
            if not value:
                raise ValueError(f"{label} list must not be empty")
            return [
                str(resolve_relative_path(worker_dir, item, label=label))
                for item in value
            ]
        if isinstance(value, str):
            return str(resolve_relative_path(worker_dir, value, label=label))
        raise ValueError(f"{label} must be a relative string or a non-empty list of strings")

    def _emit_handler_result(
        self,
        agent_id: str,
        task_id: str,
        backend: str,
        model: str,
        started_at: float,
        result: dict,
    ) -> None:
        if not self.event_logger:
            return
        with self._correlation_lock:
            step_id = self._step_id_by_task.pop(task_id, task_id)
        event_name = (
            "handler.invoke.completed"
            if result.get("status") in {"completed", "recovered"}
            else "handler.invoke.failed"
        )
        self.event_logger.emit(
            event_name,
            component="engine.dispatcher",
            request_id=task_id,
            step_id=step_id,
            message=f"Worker handler finished for {agent_id}",
            duration_ms=(time.time() - started_at) * 1000,
            error_code=result.get("error_type"),
            error_message=result.get("error_message"),
            data={
                "agent_id": agent_id,
                "backend": backend,
                "model": model,
                "status": result.get("status"),
            },
        )

    def _extract_json_result(self, text: str) -> Optional[dict]:
        """从输出文本中提取最后一个 ```json ... ``` 块并解析"""
        blocks = re.findall(r"```json\s*([\s\S]*?)```", text)
        if not blocks:
            # 尝试提取最后一行的裸 JSON
            for line in reversed(text.strip().splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        pass
            return None

        # 尝试解析最后一个块
        for block in reversed(blocks):
            try:
                return json.loads(block.strip())
            except json.JSONDecodeError:
                continue
        return None


WorkerDispatcher = SubprocessStepHandler
