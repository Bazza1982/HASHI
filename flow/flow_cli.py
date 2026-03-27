#!/usr/bin/env python3
"""
HASHI Flow CLI — 工作流命令行管理工具

用法:
    python flow/flow_cli.py run <workflow.yaml> [选项]
    python flow/flow_cli.py status <run_id>
    python flow/flow_cli.py list
    python flow/flow_cli.py eval <run_id>
    python flow/flow_cli.py resume <run_id>

示例:
    python flow/flow_cli.py run flow/workflows/examples/meta_workflow_creation.yaml
    python flow/flow_cli.py run flow/workflows/library/book_translation.yaml --prefill answers.json
    python flow/flow_cli.py status run-book-translation-20260326-062329
    python flow/flow_cli.py list
"""

import argparse
import json
import sys
from pathlib import Path

# 确保 hashi root 在 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flow.engine.flow_runner import FlowRunner
from flow.engine.preflight import PreFlightCollector, load_prefill_from_file
from flow.engine.task_state import TaskState


# =============================================================================
# 子命令：run
# =============================================================================

def cmd_run(args):
    workflow_path = args.workflow

    if not Path(workflow_path).exists():
        print(f"❌ 工作流文件不存在: {workflow_path}")
        sys.exit(1)

    # 初始化 runner（此时只加载 YAML，不执行）
    runner = FlowRunner(workflow_path)
    wf = runner.workflow
    wf_name = wf.get("workflow", {}).get("name", workflow_path)

    print(f"\n🚀 HASHI Flow — 准备运行: {wf_name}")
    print(f"   Run ID: {runner.run_id}")

    # 加载预填充答案（如果有）
    prefill = {}
    if args.prefill:
        prefill = load_prefill_from_file(args.prefill)
        print(f"   预填充答案: {args.prefill} ({len(prefill)} 项)")

    # Pre-flight 收集
    collector = PreFlightCollector(
        workflow=wf,
        prefill=prefill,
        silent=args.silent,
    )
    pre_flight_data = collector.run()

    if pre_flight_data:
        runner.set_pre_flight_data(pre_flight_data)

    # 确认运行
    if not args.yes and not args.silent:
        steps = wf.get("steps", [])
        print(f"\n📋 工作流将执行 {len(steps)} 个步骤:")
        for step in steps:
            deps = step.get("depends", [])
            dep_str = f" (需要: {', '.join(deps)})" if deps else ""
            strategy = " [并行]" if step.get("strategy") == "parallel" else ""
            print(f"   • {step['id']}: {step.get('name', step['id'])}{dep_str}{strategy}")
        print()
        try:
            confirm = input("确认运行？[Y/n]: ").strip().lower()
            if confirm and confirm != "y":
                print("已取消。")
                sys.exit(0)
        except EOFError:
            pass  # 非交互环境，直接运行

    # 执行工作流
    print(f"\n▶️  开始执行...\n")
    result = runner.start()

    # 输出结果
    print()
    if result.get("success"):
        print(f"✅ 工作流完成！")
        completed = result.get("completed_steps", [])
        print(f"   完成步骤: {', '.join(completed)}")
    else:
        print(f"❌ 工作流失败: {result.get('error', '未知错误')}")
        failed = result.get("failed_steps", [])
        if failed:
            print(f"   失败步骤: {', '.join(failed)}")

    print(f"   Run ID: {result.get('run_id')}")
    print(f"   日志目录: flow/runs/{result.get('run_id')}/logs/")

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   结果已保存: {args.output}")

    sys.exit(0 if result.get("success") else 1)


# =============================================================================
# 子命令：status
# =============================================================================

def cmd_status(args):
    run_id = args.run_id
    runs_dir = ROOT / "flow" / "runs" / run_id

    if not runs_dir.exists():
        print(f"❌ Run 不存在: {run_id}")
        sys.exit(1)

    state = TaskState(run_id)
    status = state.get_full_status()

    print(f"\n📊 Run 状态: {run_id}")
    print(f"   工作流: {status.get('workflow_id', '未知')}")
    print(f"   状态: {status.get('workflow_status', '未知')}")
    print(f"   创建时间: {status.get('created_at', '未知')}")
    print(f"   更新时间: {status.get('updated_at', '未知')}")

    steps = status.get("steps", {})
    if steps:
        print(f"\n   步骤状态:")
        for step_id, step_info in steps.items():
            s = step_info.get("status", "unknown")
            icon = {"completed": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}.get(s, "❓")
            print(f"   {icon} {step_id}: {s}")

    # 检查是否有评估报告
    scores_file = ROOT / "flow" / "evaluation_kb" / "workflow_scores" / "scores.jsonl"
    if scores_file.exists():
        for line in scores_file.read_text(encoding="utf-8").strip().splitlines():
            try:
                record = json.loads(line)
                if record.get("run_id") == run_id:
                    scores = record.get("scores", {})
                    print(f"\n   📈 评估评分:")
                    print(f"   稳定分: {scores.get('stability')}/10")
                    print(f"   效率分: {scores.get('efficiency')}/10")
                    print(f"   介入分: {scores.get('intervention')}/10")
                    print(f"   综合分: {scores.get('overall')}/10")
                    break
            except json.JSONDecodeError:
                pass


# =============================================================================
# 子命令：list
# =============================================================================

def cmd_list(args):
    runs_dir = ROOT / "flow" / "runs"
    if not runs_dir.exists():
        print("暂无运行记录。")
        return

    runs = sorted(runs_dir.iterdir(), reverse=True)
    if not runs:
        print("暂无运行记录。")
        return

    print(f"\n{'Run ID':<45} {'状态':<12} {'工作流'}")
    print("-" * 80)

    shown = 0
    for run_dir in runs:
        if not run_dir.is_dir():
            continue
        state_file = run_dir / "state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                wf_id = data.get("workflow_id", "未知")
                wf_status = data.get("workflow_status", "未知")
                icon = {"completed": "✅", "failed": "❌", "running": "🔄", "created": "🆕"}.get(wf_status, "❓")
                print(f"{run_dir.name:<45} {icon} {wf_status:<10} {wf_id}")
                shown += 1
                if args.limit and shown >= args.limit:
                    remaining = len(runs) - shown
                    if remaining > 0:
                        print(f"... 还有 {remaining} 条记录（使用 --limit 调整）")
                    break
            except Exception:
                pass

    if shown == 0:
        print("暂无有效运行记录。")


# =============================================================================
# 子命令：eval
# =============================================================================

def cmd_eval(args):
    from flow.agents.evaluator.evaluator import FlowEvaluator

    run_id = args.run_id
    print(f"\n🔍 评估 run: {run_id}")

    evaluator = FlowEvaluator()
    report = evaluator.evaluate_run(run_id)

    print(f"\n📊 评估报告")
    print(f"   工作流: {report.get('workflow_id')}")
    print(f"   结果: {'✅ 成功' if report.get('success') else '❌ 失败'}")
    print(f"   评估时间: {report.get('evaluated_at')}")

    metrics = report.get("metrics", {})
    print(f"\n   📈 指标:")
    print(f"   总耗时: {metrics.get('total_duration_seconds', 'N/A')}s")
    print(f"   完成步骤: {metrics.get('completed_steps', 0)}")
    print(f"   失败步骤: {metrics.get('failed_steps', 0)}")
    print(f"   Debug 次数: {metrics.get('debug_interventions', 0)}")
    print(f"   人工介入: {metrics.get('human_interventions', 0)}")

    scores = report.get("scores", {})
    print(f"\n   ⭐ 评分:")
    print(f"   稳定分: {scores.get('stability')}/10")
    print(f"   效率分: {scores.get('efficiency')}/10")
    print(f"   介入分: {scores.get('intervention')}/10")
    print(f"   质量分: {scores.get('quality')}/10")
    print(f"   综合分: {scores.get('overall')}/10")

    if args.json:
        print(f"\n{json.dumps(report, ensure_ascii=False, indent=2)}")


# =============================================================================
# 子命令：resume（暂未实现完整逻辑，预留接口）
# =============================================================================

def cmd_resume(args):
    run_id = args.run_id
    print(f"⚠️  Resume 功能尚在开发中。")
    print(f"   Run ID: {run_id}")
    print(f"   当前可手动检查状态: python flow/flow_cli.py status {run_id}")


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="hashi-flow",
        description="HASHI Flow — 工作流管理 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="运行工作流")
    p_run.add_argument("workflow", help="工作流 YAML 路径")
    p_run.add_argument("--prefill", "-p", help="预填充答案 JSON 文件路径")
    p_run.add_argument("--silent", "-s", action="store_true", help="静默模式（使用所有默认值）")
    p_run.add_argument("--yes", "-y", action="store_true", help="跳过运行确认")
    p_run.add_argument("--output", "-o", help="将运行结果保存到 JSON 文件")
    p_run.set_defaults(func=cmd_run)

    # status
    p_status = sub.add_parser("status", help="查看运行状态")
    p_status.add_argument("run_id", help="Run ID")
    p_status.set_defaults(func=cmd_status)

    # list
    p_list = sub.add_parser("list", help="列出所有运行记录")
    p_list.add_argument("--limit", "-n", type=int, default=20, help="最多显示条数（默认20）")
    p_list.set_defaults(func=cmd_list)

    # eval
    p_eval = sub.add_parser("eval", help="评估指定 run 的执行质量")
    p_eval.add_argument("run_id", help="Run ID")
    p_eval.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    p_eval.set_defaults(func=cmd_eval)

    # resume
    p_resume = sub.add_parser("resume", help="恢复暂停的工作流")
    p_resume.add_argument("run_id", help="Run ID")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
