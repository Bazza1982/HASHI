"""Deterministic FYI navigation/migration CI gate; no model calls or network."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.agent_documentation import (  # noqa: E402
    catalogue_domains, read_documentation, validate_catalogue, validate_knowledge_migration,
)
from orchestrator.agent_fyi import BOOTSTRAP_CHAR_BUDGET, build_agent_fyi_primer  # noqa: E402


def _size(text: str) -> dict:
    return {"characters": len(text), "estimated_tokens": math.ceil(len(text) / 4)}


def context_metrics(root: Path) -> dict:
    """Reproduce the pre-migration wrapper; estimates never become size gates."""
    from hashlib import sha256

    baseline = json.loads((root / "docs/agent-guide/fyi-v1-migration.json").read_text(encoding="utf-8"))["baseline"]
    old_fyi = baseline["text"].strip()
    old_rules = baseline["engineering_guidance"].strip()
    old_revision = sha256((old_fyi + "\0" + old_rules).encode("utf-8")).hexdigest()[:12]
    previous = "\n".join([
        "--- AGENT FYI ---",
        f"Reference revision: {old_revision}. Documentation is not proof of live adoption.",
        old_fyi, "", "--- HASHI ENGINEERING REFERENCE ---", old_rules,
    ]).strip()
    fyi = (root / "docs/AGENT_FYI.md").read_text(encoding="utf-8").strip()
    rules = (root / "AGENTS.md").read_text(encoding="utf-8").strip()
    primer = build_agent_fyi_primer(root / "docs/AGENT_FYI.md")
    from tools.schemas import TOOL_SCHEMA_MAP

    reader_schema = json.dumps(TOOL_SCHEMA_MAP["documentation_read"], ensure_ascii=False, separators=(",", ":"))
    examples = {}
    for name, path, section in [
        ("voice guide", "docs/agent-guide/voice.md", ""),
        ("Phone context", "docs/HASHI_PHONE_CONTEXT_HANDOFF.md", "Freeze before action admission"),
        ("Compute truthfulness", "docs/HASHI_COMPUTE_SERVICE_DIRECTORY.md", "Compatibility and truthfulness"),
        ("Move", "docs/HASHI_AGENT_MOVE_V1.md", "Transaction"),
        ("PCM authority", "docs/HASHI_PCM_SYSTEM_DESIGN.md", "7. PCM Authority and Conflict Resolution"),
    ]:
        first = read_documentation({"path": path, "section": section, "max_chars": 8000}, code_root=root)
        if not first["ok"]:
            examples[name] = first
            continue
        total = first["text"]
        current = first
        pages = 1
        while not current["complete"]:
            current = read_documentation(current["continuation"], code_root=root)
            if not current["ok"]:
                raise ValueError(f"Metric continuation failed: {path}: {current}")
            total += current["text"]
            pages += 1
        examples[name] = {**_size(total), "pages": pages, "source": first["source"]}
    occurrences = {"catalogue": primer.count(fyi), "engineering_guidance": primer.count(rules)}
    return {
        "token_accounting": "Estimate only: ceil(Unicode characters / 4). No backend tokenizer was installed; no model calls.",
        "baseline_commit": baseline["commit"],
        "before": {"fyi": _size(old_fyi), "engineering_guidance": _size(old_rules), "assembled_primer": _size(previous)},
        "after": {"fyi": _size(fyi), "engineering_guidance": _size(rules), "assembled_primer": _size(primer)},
        "bootstrap_char_budget": BOOTSTRAP_CHAR_BUDGET,
        "additional_reader_tool_schema": _size(reader_schema),
        "normal_bootstrap_occurrences": occurrences,
        "duplicate_bootstrap_characters": max(0, occurrences["catalogue"] - 1) * len(fyi)
            + max(0, occurrences["engineering_guidance"] - 1) * len(rules),
        "on_demand_examples": examples,
        "scope": "Reference primer only; excludes user request, Session history and backend tool schemas. Explicit /fyi plus an armed Session primer is deduplicated by runtime tests.",
    }


def validate(root: Path) -> tuple[list[str], dict]:
    errors = validate_catalogue(root / "docs/AGENT_FYI.md", full=True)
    errors.extend(validate_knowledge_migration(root))
    if not errors:
        baseline = json.loads((root / "docs/agent-guide/fyi-v1-migration.json").read_text(encoding="utf-8"))["baseline"]
        try:
            for path, key in (("docs/AGENT_FYI.md", "text"), ("AGENTS.md", "engineering_guidance")):
                original = subprocess.check_output(
                    ["git", "show", baseline["commit"] + ":" + path], cwd=root, stderr=subprocess.PIPE,
                ).decode("utf-8").replace("\r\n", "\n")
                if original != baseline[key]:
                    errors.append(f"migration: preserved {path} differs from the recorded Git baseline")
        except (OSError, subprocess.CalledProcessError, UnicodeError):
            errors.append("migration: original Git baseline is unavailable for review verification")
    if errors:
        return errors, {}
    metrics = context_metrics(root)
    primer = build_agent_fyi_primer(root / "docs/AGENT_FYI.md")
    if "Reference status: incomplete" in primer or "[fyi trimmed]" in primer:
        errors.append("bootstrap: reference assembly did not produce a complete validated catalogue")
    if len(primer) > BOOTSTRAP_CHAR_BUDGET:
        errors.append("bootstrap: actual injected primer exceeds its runtime safety budget")
    for name, result in metrics["on_demand_examples"].items():
        if result.get("ok") is False:
            errors.append(f"retrieval {name}: {result['message']}")
    return errors, metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the structured validation report")
    parser.add_argument("--metrics-output", type=Path, help="Write review-only context measurements")
    args = parser.parse_args()
    errors, metrics = validate(ROOT)
    if args.metrics_output and not errors:
        args.metrics_output.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps({"ok": not errors, "errors": errors, "metrics": metrics}, indent=2))
    elif errors:
        for error in errors:
            print("FYI validation: " + error)
    else:
        count = len(catalogue_domains((ROOT / "docs/AGENT_FYI.md").read_text(encoding="utf-8")))
        print(f"FYI catalogue: {count} domains, complete migration and source-relative links verified.")
        print("Actual reference primer: " + str(metrics["after"]["assembled_primer"]["characters"]) + " characters.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_5484e06e5306454e94bac6bddc2cb163
