#!/usr/bin/env python3
"""
Nagare Workflow Watchdog
主动检查所有运行中的工作流，检测完成/失败/卡死，通过 HChat 通知 Orchestrator。

Usage:
    python flow/scripts/workflow_watchdog.py [--notify <agent>] [--quiet]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RUNS_DIR = ROOT / "flow" / "runs"
REGISTRY = RUNS_DIR / "_trigger_registry.jsonl"

STUCK_THRESHOLD_SECONDS = 300   # 默认：步骤超过 5 分钟无日志更新视为卡死
STALE_THRESHOLD_SECONDS = 3600  # run 超过 1 小时未完成，告警

# 特定步骤的自定义超时（这些步骤正常运行时间较长，不应误判为"卡死"）
STEP_TIMEOUT_OVERRIDES = {
    "human_approval": 1800,      # 等待人工确认最多 30 分钟
    "translate_first_half": 1800,
    "translate_second_half": 1800,
    "review_and_merge": 1800,
    "design_workflow": 600,
    "evaluate_and_improve": 600,
    "apply_improvement": 600,
}


def local_now() -> datetime:
    return datetime.now()


def parse_log_last_event(log_path: Path) -> tuple[str, float]:
    """返回 (最后一条日志内容, 距今秒数)"""
    if not log_path.exists():
        return "", float("inf")
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if line:
                # 解析时间戳：2026-03-26 11:01:10,676 [INFO] ...
                # 日志时间戳是本地时间（无时区标记），用本地时间比较
                try:
                    ts_str = line[:23]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    age = (local_now() - ts).total_seconds()
                    return line, age
                except ValueError:
                    return line, float("inf")
    except Exception:
        pass
    return "", float("inf")


def get_run_status(run_id: str) -> dict:
    """检查单个 run 的当前状态"""
    run_dir = RUNS_DIR / run_id
    log_file = run_dir / "logs" / "flow_runner.log"
    state_file = run_dir / "state.json"

    result = {
        "run_id": run_id,
        "status": "unknown",
        "last_event": "",
        "last_event_age_s": float("inf"),
        "current_step": None,
        "completed_steps": [],
        "failed_steps": [],
        "alerts": [],
    }

    if not log_file.exists():
        result["status"] = "starting"
        result["alerts"].append("run 目录已创建但日志未生成")
        return result

    # 读取 state.json
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            wf_status = state.get("workflow_status", "unknown")
            steps = state.get("steps", {})

            result["completed_steps"] = [k for k, v in steps.items() if v.get("status") == "completed"]
            result["failed_steps"] = [k for k, v in steps.items() if v.get("status") == "failed"]
            running = [k for k, v in steps.items() if v.get("status") == "running"]
            result["current_step"] = running[0] if running else None

            if wf_status == "completed":
                result["status"] = "completed"
                return result
            elif wf_status == "failed":
                result["status"] = "failed"
                return result
            else:
                result["status"] = "running"
        except Exception:
            pass

    # 从日志推断状态
    last_line, age = parse_log_last_event(log_file)
    result["last_event"] = last_line
    result["last_event_age_s"] = age

    if "工作流完成 ✅" in last_line:
        result["status"] = "completed"
    elif "工作流失败 ❌" in last_line or "未预期错误" in last_line:
        result["status"] = "failed"
    elif result["status"] == "running":
        current = result["current_step"] or "?"
        threshold = STEP_TIMEOUT_OVERRIDES.get(current, STUCK_THRESHOLD_SECONDS)
        if age > threshold:
            result["alerts"].append(f"⚠️ 步骤 {current} 已 {int(age)}s 无活动（阈值 {threshold}s），可能卡死")
    elif age > STALE_THRESHOLD_SECONDS:
        result["alerts"].append(f"⚠️ run 已运行 {int(age//60)} 分钟，超过预期")

    return result


def load_active_runs() -> list[dict]:
    """从 registry 加载所有仍在运行中的 run"""
    if not REGISTRY.exists():
        return []

    seen_run_dirs = set()
    active = []

    try:
        lines = REGISTRY.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        prefix = entry.get("expected_run_prefix", "")
        if not prefix:
            continue

        # 找到实际 run 目录（精确匹配）
        run_dir = RUNS_DIR / prefix
        if not run_dir.exists():
            # 尝试模糊匹配（prefix 可能与实际目录名有细微差异）
            matches = [d for d in RUNS_DIR.iterdir() if d.is_dir() and d.name == prefix]
            if not matches:
                continue
            run_dir = matches[0]

        if run_dir.name in seen_run_dirs:
            continue
        seen_run_dirs.add(run_dir.name)

        status = get_run_status(run_dir.name)
        if status["status"] not in ("completed", "failed", "unknown"):
            # 运行中：检查是否真正活跃（24 小时内有活动）
            _, age = parse_log_last_event(run_dir / "logs" / "flow_runner.log")
            if age < 86400:  # 24 小时内
                active.append(status)
        elif status["status"] in ("completed", "failed"):
            # 已完成/失败：只报告最近 5 分钟内的
            _, age = parse_log_last_event(run_dir / "logs" / "flow_runner.log")
            if age < 300:
                active.append(status)

    return active


def format_report(runs: list[dict]) -> str:
    if not runs:
        return "✅ 当前没有运行中的工作流。"

    lines = [f"📊 Nagare 工作流状态报告 ({len(runs)} 个运行中)\n"]

    for r in runs:
        run_id = r["run_id"]
        status = r["status"]
        emoji = {"running": "🔄", "completed": "✅", "failed": "❌", "starting": "⏳"}.get(status, "❓")

        lines.append(f"{emoji} {run_id}")
        if r["current_step"]:
            lines.append(f"   当前步骤: {r['current_step']}")
        if r["completed_steps"]:
            lines.append(f"   已完成: {', '.join(r['completed_steps'])}")
        if r["failed_steps"]:
            lines.append(f"   已失败: {', '.join(r['failed_steps'])}")
        if r["last_event"]:
            age = r["last_event_age_s"]
            age_str = f"{int(age)}s 前" if age < 3600 else f"{int(age//60)}min 前"
            lines.append(f"   最后活动: {age_str}")
        for alert in r["alerts"]:
            lines.append(f"   {alert}")
        lines.append("")

    return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser(description="Nagare Workflow Watchdog")
    parser.add_argument("--notify", default="akane", help="收到告警时通知的 agent（默认 akane）")
    parser.add_argument("--quiet", action="store_true", help="只在有告警或完成时发送通知")
    parser.add_argument("--from-agent", default="nagare-watchdog", help="发送方 agent 名")
    args = parser.parse_args()

    runs = load_active_runs()

    # --quiet 模式 + 无活跃 run → 静默退出，不产生任何输出
    if args.quiet and not runs:
        sys.exit(0)

    report = format_report(runs)

    # 判断是否需要发送通知
    has_alerts = any(r["alerts"] for r in runs)
    has_completed = any(r["status"] == "completed" for r in runs)
    has_failed = any(r["status"] == "failed" for r in runs)
    needs_notify = has_alerts or has_completed or has_failed or not args.quiet

    print(report)

    if needs_notify and runs:
        try:
            sys.path.insert(0, str(ROOT))
            from tools.hchat_send import send_hchat
            send_hchat(
                to_agent=args.notify,
                from_agent=args.from_agent,
                text=report,
            )
        except Exception as e:
            print(f"HChat 通知失败: {e}", file=sys.stderr)

    # 退出码：1 = 有告警，0 = 正常
    sys.exit(1 if has_alerts or has_failed else 0)


if __name__ == "__main__":
    main()
