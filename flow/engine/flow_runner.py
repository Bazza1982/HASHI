"""
HASHI Flow — Flow Runner
工作流执行核心引擎，解析 YAML 并按 DAG 顺序执行步骤
"""

import json
import uuid
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
        self.trace_id = str(uuid.uuid4())
        self.workflow = self._load_workflow()
        self.state = TaskState(self.run_id)
        self.artifacts = ArtifactStore(self.run_id)
        self.logger = self._setup_logger()
        self._paused = False
        self._aborted = False
        self._pre_flight_data: dict = {}
        self._step_results: dict = {}  # step_id → result dict (for skip_if evaluation)
        self._human_wait_deadline: Optional[float] = None   # NAGARE-TIMEOUT-001
        self._human_wait_defaults: dict = {}                # NAGARE-TIMEOUT-001: fallback defaults
        self._timeout_assumptions: list = []                # NAGARE-TIMEOUT-001: log of auto-assumed values
        self.dispatcher = WorkerDispatcher(self.run_id)
        # 信号文件路径（外部进程可通过创建这些文件来控制工作流）
        self._run_dir = Path(f"flow/runs/{self.run_id}")
        self._pause_signal = self._run_dir / "_pause"
        self._stop_signal = self._run_dir / "_stop"
        # human_interface agent（事件驱动通知的接收方）
        self._human_interface = (
            self.workflow.get("agents", {}).get("orchestrator", {}).get("human_interface")
        )

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

        total_steps = len(self.workflow.get("steps", []))
        self._notify_human(f"🚀 工作流启动 | {total_steps} 个步骤 | run={self.run_id}\n暂停: touch {self._pause_signal}\n停止: touch {self._stop_signal}")

        try:
            self._emit_event("workflow_started", {"workflow_id": self.workflow["workflow"]["id"]})
            result = self._execute_dag()
            if result["success"]:
                self.state.set_workflow_status(WorkflowStatus.COMPLETED)
                self.logger.info(f"[FlowRunner] 工作流完成 ✅")
                self._emit_event("workflow_completed", result)
                self._notify_human(f"🎉 工作流完成 ✅ | 全部 {total_steps} 步成功")
            else:
                self.state.set_workflow_status(WorkflowStatus.FAILED)
                self.logger.error(f"[FlowRunner] 工作流失败 ❌: {result.get('error')}")
                self._emit_event("workflow_failed", result)
                error_type = result.get("error_type", "unknown")
                self._notify_human(
                    f"❌ 工作流失败 [{error_type}]\n"
                    f"错误: {result.get('error', '未知错误')}\n"
                    f"已完成: {result.get('completed_steps', [])}\n"
                    f"失败: {result.get('failed_steps', [])}"
                )

            # 自动触发 Evaluator
            self._run_evaluator()

            # Candidate 晋升/回退
            self._handle_candidate_result(result["success"])

            return result

        except Exception as e:
            self.logger.exception(f"[FlowRunner] 未预期错误")
            self.state.set_workflow_status(WorkflowStatus.FAILED)
            # Orchestrator 自身故障 → 必须主动通知 human-facing agent
            import traceback
            tb = traceback.format_exc()
            self._notify_human(
                f"🚨🚨 ORCHESTRATOR 故障 🚨🚨\n"
                f"flow_runner 自身发生未预期异常，工作流已终止。\n\n"
                f"错误: {str(e)}\n"
                f"Traceback:\n{tb[-500:]}\n\n"
                f"Run: {self.run_id}\n"
                f"已完成步骤: {list(self.state.get_full_status().get('steps', {}).keys())}\n"
                f"⚠️ 这是基础设施级故障，需要人工检查代码。"
            )
            return {"success": False, "error": str(e), "run_id": self.run_id}

    def pause(self, reason: str = ""):
        """暂停工作流（当前步骤完成后生效）— 写信号文件，_check_signals 会读取"""
        self._pause_signal.parent.mkdir(parents=True, exist_ok=True)
        self._pause_signal.write_text(reason, encoding="utf-8")
        self.logger.info(f"[FlowRunner] 暂停信号已写入: {self._pause_signal}")

    def resume(self):
        """恢复暂停的工作流 — 删除信号文件，_check_signals 循环会自动检测"""
        if self._pause_signal.exists():
            self._pause_signal.unlink()
        self.logger.info("[FlowRunner] 暂停信号已移除")

    def abort(self, reason: str = ""):
        """紧急停止工作流 — 写信号文件，_check_signals 会立即终止"""
        self._stop_signal.parent.mkdir(parents=True, exist_ok=True)
        self._stop_signal.write_text(reason, encoding="utf-8")
        self.logger.info(f"[FlowRunner] 停止信号已写入: {self._stop_signal}")

    def get_status(self) -> dict:
        """获取当前工作流状态"""
        return self.state.get_full_status()

    # =========================================================================
    # 信号控制（外部可通过文件系统触发暂停/停止）
    # =========================================================================

    def _check_signals(self) -> str:
        """
        检查外部信号文件，返回当前信号状态。
        - _stop 文件存在 → 立即终止（硬停）
        - _pause 文件存在 → 暂停，等待文件被删除后恢复
        返回: "ok" | "stopped"
        """
        # 紧急停止：最高优先级
        if self._stop_signal.exists():
            self._aborted = True
            self.state.set_workflow_status(WorkflowStatus.ABORTED)
            reason = ""
            try:
                reason = self._stop_signal.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            self.logger.error(f"[Signal] 🛑 收到紧急停止信号" + (f": {reason}" if reason else ""))
            self._notify_human(f"🛑 工作流已紧急停止。" + (f"\n原因: {reason}" if reason else ""))
            return "stopped"

        # 暂停：等待信号文件被删除
        if self._pause_signal.exists():
            self._paused = True
            self.state.set_workflow_status(WorkflowStatus.PAUSED)
            reason = ""
            try:
                reason = self._pause_signal.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            self.logger.info(f"[Signal] ⏸️ 收到暂停信号" + (f": {reason}" if reason else ""))
            self._notify_human(f"⏸️ 工作流已暂停，等待恢复。删除 _pause 文件或调用 resume() 恢复。" + (f"\n原因: {reason}" if reason else ""))

            while self._pause_signal.exists() and not self._stop_signal.exists():
                # NAGARE-TIMEOUT-001: 检查 wait_for_human 超时
                if self._human_wait_deadline is not None and time.time() > self._human_wait_deadline:
                    self._handle_human_timeout()
                    break
                time.sleep(2)

            # 暂停期间可能收到停止信号
            if self._stop_signal.exists():
                return self._check_signals()

            self._paused = False
            self.state.set_workflow_status(WorkflowStatus.RUNNING)
            self.logger.info("[Signal] ▶️ 暂停已解除，继续执行")
            self._notify_human("▶️ 工作流已恢复，继续执行。")

        return "ok"

    def _notify_human(self, text: str):
        """向 human_interface agent 发送实时通知"""
        if not self._human_interface:
            return
        wf_id = self.workflow.get("workflow", {}).get("id", "?")
        full_msg = f"[{wf_id}] {text}"
        self._notify_via_hchat(self._human_interface, full_msg)

    def _notify_step_event(self, step: dict, event: str, detail: str = "",
                           result: dict = None):
        """步骤事件通知 — 除非步骤声明 notify: false，否则通知 human_interface（含内容摘要）"""
        if step.get("notify") is False:
            return
        msg = f"{event} | {step['id']}"
        if detail:
            msg += f" — {detail}"
        # 步骤完成时附加 worker 产出摘要
        if result and result.get("success") and result.get("result"):
            summary = result["result"].get("summary", "")
            if summary:
                msg += f"\n📋 摘要: {str(summary)[:300]}"
            artifacts = result["result"].get("artifacts_produced", {})
            if artifacts:
                artifact_names = [Path(v).name if isinstance(v, str) else f"{len(v)} files"
                                  for v in artifacts.values()]
                msg += f"\n📦 产出: {', '.join(artifact_names)}"
        self._notify_human(msg)

    def _handle_wait_for_human(self, step: dict, result: dict):
        """
        wait_for_human 机制：步骤完成后，读取产出中的 clarification_questions，
        通过 HChat 发给 human_interface，然后自动暂停等待回复。
        human 回答后写入 _human_response.json 并删除 _pause 文件恢复。
        恢复后将回答合并到 _pre_flight_data 中供后续步骤使用。
        """
        if not step.get("wait_for_human"):
            return

        # 从步骤产出中提取需要确认的问题
        questions = self._extract_questions(step, result)
        if not questions:
            self.logger.info(f"[WaitForHuman] {step['id']} 无需确认问题，继续执行")
            return

        # 写查询文件
        query_file = self._run_dir / "_human_query.json"
        response_file = self._run_dir / "_human_response.json"
        query_data = {
            "from_step": step["id"],
            "questions": questions,
            "response_file": str(response_file),
            "instruction": "请回答以上问题，将答案写入 _human_response.json 后删除 _pause 文件恢复工作流。"
        }
        query_file.write_text(json.dumps(query_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 通知 human_interface
        q_text = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(questions))
        self._notify_human(
            f"❓ 步骤 {step['id']} 需要确认以下问题：\n{q_text}\n\n"
            f"请回答后写入 {response_file} 并删除 _pause 文件恢复。"
        )

        # 解析超时配置（NAGARE-TIMEOUT-001）
        # 优先级：step.wait_for_human_timeout > workflow.pre_flight.human_response_timeout_seconds > 默认 300s
        timeout_secs = (
            step.get("wait_for_human_timeout_seconds")
            or self.workflow.get("pre_flight", {}).get("human_response_timeout_seconds")
            or 300
        )
        self._human_wait_deadline = time.time() + timeout_secs
        # 尝试读取 worker 提供的 smart defaults（文件名约定：_human_defaults.json）
        defaults_file = self._run_dir / "_human_defaults.json"
        if defaults_file.exists():
            try:
                self._human_wait_defaults = json.loads(defaults_file.read_text(encoding="utf-8"))
            except Exception:
                self._human_wait_defaults = {}
        else:
            self._human_wait_defaults = {}

        # 自动暂停
        self._pause_signal.write_text(f"等待 human 回答来自 {step['id']} 的问题", encoding="utf-8")
        self.logger.info(
            f"[WaitForHuman] 已暂停，等待 human 回答 {len(questions)} 个问题（超时 {timeout_secs}s）"
        )

        # _check_signals 会在下次循环中捕获暂停并等待
        # 暂停解除后读取回答
        # 注意：实际等待发生在 _execute_dag 的 _check_signals 调用中
        # 这里我们设置一个标记，让 _check_signals 解除后读取 response
        self._pending_human_response = response_file

    def _extract_questions(self, step: dict, result: dict) -> list:
        """从步骤产出的 artifact 中提取 clarification_questions"""
        if not result.get("success") or not result.get("result"):
            return []

        artifacts = result["result"].get("artifacts_produced", {})
        for key, path in artifacts.items():
            try:
                full_path = Path(f"flow/runs/{self.run_id}/workers/{step['agent']}") / path
                if not full_path.exists():
                    full_path = Path(f"flow/runs/{self.run_id}/workers") / path
                if not full_path.exists():
                    full_path = Path(path)
                if full_path.exists() and full_path.suffix == ".json":
                    data = json.loads(full_path.read_text(encoding="utf-8"))
                    # 查找 clarification_questions 或 questions_for_human 字段
                    questions = (
                        data.get("clarification_questions")
                        or data.get("questions_for_human")
                        or data.get("questions")
                        or []
                    )
                    if isinstance(questions, list) and questions:
                        return questions
            except Exception:
                continue
        return []

    def _read_human_response(self):
        """读取 human 的回答并合并到 pre_flight_data"""
        response_file = getattr(self, "_pending_human_response", None)
        if not response_file:
            return
        response_file = Path(response_file)
        if response_file.exists():
            try:
                response = json.loads(response_file.read_text(encoding="utf-8"))
                if isinstance(response, dict):
                    self._pre_flight_data.update(response)
                    self.logger.info(f"[WaitForHuman] 已读取 human 回答: {list(response.keys())}")
                    self._notify_human(f"📝 已收到回答，合并 {len(response)} 个字段到上下文，继续执行。")
            except Exception as e:
                self.logger.warning(f"[WaitForHuman] 读取回答失败: {e}")
        else:
            self.logger.warning(f"[WaitForHuman] 回答文件不存在: {response_file}，以空回答继续")
        self._pending_human_response = None
        self._human_wait_deadline = None   # NAGARE-TIMEOUT-001: 清空超时状态
        self._human_wait_defaults = {}

    def _handle_human_timeout(self):
        """
        NAGARE-TIMEOUT-001: wait_for_human 超时处理。

        关键设计原则：pre-flight 是工作流的前置控制门，必须等待所有问题被人工回答。
        超时只是一个提醒机制，不允许自动用默认值继续——无论问题是否有默认值。
        工作流在收到人工回答前始终保持暂停状态。
        """
        step_id = "unknown"
        questions = []
        try:
            query_file = self._run_dir / "_human_query.json"
            if query_file.exists():
                qdata = json.loads(query_file.read_text(encoding="utf-8"))
                step_id = qdata.get("from_step", "unknown")
                questions = qdata.get("questions", [])
        except Exception:
            pass

        # 延长等待时间（再等 300s），重新提醒并继续等待
        self._human_wait_deadline = time.time() + 300
        self.logger.warning(
            f"[WaitForHuman] ⏱️ 超时提醒，继续等待人工回答（step={step_id}）"
        )
        q_text = "\n".join(
            f"  {i+1}. {q.get('question', q.get('key', 'unknown'))}"
            for i, q in enumerate(questions)
        ) if questions else "  （详见 _human_query.json）"
        self._notify_human(
            f"⏱️ 步骤 {step_id} 等待超时提醒——工作流仍在暂停，等待您回答以下问题：\n"
            f"{q_text}\n\n"
            f"请回答后写入 {self._run_dir}/_human_response.json 并删除 _pause 文件恢复。\n"
            f"（将在 5 分钟后再次提醒）"
        )
        # 不清除 pause，不写入任何默认值，继续等待

    # =========================================================================
    # 内部执行逻辑
    # =========================================================================

    def _should_skip(self, step: dict) -> bool:
        """评估 skip_if 条件，决定是否跳过步骤"""
        skip_if = step.get("skip_if")
        if not skip_if:
            return False

        # 构建评估上下文：所有已产生的 artifact 数据 + pre_flight 数据
        context = {"pre_flight": self._pre_flight_data}
        for step_id, result in self._step_results.items():
            if result.get("success") and result.get("result"):
                # 尝试读取该步骤的 artifact 内容
                artifacts = result["result"].get("artifacts_produced", {})
                for key, path in artifacts.items():
                    try:
                        full_path = Path(f"flow/runs/{self.run_id}/workers") / path
                        if not full_path.exists():
                            full_path = Path(path)
                        if full_path.exists() and full_path.suffix == ".json":
                            context[key] = json.loads(full_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass

        # 简单条件评估（支持常见模式）
        try:
            condition = skip_if.strip()
            # "X is empty" → 检查字段为空列表/None/空字符串
            if " is empty" in condition:
                parts = condition.split(" is empty")[0].strip()
                # "improvement_package.pending_for_human is empty"
                obj_parts = parts.split(".")
                val = context
                for p in obj_parts:
                    if isinstance(val, dict):
                        val = val.get(p)
                    else:
                        val = None
                        break
                field_empty = val is None or val == [] or val == "" or val == {}

                # 处理 "X is empty and Y == 'Z'" 复合条件
                if " and " in condition:
                    rest = condition.split(" and ", 1)[1].strip()
                    if "==" in rest:
                        lhs, rhs = rest.split("==", 1)
                        lhs_parts = lhs.strip().split(".")
                        lhs_val = context
                        for p in lhs_parts:
                            if isinstance(lhs_val, dict):
                                lhs_val = lhs_val.get(p)
                            else:
                                lhs_val = None
                                break
                        rhs_val = rhs.strip().strip("'\"")
                        return field_empty and str(lhs_val) == rhs_val
                return field_empty

            # "X == 'value'" → 简单相等比较
            if "==" in condition:
                lhs, rhs = condition.split("==", 1)
                lhs_parts = lhs.strip().split(".")
                val = context
                for p in lhs_parts:
                    if isinstance(val, dict):
                        val = val.get(p)
                    else:
                        val = None
                        break
                rhs_val = rhs.strip().strip("'\"")
                return str(val) == rhs_val

        except Exception as e:
            self.logger.warning(f"[SkipIf] 条件评估失败: {skip_if} → {e}，不跳过")

        return False

    def _execute_dag(self) -> dict:
        """解析 DAG，按依赖顺序执行所有步骤"""
        steps = self.workflow.get("steps", [])
        step_map = {s["id"]: s for s in steps}
        completed = set()
        failed = set()

        # 初始化所有步骤状态
        for step in steps:
            self.state.set_step_status(step["id"], StepStatus.PENDING)

        self.logger.info(f"[FlowRunner] trace_id={self.trace_id}")

        self._pending_human_response = None  # 初始化
        self._human_wait_deadline = None     # NAGARE-TIMEOUT-001
        self._human_wait_defaults = {}       # NAGARE-TIMEOUT-001

        while True:
            # 检查外部信号（停止/暂停）
            if self._check_signals() == "stopped":
                return {"success": False, "error": "工作流已被紧急停止", "run_id": self.run_id}

            # 暂停解除后，检查是否有待读取的 human 回答
            if self._pending_human_response:
                self._read_human_response()

            if self._aborted:
                return {"success": False, "error": "工作流已被终止", "run_id": self.run_id}

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
                # 检查 skip_if 条件
                if self._should_skip(step):
                    self.logger.info(f"[Step] 跳过: {step['id']} (skip_if 条件满足)")
                    self.state.set_step_status(step["id"], StepStatus.SKIPPED)
                    self._step_results[step["id"]] = {"success": True, "skipped": True}
                    completed.add(step["id"])
                    self._notify_step_event(step, "⏭️ 跳过", "skip_if 条件满足")
                    continue

                # 步骤间缓冲：给 human 时间查看通知并发出暂停/停止指令
                inter_step_wait = self.workflow.get("inter_step_wait_seconds", 30)
                if completed and inter_step_wait > 0:
                    self.logger.info(f"[FlowRunner] 步骤间等待 {inter_step_wait}s（可在此期间暂停/停止）")
                    for _ in range(inter_step_wait):
                        if self._check_signals() == "stopped":
                            return {"success": False, "error": "工作流已被紧急停止", "run_id": self.run_id}
                        time.sleep(1)

                # 步骤执行前最终检查信号
                if self._check_signals() == "stopped":
                    return {"success": False, "error": "工作流已被紧急停止", "run_id": self.run_id}

                self._notify_step_event(step, "🔄 开始", step.get("name", ""))
                result = self._execute_step(step)
                self._step_results[step["id"]] = result
                if result["success"]:
                    completed.add(step["id"])
                    done = len(completed)
                    total = len(steps)
                    self._notify_step_event(step, f"✅ 完成 ({done}/{total})", step.get("name", ""), result=result)
                    # wait_for_human: 步骤完成后可能需要人工确认
                    self._handle_wait_for_human(step, result)
                else:
                    failed.add(step["id"])
                    error_type = result.get("error_type", "unknown")
                    error_msg = str(result.get("error", ""))[:200]
                    self._notify_step_event(step, "❌ 失败", f"[{error_type}] {error_msg}")

                    # 基础设施故障（非任务级别）→ debug agent 无法修复，直接通知 human
                    if error_type in ("unexpected", "cli_error", "cli_not_found", "timeout", "exception"):
                        self._notify_human(
                            f"🚨 基础设施故障 | {step['id']}\n"
                            f"类型: {error_type}\n"
                            f"错误: {error_msg}\n"
                            f"⚠️ 这不是任务错误，debug agent 无法修复。需要人工介入。"
                        )
                        self._escalate_to_orchestrator(step, result)
                        return {
                            "success": False,
                            "error": f"步骤 {step['id']} 基础设施故障 [{error_type}]: {error_msg}",
                            "error_type": error_type,
                            "run_id": self.run_id
                        }

                    # 任务级别失败 → 触发 debug agent
                    recovered = self._handle_failure(step, result)
                    if recovered:
                        failed.discard(step["id"])
                        self._notify_step_event(step, "🔧 Debug 恢复成功", "重新执行中...")
                        # 重新执行
                        result = self._execute_step(step)
                        self._step_results[step["id"]] = result
                        if result["success"]:
                            completed.add(step["id"])
                            done = len(completed)
                            self._notify_step_event(step, f"✅ 重试成功 ({done}/{total})", step.get("name", ""))
                        else:
                            failed.add(step["id"])
                            self._notify_step_event(step, "❌ 重试仍失败", str(result.get("error", ""))[:200])
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
                step_names = ", ".join(s["id"] for s in parallel_steps)
                self._notify_human(f"🔄 并行执行 {len(parallel_steps)} 个步骤: {step_names}")
                results = self._execute_parallel(parallel_steps)
                for step, result in zip(parallel_steps, results):
                    if result["success"]:
                        completed.add(step["id"])
                        done = len(completed)
                        total = len(steps)
                        self._notify_step_event(step, f"✅ 完成 ({done}/{total})", step.get("name", ""), result=result)
                    else:
                        failed.add(step["id"])
                        self._notify_step_event(step, "❌ 失败", str(result.get("error", ""))[:200])

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

                # post-step handler: apply_improvement 由 runner 执行文件写入
                if step_id == "apply_improvement":
                    self._post_apply_improvement(result)

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
                # dispatcher 返回 error_message，统一提取错误信息
                error_msg = (result.get("error_message")
                             or result.get("error")
                             or "未知错误（worker 未返回错误信息）")
                error_type = result.get("error_type", "unknown")
                self.state.set_step_status(step_id, StepStatus.FAILED)
                self._emit_event("step_failed", {
                    "step_id": step_id,
                    "agent_id": agent_id,
                    "error": error_msg,
                    "error_type": error_type,
                    "duration_seconds": duration
                })
                self.logger.error(f"[Step] 失败: {step_id} [{error_type}] - {error_msg}")
                return {
                    "success": False, "step_id": step_id,
                    "error": error_msg, "error_type": error_type,
                    "result": result
                }

        except Exception as e:
            duration = time.time() - start_time
            self.state.set_step_status(step_id, StepStatus.FAILED)
            self.logger.exception(f"[Step] 异常: {step_id}")
            return {"success": False, "step_id": step_id, "error": str(e), "error_type": "exception"}

    def _post_apply_improvement(self, result: dict) -> None:
        """
        apply_improvement 步骤的 post-step handler。
        Worker 只负责生成 staged_flow/ 目录下的文件，
        runner 拥有完整文件系统权限，负责将 staged 文件写入正式路径。
        """
        flow_root = Path(__file__).parent.parent  # flow/
        repo_root = flow_root.parent              # hashi/

        # 找到 evaluator worker 目录（staged_flow 由 evaluator 产出）
        run_dir = Path(f"flow/runs/{self.run_id}")
        if not run_dir.is_absolute():
            run_dir = repo_root / run_dir

        # 搜索 staged_flow 目录
        staged_roots = list(run_dir.rglob("staged_flow/flow"))
        if not staged_roots:
            self.logger.warning("[PostApplyImprovement] 未找到 staged_flow/flow 目录，跳过写入")
            return

        staged_flow = staged_roots[0]
        target_flow = repo_root / "flow"

        import shutil
        copied = []
        for src in staged_flow.rglob("*"):
            if src.is_file():
                relative = src.relative_to(staged_flow)
                dst = target_flow / relative
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(str(relative))

        if copied:
            self.logger.info(f"[PostApplyImprovement] 写入 {len(copied)} 个文件到 flow/: {copied[:5]}{'...' if len(copied) > 5 else ''}")
        else:
            self.logger.warning("[PostApplyImprovement] staged_flow/flow 目录为空，无文件写入")

    def _execute_parallel(self, steps: list) -> list:
        """并行执行多个步骤"""
        results = [None] * len(steps)
        threads = []

        def run_step(i, step):
            try:
                results[i] = self._execute_step(step)
            except Exception as e:
                self.logger.exception(f"[Parallel] 线程异常: {step['id']}")
                results[i] = {"success": False, "step_id": step["id"], "error": str(e)}

        for i, step in enumerate(steps):
            t = threading.Thread(target=run_step, args=(i, step))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 兜底：确保没有 None 结果
        for i, step in enumerate(steps):
            if results[i] is None:
                self.logger.error(f"[Parallel] 步骤 {step['id']} 线程无返回值，标记为失败")
                results[i] = {"success": False, "step_id": step["id"], "error": "线程无返回值"}

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

    def _handle_candidate_result(self, success: bool):
        """Candidate 晋升/回退：成功则替换原文件，失败则删除 candidate"""
        if not self._using_candidate or not self._candidate_path:
            return

        if success:
            # 晋升：candidate → 正式版本
            import shutil
            backup_dir = Path("flow/evaluation_kb/workflow_versions")
            backup_dir.mkdir(parents=True, exist_ok=True)
            wf_id = self.workflow.get("workflow", {}).get("id", "unknown")
            version = self.workflow.get("workflow", {}).get("version", "unknown")
            backup_path = backup_dir / wf_id / f"v{version}_promoted.yaml"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._candidate_path, backup_path)
            shutil.copy2(self._candidate_path, self.workflow_path)
            self._candidate_path.unlink()
            self.logger.info(f"[Candidate] 晋升成功: {self._candidate_path.name} → {self.workflow_path.name}")
            self._notify_via_hchat(
                self.workflow.get("agents", {}).get("orchestrator", {}).get("human_interface", "akane"),
                f"✅ Candidate 工作流晋升成功 (v{version})，已替换为正式版本。"
            )
        else:
            # 回退：删除 candidate
            self._candidate_path.unlink()
            self.logger.warning(f"[Candidate] 运行失败，已删除 candidate: {self._candidate_path.name}")
            self._notify_via_hchat(
                self.workflow.get("agents", {}).get("orchestrator", {}).get("human_interface", "akane"),
                f"❌ Candidate 工作流运行失败，已自动回退到上一版本。"
            )

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
        backend = worker_def.get("backend", "claude-cli")
        model = worker_def.get("model", "")

        result = self.dispatcher.dispatch(
            agent_id=agent_id,
            task_message=message,
            agent_md_path=agent_md_path,
            timeout_seconds=timeout,
            backend=backend,
            model=model,
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
            "trace_id": self.trace_id,
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
        # Candidate 机制：如果存在 candidate 文件，优先加载
        candidate_path = self.workflow_path.with_name(
            self.workflow_path.stem + "_candidate" + self.workflow_path.suffix
        )
        if candidate_path.exists():
            self.logger.info(f"[FlowRunner] 发现 candidate 文件: {candidate_path}，优先加载")
            self._using_candidate = True
            self._candidate_path = candidate_path
            with open(candidate_path) as f:
                return yaml.safe_load(f)
        self._using_candidate = False
        self._candidate_path = None
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
