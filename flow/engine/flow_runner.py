"""
HASHI Flow — Flow Runner
工作流执行核心引擎，解析 YAML 并按 DAG 顺序执行步骤
"""

import json
import yaml
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .task_state import TaskState
from .artifact_store import ArtifactStore
from .worker_dispatcher import WorkerDispatcher


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StepStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowStatus:
    CREATED = "created"
    PRE_FLIGHT = "pre_flight"
    CONFIRMED = "confirmed"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class FlowRunner:
    """
    工作流执行器
    负责解析 workflow.yaml，按 DAG 顺序分配任务给 worker agents
    """

    def __init__(self, workflow_path: str, run_id: Optional[str] = None):
        self.workflow_path = Path(workflow_path)
        self.run_id = run_id or self._generate_run_id()
        self.workflow = self._load_workflow()
        self.state = TaskState(self.run_id)
        self.artifacts = ArtifactStore(self.run_id)
        self.logger = self._setup_logger()
        self._paused = False
        self._aborted = False
        self._pre_flight_data: dict = {}
        self.dispatcher = WorkerDispatcher(self.run_id)

    # =========================================================================
    # 公开接口
    # =========================================================================

    def set_pre_flight_data(self, data: dict):
        """设置 pre-flight 收集到的用户输入数据"""
        self._pre_flight_data = data
        self.logger.info(f"[PreFlight] 已设置 {len(data)} 个输入字段")

    def start(self) -> dict:
        """启动工作流"""
        self.logger.info(f"[FlowRunner] 启动工作流: {self.workflow['workflow']['id']}, run_id={self.run_id}")
        self.state.set_workflow_status(WorkflowStatus.RUNNING)

        try:
            self._emit_event("workflow_started", {"workflow_id": self.workflow["workflow"]["id"]})
            result = self._execute_dag()
            if result["success"]:
                self.state.set_workflow_status(WorkflowStatus.COMPLETED)
                self.logger.info(f"[FlowRunner] 工作流完成 ✅")
                self._emit_event("workflow_completed", result)
            else:
                self.state.set_workflow_status(WorkflowStatus.FAILED)
                self.logger.error(f"[FlowRunner] 工作流失败 ❌: {result.get('error')}")
                self._emit_event("workflow_failed", result)

            # 自动触发 Evaluator
            self._run_evaluator()
            return result

        except Exception as e:
            self.logger.exception(f"[FlowRunner] 未预期错误")
            self.state.set_workflow_status(WorkflowStatus.FAILED)
            return {"success": False, "error": str(e), "run_id": self.run_id}

    def pause(self):
        """暂停工作流（当前步骤完成后生效）"""
        self._paused = True
        self.state.set_workflow_status(WorkflowStatus.PAUSED)
        self.logger.info("[FlowRunner] 工作流暂停请求已接收")

    def resume(self):
        """恢复暂停的工作流"""
        self._paused = False
        self.state.set_workflow_status(WorkflowStatus.RUNNING)
        self.logger.info("[FlowRunner] 工作流已恢复")

    def abort(self):
        """终止工作流"""
        self._aborted = True
        self.state.set_workflow_status(WorkflowStatus.ABORTED)
        self.logger.info("[FlowRunner] 工作流已终止")

    def get_status(self) -> dict:
        """获取当前工作流状态"""
        return self.state.get_full_status()

    # =========================================================================
    # 内部执行逻辑
    # =========================================================================

    def _execute_dag(self) -> dict:
        """解析 DAG，按依赖顺序执行所有步骤"""
        steps = self.workflow.get("steps", [])
        step_map = {s["id"]: s for s in steps}
        completed = set()
        failed = set()

        # 初始化所有步骤状态
        for step in steps:
            self.state.set_step_status(step["id"], StepStatus.PENDING)

        while True:
            if self._aborted:
                return {"success": False, "error": "工作流已被终止", "run_id": self.run_id}

            # 等待暂停解除
            while self._paused and not self._aborted:
                time.sleep(2)

            # 找出所有可执行的步骤（依赖已完成且自身未执行）
            ready = []
            for step in steps:
                sid = step["id"]
                if sid in completed or sid in failed:
                    continue
                deps = step.get("depends", [])
                if all(d in completed for d in deps):
                    if not any(d in failed for d in deps):
                        ready.append(step)

            if not ready:
                # 没有可执行步骤
                all_done = all(s["id"] in completed for s in steps)
                if all_done:
                    return {"success": True, "run_id": self.run_id, "completed_steps": list(completed)}
                else:
                    # 有步骤因为依赖失败而无法执行
                    blocked = [s["id"] for s in steps if s["id"] not in completed and s["id"] not in failed]
                    return {
                        "success": False,
                        "error": f"以下步骤因依赖失败而无法执行: {blocked}",
                        "run_id": self.run_id,
                        "failed_steps": list(failed)
                    }

            # 判断是否并行执行
            parallel_steps = [s for s in ready if s.get("strategy", "sequential") == "parallel"]
            sequential_steps = [s for s in ready if s not in parallel_steps]

            # 先处理顺序步骤（第一个）
            if sequential_steps:
                step = sequential_steps[0]
                result = self._execute_step(step)
                if result["success"]:
                    completed.add(step["id"])
                else:
                    failed.add(step["id"])
                    # 触发 debug agent
                    recovered = self._handle_failure(step, result)
                    if recovered:
                        failed.discard(step["id"])
                        # 重新执行
                        result = self._execute_step(step)
                        if result["success"]:
                            completed.add(step["id"])
                        else:
                            failed.add(step["id"])
                            # 上报 Orchestrator
                            self._escalate_to_orchestrator(step, result)
                            return {
                                "success": False,
                                "error": f"步骤 {step['id']} 在 Debug 恢复后仍然失败",
                                "run_id": self.run_id
                            }
                    else:
                        self._escalate_to_orchestrator(step, result)
                        return {
                            "success": False,
                            "error": f"步骤 {step['id']} 失败且无法恢复",
                            "run_id": self.run_id
                        }

            # 并行执行
            elif parallel_steps:
                results = self._execute_parallel(parallel_steps)
                for step, result in zip(parallel_steps, results):
                    if result["success"]:
                        completed.add(step["id"])
                    else:
                        failed.add(step["id"])

    def _execute_step(self, step: dict) -> dict:
        """执行单个步骤"""
        step_id = step["id"]
        agent_id = step["agent"]
        self.logger.info(f"[Step] 开始: {step_id} → agent={agent_id}")
        self.state.set_step_status(step_id, StepStatus.RUNNING)
        self._emit_event("step_started", {"step_id": step_id, "agent_id": agent_id})

        start_time = time.time()

        try:
            # 构建任务消息
            task_message = self._build_task_message(step)

            # 通过 HChat 发送任务给 worker
            result = self._send_task_via_hchat(agent_id, task_message)

            duration = time.time() - start_time

            if result["status"] == "completed":
                # 注册工件
                for artifact_key, artifact_path in result.get("artifacts_produced", {}).items():
                    self.artifacts.register(artifact_key, artifact_path)

                self.state.set_step_status(step_id, StepStatus.COMPLETED)
                self._emit_event("step_completed", {
                    "step_id": step_id,
                    "agent_id": agent_id,
                    "duration_seconds": duration,
                    "artifacts": result.get("artifacts_produced", {})
                })
                self.logger.info(f"[Step] 完成: {step_id} ({duration:.1f}s)")
                return {"success": True, "step_id": step_id, "result": result}

            else:
                self.state.set_step_status(step_id, StepStatus.FAILED)
                self._emit_event("step_failed", {
                    "step_id": step_id,
                    "agent_id": agent_id,
                    "error": result.get("error"),
                    "duration_seconds": duration
                })
                self.logger.error(f"[Step] 失败: {step_id} - {result.get('error')}")
                return {"success": False, "step_id": step_id, "error": result.get("error"), "result": result}

        except Exception as e:
            duration = time.time() - start_time
            self.state.set_step_status(step_id, StepStatus.FAILED)
            self.logger.exception(f"[Step] 异常: {step_id}")
            return {"success": False, "step_id": step_id, "error": str(e)}

    def _execute_parallel(self, steps: list) -> list:
        """并行执行多个步骤"""
        results = [None] * len(steps)
        threads = []

        def run_step(i, step):
            results[i] = self._execute_step(step)

        for i, step in enumerate(steps):
            t = threading.Thread(target=run_step, args=(i, step))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        return results

    def _handle_failure(self, step: dict, failure_result: dict) -> bool:
        """调用 Debug Agent 处理失败步骤，返回是否成功恢复"""
        error_handling = self.workflow.get("error_handling", {})
        debug_agent = error_handling.get("debug_agent")
        max_attempts = error_handling.get("max_attempts", 3)

        if not debug_agent:
            self.logger.warning("[Debug] 未配置 debug_agent，跳过自动恢复")
            return False

        self.logger.info(f"[Debug] 启动 Debug Agent: {debug_agent}")
        self._emit_event("debug_started", {"step_id": step["id"], "debug_agent": debug_agent})

        # 调用 Debug Agent
        debug_message = {
            "task": "debug_and_recover",
            "failed_step": {
                "step_id": step["id"],
                "error": failure_result.get("error"),
                "step_definition": step,
            },
            "max_attempts": max_attempts,
            "workflow_path": str(self.workflow_path)
        }

        result = self._send_task_via_hchat(debug_agent, debug_message)
        recovered = result.get("status") == "recovered"

        self._emit_event("debug_completed", {
            "step_id": step["id"],
            "recovered": recovered,
            "debug_result": result
        })

        return recovered

    def _run_evaluator(self):
        """同步运行 Evaluator（工作流结束后立即评估，确保评分写入磁盘）"""
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            from flow.agents.evaluator.evaluator import FlowEvaluator
            ev = FlowEvaluator()
            report = ev.evaluate_run(self.run_id)
            self.logger.info(
                f"[Evaluator] 评估完成: overall={report['scores'].get('overall')}"
            )
        except Exception as e:
            self.logger.warning(f"[Evaluator] 评估失败（非致命）: {e}")

    def _escalate_to_orchestrator(self, step: dict, failure_result: dict):
        """向 Orchestrator 上报无法恢复的失败"""
        orchestrator_id = self.workflow.get("agents", {}).get("orchestrator", {}).get("id", "akane")
        error_handling = self.workflow.get("error_handling", {})
        msg_template = error_handling.get("on_max_exceeded", {}).get(
            "message",
            "工作流步骤失败（已耗尽重试）：{failed_step_id} — {error}"
        )
        error_msg = str(failure_result.get("error", ""))
        message = msg_template.replace("{failed_step_id}", step["id"]).replace("{error}", error_msg)

        self.logger.warning(f"[Escalate] 向 {orchestrator_id} 上报: {message}")
        self._emit_event("escalated_to_orchestrator", {
            "step_id": step["id"],
            "error": error_msg,
            "orchestrator_id": orchestrator_id,
            "message": message,
        })
        self._notify_via_hchat(orchestrator_id, f"⚠️ Flow Runner 上报失败\n{message}")

    # =========================================================================
    # HChat 通信
    # =========================================================================

    def _send_task_via_hchat(self, agent_id: str, message: dict) -> dict:
        """
        调度任务给 worker agent。
        - 对于本地 worker：使用 WorkerDispatcher（写 inbox，运行 claude CLI，读 outbox）
        - 对于 Orchestrator 通知：使用 hchat_send.py API
        """
        # 查找 worker 定义
        worker_def = self._get_worker_def(agent_id)
        if not worker_def:
            self.logger.warning(f"[HChat] 未找到 worker 定义: {agent_id}，尝试直接发送通知")
            self._notify_via_hchat(agent_id, json.dumps(message, ensure_ascii=False))
            return {"status": "failed", "error": f"未定义的 worker: {agent_id}"}

        agent_md_path = worker_def.get("agent_md", f"flow/agents/{agent_id}/AGENT.md")
        timeout = message.get("timeout_seconds", 600)

        result = self.dispatcher.dispatch(
            agent_id=agent_id,
            task_message=message,
            agent_md_path=agent_md_path,
            timeout_seconds=timeout,
        )
        return result

    def _notify_via_hchat(self, agent_id: str, text: str):
        """通过 HChat API 发送纯文本通知（用于 Orchestrator 通知）"""
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
            from tools.hchat_send import send_hchat
            send_hchat(to_agent=agent_id, from_agent="flow-runner", text=text)
        except Exception as e:
            self.logger.warning(f"[HChat] 通知发送失败 ({agent_id}): {e}")

    def _get_worker_def(self, agent_id: str) -> Optional[dict]:
        """从 workflow 定义中查找 worker"""
        workers = self.workflow.get("agents", {}).get("workers", [])
        for w in workers:
            if w.get("id") == agent_id:
                return w
        return None

    def _build_task_message(self, step: dict) -> dict:
        """构建符合 agent.schema.yaml 的任务消息"""
        return {
            "msg_type": "task_assign",
            "task_id": f"step-{step['id']}-{self.run_id}",
            "workflow_id": self.workflow["workflow"]["id"],
            "run_id": self.run_id,
            "from": "orchestrator",
            "to": step["agent"],
            "payload": {
                "step_id": step["id"],
                "prompt": step.get("prompt", ""),
                "input_artifacts": self._resolve_input_artifacts(step),
                "output_spec": step.get("output", {}).get("artifacts", []),
                "params": self._resolve_params(step)
            },
            "timeout_seconds": step.get("timeout_seconds", 600),
            "ts": utc_now()
        }

    def _resolve_input_artifacts(self, step: dict) -> dict:
        """解析步骤的输入工件"""
        result = {}
        for key in step.get("input", {}).get("from_artifacts", []):
            path = self.artifacts.get(key)
            if path:
                result[key] = str(path)
        return result

    def _resolve_params(self, step: dict) -> dict:
        """解析步骤参数，替换 {pre_flight.xxx} 和 {artifacts.xxx} 变量"""
        params = step.get("input", {}).get("params", {})
        return {k: self._substitute(v) for k, v in params.items()}

    def _substitute(self, value) -> str:
        """替换字符串中的变量占位符"""
        if not isinstance(value, str):
            return value
        import re
        def replacer(match):
            var_path = match.group(1)
            parts = var_path.split(".", 1)
            if len(parts) == 2:
                ns, key = parts
                if ns == "pre_flight":
                    return str(self._pre_flight_data.get(key, f"{{{var_path}}}"))
                elif ns == "artifacts":
                    path = self.artifacts.get(key)
                    return str(path) if path else f"{{{var_path}}}"
            return match.group(0)
        return re.sub(r"\{([^}]+)\}", replacer, value)

    # =========================================================================
    # 事件系统（供 Evaluator 监听）
    # =========================================================================

    def _emit_event(self, event_type: str, data: dict):
        """发布事件，写入 evaluation_events.jsonl 供 Evaluator 消费"""
        event = {
            "event_type": event_type,
            "workflow_id": self.workflow["workflow"]["id"],
            "run_id": self.run_id,
            "ts": utc_now(),
            "data": data
        }
        events_file = Path(f"flow/runs/{self.run_id}/evaluation_events.jsonl")
        events_file.parent.mkdir(parents=True, exist_ok=True)
        with open(events_file, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # =========================================================================
    # 工具方法
    # =========================================================================

    def _load_workflow(self) -> dict:
        with open(self.workflow_path) as f:
            return yaml.safe_load(f)

    def _generate_run_id(self) -> str:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        wf_id = "unknown"
        try:
            with open(self.workflow_path) as f:
                data = yaml.safe_load(f)
                wf_id = data.get("workflow", {}).get("id", "unknown")
        except Exception:
            pass
        return f"run-{wf_id}-{ts}"

    def _setup_logger(self) -> logging.Logger:
        log_dir = Path(f"flow/runs/{self.run_id}/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"flow.runner.{self.run_id}")
        logger.setLevel(logging.DEBUG)
        handler = logging.FileHandler(log_dir / "flow_runner.log")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        return logger


# =============================================================================
# CLI 入口
# =============================================================================

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python flow_runner.py <workflow.yaml>")
        sys.exit(1)

    runner = FlowRunner(sys.argv[1])
    result = runner.start()
    print(json.dumps(result, ensure_ascii=False, indent=2))
