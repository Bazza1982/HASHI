#!/usr/bin/env python3
"""
Nagare CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parent.parent
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != SCRIPT_DIR]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nagare.engine.preflight import PreFlightCollector, load_prefill_from_file
from nagare.engine.runner import FlowRunner
from nagare.engine.state import TaskState
from nagare.handlers.deterministic_handler import DeterministicStepHandler

RUNS_ROOT = ROOT / "flow" / "runs"


def cmd_run(args):
    workflow_path = args.workflow

    if not Path(workflow_path).exists():
        print(f"❌ 工作流文件不存在: {workflow_path}")
        sys.exit(1)

    step_handler = None
    if args.smoke_handler:
        step_handler = DeterministicStepHandler(runs_root=RUNS_ROOT)

    runner = FlowRunner(
        workflow_path,
        runs_root=RUNS_ROOT,
        repo_root=ROOT,
        step_handler=step_handler,
    )
    wf = runner.workflow
    wf_name = wf.get("workflow", {}).get("name", workflow_path)

    print(f"\n🚀 Nagare — 准备运行: {wf_name}")
    print(f"   Run ID: {runner.run_id}")

    prefill = {}
    if args.prefill:
        prefill = load_prefill_from_file(args.prefill)
        print(f"   预填充答案: {args.prefill} ({len(prefill)} 项)")

    runner.event_logger.emit(
        "run.preflight.started",
        component="cli",
        message="Starting pre-flight collection",
        data={"prefill_path": args.prefill, "silent": args.silent},
    )
    collector = PreFlightCollector(workflow=wf, prefill=prefill, silent=args.silent)
    pre_flight_data = collector.run()

    if pre_flight_data:
        runner.set_pre_flight_data(pre_flight_data)

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
                runner.event_logger.emit(
                    "run.cancelled",
                    component="cli",
                    message="Workflow run cancelled before confirmation",
                    data={"reason": "user_declined_confirmation"},
                )
                sys.exit(0)
        except EOFError:
            pass
    runner.event_logger.emit(
        "run.confirmed",
        component="cli",
        message="Workflow run confirmed by CLI",
        data={"silent": args.silent, "yes": args.yes},
    )

    print(f"\n▶️  开始执行...\n")
    result = runner.start()

    print()
    if result.get("success"):
        print("✅ 工作流完成！")
        completed = result.get("completed_steps", [])
        print(f"   完成步骤: {', '.join(completed)}")
    else:
        print(f"❌ 工作流失败: {result.get('error', '未知错误')}")
        failed = result.get("failed_steps", [])
        if failed:
            print(f"   失败步骤: {', '.join(failed)}")

    print(f"   Run ID: {result.get('run_id')}")
    print(f"   日志目录: {RUNS_ROOT / str(result.get('run_id')) / 'logs'}")

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   结果已保存: {args.output}")

    sys.exit(0 if result.get("success") else 1)


def cmd_status(args):
    run_id = args.run_id
    runs_dir = RUNS_ROOT / run_id

    if not runs_dir.exists():
        print(f"❌ Run 不存在: {run_id}")
        sys.exit(1)

    state = TaskState(run_id, runs_root=RUNS_ROOT)
    status = state.get_full_status()
    snapshot = state.get_runtime_snapshot()

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
    if snapshot.get("current_steps"):
        print(f"\n   当前运行步骤: {', '.join(snapshot['current_steps'])}")


def cmd_list(args):
    if not RUNS_ROOT.exists():
        print("暂无运行记录。")
        return

    runs = sorted(RUNS_ROOT.iterdir(), reverse=True)
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
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            continue
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

    if shown == 0:
        print("暂无有效运行记录。")


def cmd_resume(args):
    run_id = args.run_id
    print("⚠️  Resume 功能尚在开发中。")
    print(f"   Run ID: {run_id}")
    print(f"   当前可手动检查状态: nagare status {run_id}")


def cmd_eval(args):
    print("⚠️  eval 依赖宿主应用提供可选 Evaluator 适配器；nagare-core 默认不包含该实现。")
    print(f"   Run ID: {args.run_id}")


def cmd_api(args):
    from nagare.api.app import serve

    serve(host=args.host, port=args.port, runs_root=args.runs_root)


def main():
    parser = argparse.ArgumentParser(
        prog="nagare",
        description="Nagare workflow CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="运行工作流")
    p_run.add_argument("workflow", help="工作流 YAML 路径")
    p_run.add_argument("--prefill", "-p", help="预填充答案 JSON 文件路径")
    p_run.add_argument("--silent", "-s", action="store_true", help="静默模式（使用所有默认值）")
    p_run.add_argument("--yes", "-y", action="store_true", help="跳过运行确认")
    p_run.add_argument("--output", "-o", help="将运行结果保存到 JSON 文件")
    p_run.add_argument(
        "--smoke-handler",
        action="store_true",
        help="使用确定性本地 handler 运行，用于包安装/CI 冒烟验证",
    )
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="查看运行状态")
    p_status.add_argument("run_id", help="Run ID")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="列出所有运行记录")
    p_list.add_argument("--limit", "-n", type=int, default=20, help="最多显示条数（默认20）")
    p_list.set_defaults(func=cmd_list)

    p_eval = sub.add_parser("eval", help="评估指定 run 的执行质量")
    p_eval.add_argument("run_id", help="Run ID")
    p_eval.set_defaults(func=cmd_eval)

    p_resume = sub.add_parser("resume", help="恢复暂停的工作流")
    p_resume.add_argument("run_id", help="Run ID")
    p_resume.set_defaults(func=cmd_resume)

    p_api = sub.add_parser("api", help="启动只读运行观察 API")
    p_api.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_api.add_argument("--port", type=int, default=8787, help="监听端口")
    p_api.add_argument("--runs-root", default=str(RUNS_ROOT), help="运行目录根路径")
    p_api.set_defaults(func=cmd_api)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
