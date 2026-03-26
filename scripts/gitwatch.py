#!/usr/bin/env python3
"""
gitwatch.py — 增量式 Git 安全巡查脚本
每次运行检查三个 HASHI 实例的新增未追踪文件和最近 remote 提交。
自动修复明显问题，不确定时生成报告等待指示。
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─── 配置 ───────────────────────────────────────────────────────────────────

INSTANCES = [
    {"name": "HASHI1", "path": "/home/lily/projects/hashi"},
    {"name": "HASHI2", "path": "/home/lily/projects/hashi2"},
    {"name": "HASHI9", "path": "/mnt/c/Users/thene/projects/HASHI"},
]

STATE_FILE = Path("/home/lily/projects/hashi2/scripts/.gitwatch_state.json")
REPORT_FILE = Path("/home/lily/projects/hashi2/scripts/gitwatch_report.md")

# 明确危险 → 自动加入 .gitignore（不推送，只本地标记）
AUTO_IGNORE_PATTERNS = [
    # 凭证与密钥
    ("credentials", r"credentials[\._]", "明确凭证文件"),
    ("api_key_file", r"api[_-]?key", "API key 文件"),
    ("token_file", r"\.token$|auth_token", "token 文件"),
    ("env_file", r"^\.env(\.|$)", ".env 配置文件"),
    ("secrets_dir", r"^secrets/", "secrets 目录"),
    # 运行时产物
    ("pid_file", r"\.pid$", "进程 ID 文件"),
    ("lock_file", r"\.lock$", "锁文件"),
    ("log_file", r"\.log$", "日志文件"),
    ("sqlite_file", r"\.sqlite(-shm|-wal)?$", "SQLite 数据库文件"),
    # 已知私有目录
    ("mailbox", r"^mailbox/", "内部通讯目录"),
    ("flow_runs", r"^flow/runs/", "Flow 运行记录"),
    ("flow_lib", r"^flow/workflows/library/", "Flow 私有工作流"),
    ("sessions", r"workspaces/.*/sessions/", "会话状态目录"),
    ("transcript", r"transcript\.jsonl$|recent_context\.jsonl$", "对话记录文件"),
]

# 需要人工判断 → 生成报告
SUSPICIOUS_PATTERNS = [
    (r"\.json$", "JSON 文件（可能含配置或数据）"),
    (r"payload", "payload 文件"),
    (r"private|secret|password|passwd|key", "名称含敏感词"),
    (r"instances\.json$", "实例注册表"),
    (r"AGENT\.md$", "Agent 指令文件"),
    (r"\.personal$", "个人配置文件"),
    (r"_payload\.json$", "payload 数据文件"),
]

# ─── 工具函数 ────────────────────────────────────────────────────────────────

def run(cmd, cwd=None, capture=True):
    result = subprocess.run(
        cmd, shell=True, cwd=cwd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True
    )
    return result.stdout.strip() if capture else result.returncode


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def matches_any(path_str, patterns):
    import re
    for pat_id, pattern, desc in patterns:
        if re.search(pattern, path_str, re.IGNORECASE):
            return pat_id, pattern, desc
    return None


def matches_suspicious(path_str, patterns):
    import re
    for pattern, desc in patterns:
        if re.search(pattern, path_str, re.IGNORECASE):
            return pattern, desc
    return None


def gitignore_path(repo_path):
    return Path(repo_path) / ".gitignore"


def add_to_gitignore(repo_path, entry, comment):
    gi = gitignore_path(repo_path)
    content = gi.read_text() if gi.exists() else ""
    if entry in content:
        return False
    line = f"\n# [gitwatch] {comment}\n{entry}\n"
    gi.write_text(content + line)
    return True


def get_untracked_files(repo_path):
    output = run("git ls-files --others --exclude-standard", cwd=repo_path)
    if not output:
        return []
    return [f.strip() for f in output.splitlines() if f.strip()]


def get_recent_remote_commits(repo_path, since_hash=None):
    """获取 remote 上自 since_hash 以来的新增文件列表"""
    if since_hash:
        log_range = f"{since_hash}..origin/release/v2.0.0"
    else:
        log_range = "origin/release/v2.0.0~5..origin/release/v2.0.0"

    run("git fetch origin --quiet", cwd=repo_path)

    diff_output = run(
        f"git diff --name-status --diff-filter=A {log_range}",
        cwd=repo_path
    )
    files = []
    for line in diff_output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append(parts[-1].strip())
    return files


def get_current_remote_hash(repo_path):
    return run("git rev-parse origin/release/v2.0.0", cwd=repo_path)


# ─── 主逻辑 ──────────────────────────────────────────────────────────────────

def check_instance(inst, state):
    name = inst["name"]
    path = inst["path"]
    report_lines = []
    auto_fixed = []
    uncertain = []

    if not Path(path).exists():
        return [], [], [f"⚠️  {name} 路径不存在：{path}"]

    untracked = get_untracked_files(path)
    prev_untracked = set(state.get(name, {}).get("seen_untracked", []))
    new_untracked = [f for f in untracked if f not in prev_untracked]

    for fpath in new_untracked:
        match = matches_any(fpath, AUTO_IGNORE_PATTERNS)
        if match:
            pat_id, pattern, desc = match
            parts = fpath.split("/")
            entry = parts[0] + ("/" if len(parts) > 1 else "")
            if add_to_gitignore(path, entry, desc):
                auto_fixed.append(f"`{fpath}` → 已加入 .gitignore（{desc}）")
        else:
            susp = matches_suspicious(fpath, SUSPICIOUS_PATTERNS)
            if susp:
                pattern, desc = susp
                uncertain.append((fpath, desc))

    prev_hash = state.get(name, {}).get("last_remote_hash")
    try:
        remote_files = get_recent_remote_commits(path, prev_hash)
    except Exception as e:
        remote_files = []
        report_lines.append(f"⚠️  {name} fetch 失败：{e}")

    for fpath in remote_files:
        match = matches_any(fpath, AUTO_IGNORE_PATTERNS)
        if match:
            _, _, desc = match
            uncertain.append((fpath, f"**已推送到 remote** — {desc}（需要人工评估是否清除）"))
        else:
            susp = matches_suspicious(fpath, SUSPICIOUS_PATTERNS)
            if susp:
                _, desc = susp
                uncertain.append((fpath, f"remote 新增 — {desc}"))

    new_hash = get_current_remote_hash(path)
    state[name] = {
        "seen_untracked": list(set(untracked)),
        "last_remote_hash": new_hash,
        "last_check": datetime.now(timezone.utc).isoformat(),
    }

    return auto_fixed, uncertain, report_lines


def main():
    state = load_state()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    all_fixed = {}
    all_uncertain = {}
    all_errors = {}

    for inst in INSTANCES:
        fixed, uncertain, errors = check_instance(inst, state)
        if fixed:
            all_fixed[inst["name"]] = fixed
        if uncertain:
            all_uncertain[inst["name"]] = uncertain
        if errors:
            all_errors[inst["name"]] = errors

    save_state(state)

    lines = [f"# gitwatch 报告 — {now}\n"]

    if not all_fixed and not all_uncertain and not all_errors:
        lines.append("✅ 三个实例均无新发现，一切正常。\n")
    else:
        if all_fixed:
            lines.append("## ✅ 已自动处理\n")
            for inst, items in all_fixed.items():
                lines.append(f"### {inst}")
                for item in items:
                    lines.append(f"- {item}")
            lines.append("")

        if all_uncertain:
            lines.append("## ⚠️ 需要人工确认\n")
            lines.append("以下文件不确定，建议你逐一确认是否加入 .gitignore：\n")
            for inst, items in all_uncertain.items():
                lines.append(f"### {inst}")
                for fpath, reason in items:
                    lines.append(f"- `{fpath}` — {reason}")
            lines.append("")

        if all_errors:
            lines.append("## ❌ 错误\n")
            for inst, items in all_errors.items():
                lines.append(f"### {inst}")
                for item in items:
                    lines.append(f"- {item}")
            lines.append("")

    report = "\n".join(lines)
    REPORT_FILE.write_text(report)

    print(report)

    sys.exit(1 if all_uncertain or all_errors else 0)


if __name__ == "__main__":
    main()
