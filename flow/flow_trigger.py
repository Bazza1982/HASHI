#!/usr/bin/env python3
"""
HASHI Flow Trigger — 供 Orchestrator Agent 调用的非阻塞启动器

用法（akane 在 claude-cli 中执行）:
    # 启动工作流（后台运行，立即返回 run_id）
    python flow/flow_trigger.py start book_translation '{"source_path": "...", "output_path": "..."}'

    # 查看状态
    python flow/flow_trigger.py status run-book-translation-20260326-071229

    # 列出运行中的工作流
    python flow/flow_trigger.py list

    # 等待完成并获取结果（用于可以阻塞等待的场景）
    python flow/flow_trigger.py wait run-book-translation-xxx --timeout 3600
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOW_DIR = ROOT / "flow"
RUNS_DIR = FLOW_DIR / "runs"

# 预定义工作流别名
WORKFLOW_ALIASES = {
    "book_translation":       "flow/workflows/library/book_translation.yaml",
    "meta":                   "flow/workflows/examples/meta_workflow_creation.yaml",
    "smoke_test":             "flow/workflows/examples/smoke_test.yaml",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# start — 后台启动工作流
# =============================================================================

def cmd_start(args):
    # 解析工作流路径
    workflow_alias = args.workflow
    workflow_path = WORKFLOW_ALIASES.get(workflow_alias, workflow_alias)
    full_path = ROOT / workflow_path

    if not full_path.exists():
        print(json.dumps({"ok": False, "error": f"工作流不存在: {workflow_path}"}))
        sys.exit(1)

    # 解析 prefill
    prefill_data = {}
    if args.prefill_json:
        try:
            prefill_data = json.loads(args.prefill_json)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": f"prefill JSON 解析失败: {e}"}))
            sys.exit(1)

    # 写入临时 prefill 文件
    prefill_file = None
    if prefill_data:
        fd, prefill_file = tempfile.mkstemp(suffix=".json", prefix="hashi_flow_prefill_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prefill_data, f, ensure_ascii=False)

    # 构建命令
    cmd = [
        sys.executable,
        str(FLOW_DIR / "flow_cli.py"),
        "run", str(full_path),
        "--yes", "--silent",
    ]
    if prefill_file:
        cmd += ["--prefill", prefill_file]

    # 移除嵌套 Claude 会话检测（同 worker_dispatcher.py）
    child_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    # 后台启动
    log_dir = RUNS_DIR / "_trigger_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"trigger-{workflow_alias}-{ts}.log"

    with open(log_file, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            cwd=str(ROOT),
            env=child_env,
            start_new_session=True,  # 脱离当前进程组，真正后台运行
        )

    # 记录触发信息到 trigger registry
    # run_id 格式由 flow_cli.py 生成，我们从 YAML 推导一个预期 run_id
    import yaml
    with open(full_path) as f:
        wf_data = yaml.safe_load(f)
    wf_id = wf_data.get("workflow", {}).get("id", "unknown")
    expected_run_prefix = f"run-{wf_id}-{ts}"

    trigger_info = {
        "workflow": workflow_alias,
        "workflow_path": str(full_path),
        "expected_run_prefix": expected_run_prefix,
        "pid": proc.pid,
        "started_at": utc_now(),
        "prefill": prefill_data,
        "log_file": str(log_file),
        "status": "started",
    }
    registry_file = RUNS_DIR / "_trigger_registry.jsonl"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with open(registry_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(trigger_info, ensure_ascii=False) + "\n")

    print(json.dumps({
        "ok": True,
        "pid": proc.pid,
        "expected_run_prefix": expected_run_prefix,
        "workflow": workflow_alias,
        "log_file": str(log_file),
        "message": f"工作流已在后台启动（PID {proc.pid}），可用 status 命令查询进度",
    }, ensure_ascii=False, indent=2))


# =============================================================================
# status — 查询运行状态
# =============================================================================

def cmd_status(args):
    run_id = args.run_id

    # 支持前缀匹配（不需要完整 run_id）
    matched_run_id = _resolve_run_id(run_id)
    if not matched_run_id:
        print(json.dumps({"ok": False, "error": f"找不到 run: {run_id}"}))
        sys.exit(1)

    state_file = RUNS_DIR / matched_run_id / "state.json"
    if not state_file.exists():
        print(json.dumps({"ok": True, "run_id": matched_run_id, "status": "initializing"}))
        return

    state = json.loads(state_file.read_text(encoding="utf-8"))
    workflow_status = state.get("workflow_status", "unknown")

    # 统计步骤
    steps = state.get("steps", {})
    step_summary = {}
    for sid, sdata in steps.items():
        s = sdata.get("status", "unknown")
        step_summary[sid] = s

    # 读取评分（如果已完成）
    scores = None
    if workflow_status in ("completed", "failed"):
        scores = _get_scores(matched_run_id)

    result = {
        "ok": True,
        "run_id": matched_run_id,
        "workflow_id": state.get("workflow_id"),
        "status": workflow_status,
        "steps": step_summary,
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
    }
    if scores:
        result["scores"] = scores

    print(json.dumps(result, ensure_ascii=False, indent=2))


# =============================================================================
# list — 列出运行中的工作流
# =============================================================================

def cmd_list(args):
    if not RUNS_DIR.exists():
        print(json.dumps({"ok": True, "runs": []}))
        return

    runs = []
    for run_dir in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not run_dir.is_dir() or run_dir.name.startswith("_"):
            continue
        state_file = run_dir / "state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                runs.append({
                    "run_id": run_dir.name,
                    "workflow_id": state.get("workflow_id"),
                    "status": state.get("workflow_status"),
                    "updated_at": state.get("updated_at"),
                })
            except Exception:
                pass
        if args.limit and len(runs) >= args.limit:
            break

    print(json.dumps({"ok": True, "count": len(runs), "runs": runs}, ensure_ascii=False, indent=2))


# =============================================================================
# wait — 等待运行完成（阻塞，带超时）
# =============================================================================

def cmd_wait(args):
    run_id = args.run_id
    timeout = args.timeout
    poll_interval = 5

    deadline = time.time() + timeout
    while time.time() < deadline:
        matched = _resolve_run_id(run_id)
        if matched:
            state_file = RUNS_DIR / matched / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                status = state.get("workflow_status")
                if status in ("completed", "failed", "aborted"):
                    scores = _get_scores(matched)
                    print(json.dumps({
                        "ok": True,
                        "run_id": matched,
                        "status": status,
                        "scores": scores,
                    }, ensure_ascii=False, indent=2))
                    sys.exit(0 if status == "completed" else 1)
        time.sleep(poll_interval)

    print(json.dumps({"ok": False, "error": f"等待超时 ({timeout}s)", "run_id": run_id}))
    sys.exit(1)


# =============================================================================
# 辅助函数
# =============================================================================

def _resolve_run_id(run_id_or_prefix: str) -> str | None:
    """支持前缀匹配，返回完整 run_id"""
    if not RUNS_DIR.exists():
        return None
    exact = RUNS_DIR / run_id_or_prefix
    if exact.exists():
        return run_id_or_prefix
    # 前缀匹配
    matches = [d.name for d in RUNS_DIR.iterdir()
               if d.is_dir() and d.name.startswith(run_id_or_prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # 返回最新的
        return sorted(matches)[-1]
    return None


def _get_scores(run_id: str) -> dict | None:
    scores_file = FLOW_DIR / "evaluation_kb" / "workflow_scores" / "scores.jsonl"
    if not scores_file.exists():
        return None
    for line in reversed(scores_file.read_text(encoding="utf-8").strip().splitlines()):
        try:
            record = json.loads(line)
            if record.get("run_id") == run_id:
                return record.get("scores")
        except json.JSONDecodeError:
            pass
    return None


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(prog="flow_trigger", description="HASHI Flow 触发器")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="后台启动工作流")
    p_start.add_argument("workflow", help=f"工作流别名或 YAML 路径。别名: {list(WORKFLOW_ALIASES.keys())}")
    p_start.add_argument("prefill_json", nargs="?", default="{}", help='prefill JSON 字符串，如 \'{"source_path": "..."}\' ')
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status", help="查询运行状态")
    p_status.add_argument("run_id", help="Run ID（支持前缀）")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="列出运行记录")
    p_list.add_argument("--limit", "-n", type=int, default=10)
    p_list.set_defaults(func=cmd_list)

    p_wait = sub.add_parser("wait", help="等待运行完成（阻塞）")
    p_wait.add_argument("run_id", help="Run ID（支持前缀）")
    p_wait.add_argument("--timeout", type=int, default=3600, help="最长等待秒数")
    p_wait.set_defaults(func=cmd_wait)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
