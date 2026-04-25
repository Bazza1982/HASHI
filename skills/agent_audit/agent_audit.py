#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import re
from pathlib import Path


PROJECT_ROOT = Path("/home/lily/projects/hashi")
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "generate_agent_behavior_audit.py"
WORKSPACE = PROJECT_ROOT / "workspaces" / "lily"
LATEST_REPORT = WORKSPACE / "agent_behavior_audit_report_latest.md"


def extract_summary(report_text: str) -> list[str]:
    lines = report_text.splitlines()
    findings: list[str] = []
    for i, line in enumerate(lines):
        if re.match(r"^\d+\.\s+\*\*\[", line):
            findings.append(re.sub(r"^\d+\.\s+", "", line).strip("*"))
        if len(findings) >= 4:
            break
    return findings


def main() -> int:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        print("Daily agent audit failed.")
        if output:
            print(output)
        if err:
            print("\nstderr:\n" + err)
        return proc.returncode

    report_path = None
    for line in output.splitlines():
        if line.startswith("Agent behavior audit report written to:"):
            report_path = line.split(":", 1)[1].strip()
            break
    if not report_path:
        report_path = str(LATEST_REPORT)

    findings = []
    if LATEST_REPORT.exists():
        findings = extract_summary(LATEST_REPORT.read_text(encoding="utf-8"))

    print("Daily agent behavior audit executed.")
    print(f"Report path: {report_path}")
    print(f"Latest report: {LATEST_REPORT}")
    print("Execution mode: local-only action skill (no external API, no OpenRouter, no DeepSeek)")
    if findings:
        print("Key findings:")
        for item in findings:
            print(f"- {item}")
    else:
        print("Key findings: none in checked scope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
