from __future__ import annotations

from pathlib import Path

from exp.loader import ExpStore


def _format_exp_catalog(store: ExpStore) -> str:
    lines: list[str] = []
    for exp_id in store.list_ids():
        try:
            manifest = store.get_manifest(exp_id)
        except Exception as exc:
            lines.append(f"- {exp_id}: unavailable ({type(exc).__name__}: {exc})")
            continue
        summary = str(manifest.get("summary") or "").strip() or "No summary."
        playbooks = manifest.get("playbooks", {})
        playbook_names = ", ".join(sorted(playbooks)) if isinstance(playbooks, dict) else "none"
        lines.append(f"- {exp_id}: {summary} Playbooks: {playbook_names}.")
    return "\n".join(lines) if lines else "- No EXP entries found."


def build_exp_task_prompt(task: str, exp_root: str | Path | None = None) -> str:
    """Build the prompt injected by `/exp <task>`."""

    clean_task = (task or "").strip()
    if not clean_task:
        raise ValueError("EXP task cannot be empty")

    store = ExpStore(exp_root)
    catalog = _format_exp_catalog(store)
    root_path = str(store.root.resolve())

    return (
        "--- EXP GUIDEBOOK REQUEST ---\n"
        "The user invoked /exp. EXP means context-specific expertise and "
        "experience, not a generic skill.\n\n"
        "User task:\n"
        f"{clean_task}\n\n"
        "Available EXP dictionary:\n"
        f"{catalog}\n\n"
        "EXP root:\n"
        f"{root_path}\n\n"
        "Instructions for the agent:\n"
        "1. Inspect the EXP dictionary before acting.\n"
        "2. Select the most relevant EXP id and playbook(s). If no EXP applies, "
        "say so and proceed normally.\n"
        "3. Read the selected EXP files as a guidebook, especially manifest.json, "
        "EXP.md, playbooks, failure memory, validators, templates, and evidence.\n"
        "4. Apply only the parts that match this user's current context. Do not "
        "treat EXP as universal knowledge.\n"
        "5. Execute the task using the chosen EXP, and leave evidence when the "
        "task produces files or uses desktop software.\n"
        "6. If the task reveals a reusable context-specific lesson, propose or "
        "make an update to the relevant EXP after the work is done.\n"
        "--- END EXP GUIDEBOOK REQUEST ---"
    )


def get_exp_usage_text(exp_root: str | Path | None = None) -> str:
    store = ExpStore(exp_root)
    catalog = _format_exp_catalog(store)
    return (
        "Usage:\n"
        "/exp <task>\n\n"
        "Example:\n"
        "/exp do a presentation slides for Armidale council using the council presentation template\n\n"
        "Available EXP dictionary:\n"
        f"{catalog}"
    )
