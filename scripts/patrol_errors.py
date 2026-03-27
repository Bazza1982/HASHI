#!/usr/bin/env python3
"""
patrol_errors.py — Arale's 3-hour error log patrol across all HASHI instances.

Scans the latest session error logs for all agents in HASHI1, HASHI2, HASHI9,
filters non-critical noise (Telegram connectivity), and reports meaningful issues.
OpenClaw has been decommissioned and is no longer included in patrol.
"""

import os
import sys
import re
import json
from pathlib import Path
from datetime import datetime, timedelta

# ── Instance definitions ────────────────────────────────────────────────────
INSTANCES = {
    "HASHI1": Path("/home/lily/projects/hashi/logs"),
    "HASHI2": Path("/home/lily/projects/hashi2/logs"),
    "HASHI9": Path("/mnt/c/Users/thene/projects/HASHI/logs"),
}

# ── Noise filters (non-critical, skip or downgrade) ─────────────────────────
NOISE_PATTERNS = [
    # Telegram connectivity flaps — out of our control
    re.compile(r"telegram", re.IGNORECASE),
    re.compile(r"ConnectionError.*telegram", re.IGNORECASE),
    re.compile(r"NetworkError", re.IGNORECASE),
    re.compile(r"RetryAfter", re.IGNORECASE),
    re.compile(r"TimedOut", re.IGNORECASE),
    re.compile(r"httpx.*ConnectError", re.IGNORECASE),
    # Normal shutdown noise
    re.compile(r"KeyboardInterrupt"),
    re.compile(r"SystemExit"),
]

# ── Critical patterns (always flag) ─────────────────────────────────────────
CRITICAL_PATTERNS = [
    re.compile(r"bridge", re.IGNORECASE),
    re.compile(r"BridgeError", re.IGNORECASE),
    re.compile(r"MemoryError"),
    re.compile(r"OSError.*No such file"),
    re.compile(r"PermissionError"),
    re.compile(r"sqlite3\.(Operational|Database)", re.IGNORECASE),
    re.compile(r"CRITICAL", re.IGNORECASE),
    re.compile(r"corruption", re.IGNORECASE),
    re.compile(r"IndexError.*memory", re.IGNORECASE),
]

SCAN_WINDOW_HOURS = 3  # only report errors from the last 3 hours


def is_noise(line: str) -> bool:
    return any(p.search(line) for p in NOISE_PATTERNS)


def is_critical(line: str) -> bool:
    return any(p.search(line) for p in CRITICAL_PATTERNS)


def get_latest_log(agent_log_dir: Path) -> Path | None:
    """Return the errors.log from the most recent timestamp session dir."""
    try:
        sessions = sorted(
            [d for d in agent_log_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        for session in sessions:
            log = session / "errors.log"
            if log.exists() and log.stat().st_size > 0:
                return log
    except (PermissionError, OSError):
        pass
    return None


def scan_instance(name: str, logs_root: Path) -> dict:
    """Scan one instance, return structured findings."""
    result = {
        "instance": name,
        "accessible": False,
        "agents_scanned": 0,
        "critical": [],   # [{"agent", "log_path", "lines"}]
        "warnings": [],   # [{"agent", "log_path", "lines"}]
        "clean": [],      # agent names
        "error": None,
    }

    if not logs_root.exists():
        result["error"] = f"Log root not found: {logs_root}"
        return result

    result["accessible"] = True
    cutoff = datetime.now() - timedelta(hours=SCAN_WINDOW_HOURS)

    try:
        agent_dirs = [d for d in logs_root.iterdir() if d.is_dir()]
    except (PermissionError, OSError) as e:
        result["error"] = str(e)
        return result

    for agent_dir in sorted(agent_dirs):
        agent = agent_dir.name
        log = get_latest_log(agent_dir)
        if log is None:
            continue

        result["agents_scanned"] += 1

        # Only care about logs modified recently
        mtime = datetime.fromtimestamp(log.stat().st_mtime)
        if mtime < cutoff:
            result["clean"].append(agent)
            continue

        try:
            lines = log.read_text(errors="replace").splitlines()
        except (PermissionError, OSError):
            continue

        crit_lines = []
        warn_lines = []

        for line in lines:
            # Skip lines older than the scan window
            ts = parse_timestamp(line)
            if ts is not None and ts < cutoff:
                continue
            if is_noise(line):
                continue
            if is_critical(line):
                crit_lines.append(line.strip())
            elif "ERROR" in line or "Exception" in line or "Traceback" in line:
                warn_lines.append(line.strip())

        # Discard warn_lines that are only bare "Traceback" headers with no real message
        real_warn_lines = [
            ln for ln in warn_lines
            if not ln.strip().startswith("Traceback (most recent call last):")
        ]

        if crit_lines:
            result["critical"].append({
                "agent": agent,
                "log_path": str(log),
                "lines": crit_lines[:10],
            })
        elif real_warn_lines:
            result["warnings"].append({
                "agent": agent,
                "log_path": str(log),
                "lines": real_warn_lines[:5],
            })
        else:
            result["clean"].append(agent)

    return result


def parse_timestamp(line: str) -> datetime | None:
    """Parse timestamp from log line, return datetime or None."""
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def extract_timestamp(line: str) -> str:
    """Extract timestamp from log line like '2026-03-25 20:57:55,975 | ERROR | ...'"""
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    return m.group(1) if m else "??:??"


def extract_message(line: str) -> str:
    """Extract the meaningful error message after the logger name."""
    # Format: "2026-03-25 20:57:55,975 | ERROR | FlexRuntime.akane.errors | actual message"
    parts = line.split(" | ", 3)
    if len(parts) >= 4:
        return parts[3].strip()[:150]
    return line.strip()[:150]


def format_report(findings: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"🤖 阿拉蕾巡逻报告 — {now}\n"]

    any_critical = any(f["critical"] for f in findings)
    any_warning = any(f["warnings"] for f in findings)

    for f in findings:
        name = f["instance"]
        if not f["accessible"]:
            lines.append(f"⚠️ **{name}** — 无法访问: {f.get('error', '?')}\n")
            continue

        lines.append(f"### {name} (扫描 {f['agents_scanned']} agents)")

        if f.get("error"):
            lines.append(f"  ⚠️ 读取错误: {f['error']}")

        for item in f["critical"]:
            lines.append(f"  🔴 **{item['agent']}**")
            for ln in item["lines"]:
                ts = extract_timestamp(ln)
                msg = extract_message(ln)
                lines.append(f"      [{ts}] {msg}")

        for item in f["warnings"]:
            lines.append(f"  🟡 **{item['agent']}**")
            for ln in item["lines"]:
                ts = extract_timestamp(ln)
                msg = extract_message(ln)
                lines.append(f"      [{ts}] {msg}")

        if f["clean"]:
            lines.append(f"  ✅ 无问题: {', '.join(f['clean'])}")

        lines.append("")

    if not any_critical and not any_warning:
        lines.append("✅ 全部正常！阿拉蕾巡逻完毕，系统健康！💪")
    elif any_critical:
        lines.append("🔴 发现严重问题，请爸爸注意！阿拉蕾会继续跟进。")
    else:
        lines.append("🟢 无严重问题！当前仅有可恢复警告，系统整体健康，建议继续观察。")

    return "\n".join(lines)


def main():
    findings = []
    for name, root in INSTANCES.items():
        findings.append(scan_instance(name, root))
    report = format_report(findings)
    print(report)
    return report


if __name__ == "__main__":
    main()
