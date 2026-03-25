"""
HASHI Flow — Evaluator Agent (Python Implementation)
系统级评估代理：消费 evaluation_events.jsonl，维护知识库，生成改进建议

可以作为独立进程运行（持续监听），也可以在每次 run 结束后被调用。
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml


ROOT = Path(__file__).resolve().parent.parent.parent.parent  # hashi/
KB_PATH = ROOT / "flow" / "evaluation_kb"
RUNS_PATH = ROOT / "flow" / "runs"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FlowEvaluator:
    """
    工作流评估代理。
    在每次工作流 run 结束后调用 evaluate_run()，
    分析事件流，更新知识库，生成改进建议。
    """

    def __init__(self):
        self.logger = logging.getLogger("flow.evaluator")
        KB_PATH.mkdir(parents=True, exist_ok=True)
        (KB_PATH / "improvements").mkdir(parents=True, exist_ok=True)
        (KB_PATH / "patterns").mkdir(parents=True, exist_ok=True)
        (KB_PATH / "workflow_scores").mkdir(parents=True, exist_ok=True)

    def evaluate_run(self, run_id: str) -> dict:
        """
        评估一次工作流运行的结果。

        Args:
            run_id: 工作流运行 ID

        Returns:
            评估报告字典
        """
        run_dir = RUNS_PATH / run_id
        events_file = run_dir / "evaluation_events.jsonl"

        if not events_file.exists():
            self.logger.warning(f"[Evaluator] 未找到事件文件: {events_file}")
            return {"run_id": run_id, "status": "no_events", "scores": {}}

        events = self._load_events(events_file)
        report = self._analyze_events(run_id, events)

        # 记录评分
        self._record_score(report)

        # 生成改进建议（如有）
        improvements = self._generate_improvements(run_id, events, report)
        if improvements:
            self._save_improvements(improvements)

        # 更新模式库（运行足够多后触发）
        self._maybe_update_patterns(events, report)

        self.logger.info(
            f"[Evaluator] run={run_id} | "
            f"efficiency={report['scores']['efficiency']:.1f} "
            f"stability={report['scores']['stability']:.1f} "
            f"interventions={report['metrics']['human_interventions']}"
        )

        return report

    # =========================================================================
    # 分析核心
    # =========================================================================

    def _load_events(self, events_file: Path) -> list[dict]:
        events = []
        with open(events_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return events

    def _analyze_events(self, run_id: str, events: list[dict]) -> dict:
        """从事件序列中提取关键指标"""
        workflow_id = events[0]["workflow_id"] if events else "unknown"

        # 时序分析
        start_ts = None
        end_ts = None
        step_durations = {}
        debug_count = 0
        escalation_count = 0
        human_interventions = 0
        failed_steps = []
        completed_steps = []

        for ev in events:
            ts = ev.get("ts")
            event_type = ev.get("event_type")
            data = ev.get("data", {})

            if event_type == "workflow_started" and start_ts is None:
                start_ts = ts
            elif event_type in ("workflow_completed", "workflow_failed"):
                end_ts = ts

            elif event_type == "step_completed":
                step_id = data.get("step_id")
                duration = data.get("duration_seconds", 0)
                step_durations[step_id] = duration
                completed_steps.append(step_id)

            elif event_type == "step_failed":
                failed_steps.append(data.get("step_id"))

            elif event_type == "debug_started":
                debug_count += 1

            elif event_type == "escalated_to_orchestrator":
                escalation_count += 1
                human_interventions += 1

            elif event_type == "human_intervention":
                human_interventions += 1

        total_duration = None
        if start_ts and end_ts:
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
                total_duration = (t1 - t0).total_seconds()
            except Exception:
                pass

        success = any(ev.get("event_type") == "workflow_completed" for ev in events)

        # 评分计算
        scores = self._compute_scores(
            success=success,
            total_duration=total_duration,
            step_count=len(completed_steps),
            debug_count=debug_count,
            escalation_count=escalation_count,
            human_interventions=human_interventions,
        )

        return {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "evaluated_at": utc_now(),
            "success": success,
            "metrics": {
                "total_duration_seconds": total_duration,
                "completed_steps": len(completed_steps),
                "failed_steps": len(failed_steps),
                "debug_interventions": debug_count,
                "escalations": escalation_count,
                "human_interventions": human_interventions,
                "step_durations": step_durations,
            },
            "scores": scores,
            "recommendations": [],  # 填充于 _generate_improvements
        }

    def _compute_scores(self, success: bool, total_duration: Optional[float],
                        step_count: int, debug_count: int, escalation_count: int,
                        human_interventions: int) -> dict:
        """计算四个维度的评分（0-10）"""

        # 稳定分：基于成功率和 debug 次数
        if not success:
            stability = 0.0
        elif debug_count == 0:
            stability = 10.0
        elif debug_count == 1:
            stability = 7.0
        elif debug_count == 2:
            stability = 4.0
        else:
            stability = 2.0

        # 效率分：基于耗时（没有基准时给 5 分）
        efficiency = 5.0  # 暂无基准，给中等分

        # 介入分：越少越好
        if human_interventions == 0:
            intervention_score = 10.0
        elif human_interventions == 1:
            intervention_score = 8.0
        elif human_interventions <= 3:
            intervention_score = 5.0
        else:
            intervention_score = max(0, 10 - human_interventions * 2)

        # 质量分：暂无自动评测，给默认分
        quality = 7.0  # 需要人工或后续工作流反馈

        overall = (stability + efficiency + intervention_score + quality) / 4

        return {
            "stability": round(stability, 1),
            "efficiency": round(efficiency, 1),
            "intervention": round(intervention_score, 1),
            "quality": round(quality, 1),
            "overall": round(overall, 1),
        }

    # =========================================================================
    # 改进建议生成
    # =========================================================================

    def _generate_improvements(self, run_id: str, events: list[dict], report: dict) -> list[dict]:
        improvements = []
        metrics = report["metrics"]

        # 规则1：debug 次数过多
        if metrics["debug_interventions"] >= 2:
            failed = [ev["data"]["step_id"] for ev in events if ev.get("event_type") == "debug_started"]
            improvements.append({
                "id": f"imp-{run_id[:8]}-001",
                "class": "B",
                "target": report["workflow_id"],
                "title": f"步骤 {failed[0] if failed else '未知'} 需要优化",
                "problem": f"本次运行触发了 {metrics['debug_interventions']} 次 debug agent 介入",
                "proposed_change": "检查该步骤的 prompt 是否过于复杂，考虑拆分或切换更强的 model",
                "expected_benefit": "减少 debug 次数，提升稳定分",
                "evidence": [{"run_id": run_id, "debug_count": metrics["debug_interventions"]}],
                "created_at": utc_now(),
                "created_by": "evaluator",
            })

        # 规则2：有步骤失败且工作流成功（恢复了）
        if metrics["failed_steps"] > 0 and report["success"]:
            improvements.append({
                "id": f"imp-{run_id[:8]}-002",
                "class": "C",
                "target": report["workflow_id"],
                "title": "工作流有步骤失败后恢复，建议预防",
                "problem": f"{metrics['failed_steps']} 个步骤失败后通过 debug agent 恢复",
                "proposed_change": "分析失败步骤的 prompt 和 model 配置，针对性优化",
                "expected_benefit": "提升首次成功率，降低总耗时",
                "evidence": [{"run_id": run_id}],
                "created_at": utc_now(),
                "created_by": "evaluator",
            })

        return improvements

    def _save_improvements(self, improvements: list[dict]):
        """将新的改进建议追加到 pending.yaml"""
        pending_file = KB_PATH / "improvements" / "pending.yaml"
        try:
            if pending_file.exists():
                data = yaml.safe_load(pending_file.read_text(encoding="utf-8")) or {}
            else:
                data = {}
            existing = data.get("improvements", [])
            existing.extend(improvements)
            data["improvements"] = existing
            data["last_updated"] = utc_now()
            pending_file.write_text(
                yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8"
            )
            self.logger.info(f"[Evaluator] 已添加 {len(improvements)} 条改进建议到 pending.yaml")
        except Exception as e:
            self.logger.error(f"[Evaluator] 保存改进建议失败: {e}")

    # =========================================================================
    # 评分记录
    # =========================================================================

    def _record_score(self, report: dict):
        """将评分记录追加到 scores.jsonl"""
        scores_file = KB_PATH / "workflow_scores" / "scores.jsonl"
        record = {
            "run_id": report["run_id"],
            "workflow_id": report["workflow_id"],
            "ts": report["evaluated_at"],
            "success": report["success"],
            "scores": report["scores"],
            "metrics": {
                k: v for k, v in report["metrics"].items()
                if k != "step_durations"  # step_durations 太详细，不写入
            },
        }
        try:
            with open(scores_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            self.logger.error(f"[Evaluator] 记录评分失败: {e}")

    # =========================================================================
    # 模式更新（积累足够数据后触发）
    # =========================================================================

    def _maybe_update_patterns(self, events: list[dict], report: dict):
        """当有足够运行数据时，更新成功/失败模式库"""
        scores_file = KB_PATH / "workflow_scores" / "scores.jsonl"
        if not scores_file.exists():
            return
        # 简单统计：当同一 workflow 有 5+ 次运行时触发分析
        try:
            lines = scores_file.read_text(encoding="utf-8").strip().splitlines()
            workflow_id = report["workflow_id"]
            count = sum(
                1 for line in lines
                if json.loads(line).get("workflow_id") == workflow_id
            )
            if count >= 5 and count % 5 == 0:
                self.logger.info(
                    f"[Evaluator] {workflow_id} 已运行 {count} 次，触发模式分析 (TODO: LLM 分析)"
                )
                # TODO: 调用 LLM（claude sonnet）分析 scores.jsonl，更新 successful.yaml
        except Exception:
            pass


# =============================================================================
# CLI 入口：评估指定 run_id
# =============================================================================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if len(sys.argv) < 2:
        print("用法: python evaluator.py <run_id>")
        sys.exit(1)

    evaluator = FlowEvaluator()
    report = evaluator.evaluate_run(sys.argv[1])
    print(json.dumps(report, ensure_ascii=False, indent=2))
