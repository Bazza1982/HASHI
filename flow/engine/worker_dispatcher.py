"""
HASHI Flow — Worker Dispatcher
负责调度本地 worker agent：写入任务到 inbox，启动 claude CLI 子进程，等待 outbox 结果
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent.parent  # hashi/


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerDispatcher:
    """
    本地 worker 任务调度器。
    每次调用 dispatch() 都会：
    1. 将任务写入 worker inbox
    2. 以子进程方式运行 claude CLI（--print 模式，非交互）
    3. 解析 JSON 输出，写入 outbox
    4. 返回结构化结果

    Worker 的 AGENT.md 作为 system prompt 传入 claude CLI。
    """

    def __init__(self, run_id: str, workers_base: Optional[Path] = None):
        self.run_id = run_id
        self.workers_base = workers_base or (ROOT / "flow" / "runs" / run_id / "workers")
        self.logger = logging.getLogger(f"flow.dispatcher.{run_id}")

    def dispatch(self, agent_id: str, task_message: dict, agent_md_path: str,
                 timeout_seconds: int = 600, backend: str = "claude-cli",
                 model: str = "") -> dict:
        """
        调度任务给指定 worker。

        Args:
            agent_id: worker 标识符
            task_message: 符合 agent.schema.yaml 的任务消息
            agent_md_path: worker AGENT.md 文件路径（相对于 hashi root）
            timeout_seconds: 最大等待秒数
            backend: 后端类型 ("claude-cli" 或 "codex-cli")
            model: 模型标识符（如 "claude-opus-4-6", "gpt-5.4"）

        Returns:
            {"status": "completed"|"failed", "artifacts_produced": {}, "summary": "", ...}
        """
        task_id = task_message.get("task_id", f"task-{agent_id}-{int(time.time())}")
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
        agent_md_full = ROOT / agent_md_path
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
            timeout_seconds=timeout_seconds,
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

    # =========================================================================
    # 内部方法
    # =========================================================================

    def _load_agent_md(self, agent_md_path: Path) -> str:
        if agent_md_path.exists():
            return agent_md_path.read_text(encoding="utf-8")
        self.logger.warning(f"[Dispatch] AGENT.md 不存在: {agent_md_path}")
        return "You are a helpful worker agent."

    def _build_worker_prompt(self, task_message: dict, worker_dir: Path) -> str:
        """将 task_assign 消息转为 worker 可执行的 prompt"""
        payload = task_message.get("payload", {})
        prompt = payload.get("prompt", "")
        input_artifacts = payload.get("input_artifacts", {})
        params = payload.get("params", {})

        lines = [
            f"# Task Assignment",
            f"",
            f"**Task ID**: {task_message.get('task_id', 'unknown')}",
            f"**Workflow**: {task_message.get('workflow_id', 'unknown')}",
            f"**Run**: {task_message.get('run_id', 'unknown')}",
            f"",
            f"## Your Task",
            f"",
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
                 user_prompt: str, worker_dir: Path, timeout_seconds: int,
                 backend: str = "claude-cli", model: str = "") -> dict:
        """调用 CLI 执行任务，根据 backend 选择 claude 或 codex，解析结果"""
        log_dir = worker_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_id}.log"

        # 根据 backend 构建命令
        if backend == "codex-cli":
            # Codex CLI（OpenAI）— 使用 exec 子命令非交互运行
            cmd = ["codex", "exec",
                   "--model", model or "o4-mini",
                   "--full-auto"]
            # codex exec 用 prompt 作为最后一个参数，合并 system + user prompt
            full_prompt = f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nTASK:\n{user_prompt}"
            cmd.append(full_prompt)
        else:
            # Claude CLI（默认）
            cmd = ["claude", "--print", "--dangerously-skip-permissions"]
            if model:
                cmd.extend(["--model", model])
            cmd.extend(["--system-prompt", system_prompt, user_prompt])

        self.logger.info(f"[{backend}] 启动 worker {agent_id}, task={task_id}, model={model}")

        # 移除嵌套会话检测变量（允许在 Claude Code 会话内启动子进程）
        child_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        child_env["CLAUDE_FLOW_WORKER"] = agent_id
        child_env["CLAUDE_FLOW_RUN"] = self.run_id

        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(worker_dir),
                env=child_env,
            )

            stdout = process.stdout or ""
            stderr = process.stderr or ""

            # 记录日志
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n")

            if process.returncode != 0:
                return {
                    "status": "failed",
                    "error_type": "cli_error",
                    "error_message": f"claude CLI exited with code {process.returncode}: {stderr[:500]}",
                    "suggested_fix": "检查 claude CLI 是否安装，model 是否可访问",
                    "raw_output": stdout[:2000],
                    "ts": utc_now(),
                }

            # 提取 JSON 结果块（最后一个 ```json ... ``` 块）
            parsed = self._extract_json_result(stdout)
            if parsed:
                parsed["ts"] = utc_now()
                parsed["raw_output_preview"] = stdout[:500]
                # 展开 artifacts 相对路径为绝对路径（支持单文件和文件列表）
                resolved = {}
                for k, v in parsed.get("artifacts_produced", {}).items():
                    if isinstance(v, list):
                        resolved[k] = [str(worker_dir / f) if not Path(f).is_absolute() else f for f in v]
                    elif isinstance(v, str):
                        resolved[k] = str(worker_dir / v) if not Path(v).is_absolute() else v
                    else:
                        resolved[k] = v
                parsed["artifacts_produced"] = resolved
                return parsed
            else:
                # 没有找到结构化 JSON — 视为完成但无工件
                self.logger.warning(f"[Claude] {agent_id} 未输出结构化 JSON 结果")
                return {
                    "status": "completed",
                    "artifacts_produced": {},
                    "summary": stdout[-500:],
                    "quality_notes": "Worker 未输出结构化 JSON，已保存原始输出",
                    "ts": utc_now(),
                }

        except subprocess.TimeoutExpired as e:
            # 超时时也保存已有输出到日志
            try:
                partial_stdout = (e.stdout or "") if hasattr(e, "stdout") else ""
                partial_stderr = (e.stderr or "") if hasattr(e, "stderr") else ""
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(f"=== TIMEOUT after {timeout_seconds}s ===\n\n"
                            f"=== STDOUT (partial) ===\n{partial_stdout}\n\n"
                            f"=== STDERR (partial) ===\n{partial_stderr}\n")
            except Exception:
                pass
            return {
                "status": "failed",
                "error_type": "timeout",
                "error_message": f"Worker {agent_id} 超时 ({timeout_seconds}s)",
                "suggested_fix": "增加 timeout_seconds 或简化任务",
                "ts": utc_now(),
            }
        except FileNotFoundError:
            return {
                "status": "failed",
                "error_type": "cli_not_found",
                "error_message": "claude CLI 未找到，请确保已安装 Claude Code CLI",
                "suggested_fix": "安装 claude CLI: npm install -g @anthropic-ai/claude-code",
                "ts": utc_now(),
            }
        except Exception as e:
            return {
                "status": "failed",
                "error_type": "unexpected",
                "error_message": str(e),
                "suggested_fix": "检查 worker 配置和环境",
                "ts": utc_now(),
            }

    def _extract_json_result(self, text: str) -> Optional[dict]:
        """从输出文本中提取最后一个 ```json ... ``` 块并解析"""
        import re
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
