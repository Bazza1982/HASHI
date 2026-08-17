from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_ui import (
    card_title,
    confirm_card,
    selected_label,
    setting_card,
    status_label,
)


def _command(command: str, description: str) -> str:
    return f"<code>{html.escape(command)}</code> · {html.escape(description)}"


def parked_topics_text(topics: Sequence[dict[str, Any]]) -> str:
    lines = [
        card_title("🅿️", "Parked topics"),
        "",
        f"<b>Current</b> · <code>{len(topics)}</code> saved",
        "<b>Changes</b> · saved immediately in this workspace",
    ]
    if not topics:
        lines.extend(["", "No parked topics are available."])
    else:
        lines.extend(["", "<b>TOPICS</b>"])
        for topic in topics:
            slot_id = int(topic.get("slot_id", 0) or 0)
            title = topic.get("title") or f"Topic {slot_id}"
            short = topic.get("summary_short") or "(no short summary)"
            followup = topic.get("followup") or {}
            reminder_status = followup.get("status") or "scheduled"
            attempts = int(followup.get("attempts", 0) or 0)
            next_at = followup.get("next_at")
            lines.extend(
                [
                    "",
                    f"<b><code>{slot_id}</code> · {html.escape(str(title))}</b>",
                    html.escape(str(short)),
                    (
                        f"Reminders · <code>{html.escape(str(reminder_status))}</code> "
                        f"· <code>{attempts}/3</code>"
                        + (
                            f" · next <code>{html.escape(str(next_at))}</code>"
                            if next_at
                            else ""
                        )
                    ),
                ]
            )
    lines.extend(
        [
            "",
            "<b>Use</b>",
            _command("/park chat [title]", "park the current topic"),
            _command("/load <slot>", "restore a parked topic"),
            _command("/park delete <slot>", "remove a parked topic"),
        ]
    )
    return "\n".join(lines)


def ticket_list_text(
    open_tickets: Sequence[dict[str, Any]],
    in_progress_tickets: Sequence[dict[str, Any]],
) -> str:
    total = len(open_tickets) + len(in_progress_tickets)
    lines = [
        card_title("🎫", "Support tickets"),
        "",
        f"<b>Current</b> · <code>{total}</code> active",
        f"<b>Open</b> · <code>{len(open_tickets)}</code>",
        f"<b>In progress</b> · <code>{len(in_progress_tickets)}</code>",
    ]
    for heading, tickets in (
        ("OPEN", open_tickets),
        ("IN PROGRESS", in_progress_tickets),
    ):
        if not tickets:
            continue
        lines.extend(["", f"<b>{heading}</b>"])
        for ticket in tickets:
            ticket_id = html.escape(str(ticket.get("ticket_id") or "unknown"))
            source = html.escape(str(ticket.get("source_agent") or "unknown"))
            summary = html.escape(str(ticket.get("summary") or "")[:90])
            lines.append(f"<code>{ticket_id}</code> · {source} · {summary}")
    if not total:
        lines.extend(["", "No open or in-progress tickets."])
    lines.extend(
        [
            "",
            _command("/ticket <description>", "create a support ticket"),
        ]
    )
    return "\n".join(lines)


def sys_slots_text(manager: Any) -> str:
    slots = []
    active_count = 0
    configured_count = 0
    for slot_id in manager.SLOTS:
        slot = manager._slot(slot_id)
        active = bool(slot.get("active"))
        text = str(slot.get("text") or "")
        active_count += int(active)
        configured_count += int(bool(text))
        slots.append((slot_id, active, text))

    lines = [
        card_title("🧾", "System prompt slots"),
        "",
        f"<b>Current</b> · <code>{active_count}</code> active",
        f"<b>Configured</b> · <code>{configured_count}/{len(slots)}</code>",
        "<b>Changes</b> · immediate and persistent in this workspace",
        "",
        "<b>SLOTS</b>",
    ]
    for slot_id, active, text in slots:
        state = status_label(active)
        preview = text[:70].strip() + ("…" if len(text) > 70 else "")
        lines.append(
            f"<code>{slot_id}</code> · <b>{state}</b> · "
            f"{html.escape(preview or '(empty)')}"
        )
    lines.extend(
        [
            "",
            "<b>Use</b>",
            _command("/sys <slot>", "view one slot"),
            _command("/sys <slot> on|off", "change its active state"),
            _command("/sys <slot> save|replace <text>", "update its content"),
            _command("/sys output <slot>", "return its raw content"),
        ]
    )
    return "\n".join(lines)


def sys_slot_text(manager: Any, slot_id: str) -> str:
    slot = manager._slot(slot_id)
    active = bool(slot.get("active"))
    text = str(slot.get("text") or "")
    return "\n".join(
        [
            card_title("🧾", "System prompt slot"),
            "",
            f"<b>Current</b> · <b>{status_label(active)}</b>",
            f"<b>Slot</b> · <code>{html.escape(slot_id)}</code>",
            f"<b>Size</b> · <code>{len(text)}</code> chars",
            "",
            f"<pre>{html.escape(text or '(empty)')}</pre>",
            "",
            "<b>Use</b>",
            _command(f"/sys {slot_id} on|off", "change its active state"),
            _command(f"/sys {slot_id} replace <text>", "replace its content"),
            _command(f"/sys output {slot_id}", "return its raw content"),
        ]
    )


def credit_status_text(data: dict[str, Any]) -> str:
    free_tier = bool(data.get("is_free_tier", False))
    return setting_card(
        "💳",
        "OpenRouter credit",
        current=f"<code>{html.escape(str(data.get('limit_remaining', 'unknown')))}</code> remaining",
        facts=[
            f"<b>Key</b> · <code>{html.escape(str(data.get('label', 'unknown')))}</code>",
            f"<b>Usage</b> · <code>{html.escape(str(data.get('usage', 'unknown')))}</code>",
            f"<b>Limit</b> · <code>{html.escape(str(data.get('limit', 'unknown')))}</code>",
            f"<b>Free tier</b> · <code>{'YES' if free_tier else 'NO'}</code>",
        ],
        consequence="Values come from the active OpenRouter key and are read-only.",
        action="Run <code>/credit</code> again to refresh.",
    )


def model_menu_text(
    *,
    model: str,
    backend: str,
    has_choices: bool,
    persists: bool,
    provider: str | None = None,
) -> str:
    facts = [f"<b>Backend</b> · <code>{html.escape(backend)}</code>"]
    if provider:
        facts.append(f"<b>Provider</b> · <code>{html.escape(provider)}</code>")
    return setting_card(
        "🧠",
        "Hashi model",
        current=f"<code>{html.escape(model)}</code>",
        facts=facts,
        consequence=(
            "The selection applies immediately to the next request"
            + (" and persists." if persists else ".")
            if has_choices
            else "This backend accepts a model name through the typed command."
        ),
        action=(
            "Choose a model below."
            if has_choices
            else "Use <code>/model &lt;name&gt;</code> to switch."
        ),
    )


def thinking_output_text(
    *,
    enabled: bool,
    her_backend: bool,
    reasoning_available: bool,
    commentary_available: bool,
) -> str:
    """Render backend-aware reasoning ownership without growing the runtime."""

    commentary_fact = (
        "<b>HER Persona commentary</b> · <code>OWNED BY /commentary</code>"
        if her_backend
        else (
            "<b>Model commentary</b> · "
            f"<code>{'AVAILABLE' if commentary_available else 'NOT EXPOSED'}</code>"
        )
    )
    if enabled and her_backend:
        consequence = (
            "For HER, shows genuine provider-returned reasoning only; "
            "Persona reports stay under /commentary."
        )
    elif enabled:
        consequence = (
            "Shows model-authored interim commentary and genuine "
            "provider-returned reasoning when available."
        )
    elif her_backend:
        consequence = (
            "HER provider reasoning is hidden; /commentary and /verbose are unaffected."
        )
    else:
        consequence = (
            "Model commentary and provider reasoning are hidden. "
            "Progress and typing are unaffected."
        )
    return setting_card(
        "💭",
        "Thinking output",
        current=f"<b>{status_label(enabled)}</b>",
        facts=[
            "<b>Provider reasoning</b> · "
            f"<code>{'AVAILABLE' if reasoning_available else 'NOT EXPOSED'}</code>",
            commentary_fact,
            "<b>Saved</b> · workspace setting",
        ],
        consequence=consequence,
        action="Changes apply immediately and persist across reboot.",
    )


def her_commentary_text(*, enabled: bool, effort: str) -> str:
    """Render the HER-only Persona commentary ownership contract."""

    return setting_card(
        "🌿",
        "HER commentary",
        current=f"<b>{status_label(enabled)}</b>",
        facts=[
            f"<b>HER effort</b> · <code>{html.escape(effort)}</code>",
            "<b>Medium</b> · may emit the model-authored Persona acknowledgement",
            "<b>High+</b> · may also emit model-authored material progress/Replan reports",
            "<b>Delivery</b> · one durable message per stable event identity",
            "<b>Neutral runtime leases</b> · technical /verbose only",
            "<b>Independent</b> · does not change /think or /verbose",
            "<b>Saved</b> · workspace setting",
        ],
        consequence=(
            "HER shows only explicit model-authored Persona reports here; "
            "technical telemetry and provider reasoning stay separate."
            if enabled
            else "Optional HER Persona acknowledgements and interim reports are "
            "hidden; final and required control messages remain visible."
        ),
        action="Use /commentary on, /commentary off, or the buttons below.",
    )


def claw_provider_menu_text(
    *,
    current_provider: str | None,
    available_count: int,
    unavailable: Sequence[tuple[str, str]] = (),
    backend_flow: bool = False,
) -> str:
    facts = [
        "<b>Backend</b> · <code>her</code>",
        f"<b>Available</b> · <code>{available_count}</code> providers",
        "<b>Changes</b> · applied and saved only after a model is selected",
    ]
    if unavailable:
        locked = ", ".join(
            f"{html.escape(name)} ({html.escape(reason)})"
            for name, reason in unavailable
        )
        facts.append(f"<b>Unavailable</b> · {locked}")
    return setting_card(
        "🔌",
        "HER provider",
        current=(
            f"<code>{html.escape(current_provider)}</code>"
            if current_provider
            else "<code>not selected</code>"
        ),
        facts=facts,
        consequence=(
            "Selecting a provider opens its model list. The current backend remains unchanged until the model choice succeeds."
            if backend_flow
            else "Selecting a provider opens its model list. The current provider remains active until the model choice succeeds."
        ),
        action=(
            "Choose a provider below."
            if available_count
            else "No usable HER provider is configured for this agent."
        ),
    )


def claw_provider_model_text(
    *,
    provider: str,
    current_model: str,
    model_count: int,
    with_context: bool,
) -> str:
    facts = [
        "<b>Backend</b> · <code>her</code>",
        f"<b>Provider</b> · <code>{html.escape(provider)}</code>",
        f"<b>Models</b> · <code>{model_count}</code> available",
        f"<b>Switch</b> · {'with handoff context' if with_context else 'without handoff context'}",
    ]
    return setting_card(
        "🧠",
        "Choose HER model",
        current=f"<code>{html.escape(current_model or 'not selected')}</code>",
        facts=facts,
        consequence="Provider and model are validated, applied, and saved together. A failed switch keeps the current configuration.",
        action=(
            "Choose a model to complete the switch."
            if model_count
            else "No model is available for this provider. Go back and choose another provider."
        ),
    )


def claw_provider_unavailable_text(*, backend: str) -> str:
    return setting_card(
        "🔌",
        "HER provider",
        current="<b>UNAVAILABLE</b>",
        facts=[
            f"<b>Backend</b> · <code>{html.escape(backend)}</code>",
            "<b>Scope</b> · <code>/provider</code> is available only for <code>her</code>",
        ],
        consequence="No backend, provider, or model was changed.",
        action="Use <code>/backend</code> to select Claw first.",
    )


def backend_menu_text(*, active_backend: str) -> str:
    return setting_card(
        "🧠",
        "Hashi backend",
        current=f"<code>{html.escape(active_backend)}</code>",
        consequence="Selecting with context first rebuilds a continuity handoff.",
        action="Choose a backend to select its model and switch.",
    )


def backend_model_prompt_text(
    *,
    backend: str,
    current_model: str,
    with_context: bool,
) -> str:
    mode_text = "with handoff context" if with_context else "without handoff context"
    return setting_card(
        "🧠",
        "Choose model",
        current=f"<code>{html.escape(current_model or 'auto')}</code>",
        facts=[
            f"<b>Backend</b> · <code>{html.escape(backend)}</code>",
            f"<b>Switch</b> · {html.escape(mode_text)}",
        ],
        action="Select a model to complete the switch.",
    )


def loop_manager_text() -> str:
    return setting_card(
        "🔄",
        "Loop manager",
        current="<b>READY</b>",
        facts=["<b>Scope</b> · recurring cron and heartbeat tasks"],
        consequence="New loops and state changes are saved immediately in the task scheduler.",
        action=(
            _command("/loop <task>", "create a loop")
            + "\n"
            + _command("/loop list", "list configured loops")
            + "\n"
            + _command("/loop stop [id]", "stop one or all active loops")
        ),
    )


def loop_list_text(loops: Iterable[tuple[str, dict[str, Any]]]) -> str:
    rows = list(loops)
    enabled_count = sum(bool(job.get("enabled")) for _kind, job in rows)
    lines = [
        card_title("🔄", "Loops"),
        "",
        f"<b>Current</b> · <code>{enabled_count}</code> active · <code>{len(rows)}</code> configured",
        "<b>Changes</b> · immediate and persistent",
    ]
    if not rows:
        lines.extend(["", "No loops are configured for this agent."])
    for job_kind, job in rows:
        meta = job.get("loop_meta") or {}
        enabled = bool(job.get("enabled"))
        count = int(meta.get("count", 0) or 0)
        maximum = int(meta.get("max", 100) or 100)
        schedule = (
            f"every {job.get('interval_seconds', '?')}s"
            if job_kind == "heartbeat"
            else str(job.get("schedule") or "unknown")
        )
        job_id = html.escape(str(job.get("id") or "unknown"))
        summary = html.escape(
            str(meta.get("task_summary") or job.get("note") or "")[:90]
        )
        reason = html.escape(str(meta.get("stopped_reason") or ""))
        lines.extend(
            [
                "",
                f"<b>{'ON' if enabled else 'OFF'}</b> · <code>{job_id}</code>",
                f"<b>Schedule</b> · <code>{html.escape(schedule)}</code> · {html.escape(job_kind)}",
                f"<b>Progress</b> · <code>{count}/{maximum}</code>",
            ]
        )
        if summary:
            lines.append(summary)
        if reason:
            lines.append(f"⚠️ {reason}")
    lines.extend(["", _command("/jobs", "open full scheduler controls")])
    return "\n".join(lines)


def debug_menu_text(*, enabled: bool) -> str:
    return setting_card(
        "🐛",
        "Debug mode",
        current=f"<b>{status_label(enabled)}</b>",
        facts=["<b>Scope</b> · strict debugging skill context"],
        consequence="The toggle persists in this workspace; prompt runs use the debug skill immediately.",
        action=(
            _command("/debug on|off", "change the persistent toggle")
            + "\n"
            + _command("/debug <prompt>", "run one debugging task")
        ),
    )


def skills_menu_text(
    *,
    count: int,
    agent_name: str,
    enabled_count: int | None = None,
    invalid_count: int = 0,
) -> str:
    enabled_count = count if enabled_count is None else enabled_count
    return setting_card(
        "🧰",
        "Skills",
        current=f"<code>{count}</code> available",
        facts=[
            f"<b>Agent</b> · <code>{html.escape(agent_name)}</code>",
            f"<b>Enabled here</b> · <code>{enabled_count}/{count}</code>",
            f"<b>Invalid</b> · <code>{invalid_count}</code>",
            "<b>Format</b> · standard <code>SKILL.md</code> instruction packages",
        ],
        consequence="Skills add focused instructions to a request; Jobs and runtime settings stay on their own control surfaces.",
        action="Choose a Skill to inspect or manage it, or use the maintenance actions below.",
    )


def skill_detail_text(skill: Any, workspace_dir: Any, *, manager: Any) -> str:
    skill_id = str(getattr(skill, "id", "unknown") or "unknown")
    skill_name = str(getattr(skill, "name", skill_id) or skill_id)
    description = str(getattr(skill, "description", "") or "No description.")
    enabled_method = getattr(manager, "is_skill_enabled", None)
    enabled = (
        bool(enabled_method(workspace_dir, skill_id))
        if callable(enabled_method)
        else None
    )
    source_type = str(getattr(skill, "source_type", "project") or "project")
    scope = str(getattr(skill, "scope", "project") or "project")
    source = str(getattr(skill, "source", "") or "")
    source_labels = {
        "project": "PROJECT · built-in",
        "installed": "INSTALLED · managed",
        "linked": "LINKED",
    }
    resource_method = getattr(manager, "skill_resource_counts", None)
    resources = (
        resource_method(skill)
        if callable(resource_method)
        else {"scripts": 0, "references": 0, "assets": 0, "other": 0}
    )
    dependency_method = getattr(manager, "skill_dependencies", None)
    dependencies = dependency_method(skill_id) if callable(dependency_method) else []
    usage_method = getattr(manager, "skill_usage_stats", None)
    usage_stats = (
        usage_method(skill_id)
        if callable(usage_method)
        else {"total": 0, "agents": 0}
    )
    facts = [
        f"<b>ID</b> · <code>{html.escape(skill_id)}</code>",
        f"<b>Source</b> · <code>{html.escape(source_labels.get(source_type, source_type.upper()))}</code>",
        f"<b>Scope</b> · <code>{html.escape(scope)}</code>",
        "<b>Format</b> · <code>SKILL.md</code>",
        (
            "<b>Uses</b> · "
            f"<code>{int(usage_stats.get('total', 0))}</code> cumulative · "
            f"<code>{int(usage_stats.get('agents', 0))}</code> agents"
        ),
        "<b>Usage log</b> · <code>state/skill_usage.jsonl</code>",
        (
            "<b>Resources</b> · "
            f"scripts <code>{int(resources.get('scripts', 0))}</code> · "
            f"references <code>{int(resources.get('references', 0))}</code> · "
            f"assets <code>{int(resources.get('assets', 0))}</code>"
        ),
        f"<b>Job references</b> · <code>{len(dependencies)}</code>",
    ]
    version = str(getattr(skill, "version", "") or "")
    if version:
        facts.append(f"<b>Version</b> · <code>{html.escape(version[:120])}</code>")
    author = str((getattr(skill, "metadata", {}) or {}).get("author") or "")
    if author:
        facts.append(f"<b>Author</b> · <code>{html.escape(author[:120])}</code>")
    license_name = str(getattr(skill, "license", "") or "")
    if license_name:
        facts.append(f"<b>License</b> · <code>{html.escape(license_name[:120])}</code>")
    compatibility = str(getattr(skill, "compatibility", "") or "")
    if compatibility:
        facts.append(f"<b>Compatibility</b> · {html.escape(compatibility[:240])}")
    allowed_tools = str(getattr(skill, "allowed_tools", "") or "")
    if allowed_tools:
        facts.append(
            f"<b>Declared tools</b> · <code>{html.escape(allowed_tools[:180])}</code>"
        )
    if source:
        facts.append(f"<b>Path</b> · <code>{html.escape(source[:240])}</code>")
    if enabled is None:
        current = "<b>READY</b>"
    else:
        current = f"<b>{'ENABLED' if enabled else 'DISABLED'}</b>"
    usage = f"/skill {skill_id} <request>"

    text = setting_card(
        "🧰",
        skill_name,
        current=current,
        facts=facts,
        consequence=html.escape(description),
        action=f"Use <code>{html.escape(usage)}</code> or choose an action below.",
    )
    body = str(getattr(skill, "body", "") or "").strip()
    if body:
        preview = body if len(body) <= 700 else body[:700].rstrip() + "\n\n[truncated]"
        text += f"\n\n<b>REFERENCE</b>\n<pre>{html.escape(preview)}</pre>"
    return text


def skill_validation_text(skill: Any, *, manager: Any) -> str:
    skill_id = str(getattr(skill, "id", "unknown") or "unknown")
    validate = getattr(manager, "validate_skill", None)
    ok, errors = validate(skill_id) if callable(validate) else (True, [])
    lines = [
        card_title("🧪", "Skill validation"),
        "",
        f"<b>Current</b> · <b>{'VALID' if ok else 'INVALID'}</b>",
        f"<b>Skill</b> · <code>{html.escape(skill_id)}</code>",
        "<b>Contract</b> · standard <code>SKILL.md</code> package",
    ]
    if errors:
        lines.extend(["", "<b>ERRORS</b>"])
        for error in errors[:8]:
            lines.append(f"• {html.escape(str(error)[:500])}")
    else:
        lines.extend(
            [
                "",
                "Frontmatter, package name, directory name, and instruction body are valid.",
            ]
        )
    return "\n".join(lines)


def skill_invalid_packages_text(errors: Sequence[str]) -> str:
    lines = [
        card_title("⚠️", "Invalid Skill packages"),
        "",
        f"<b>Current</b> · <code>{len(errors)}</code> invalid",
        "Invalid packages are skipped and cannot execute.",
    ]
    if not errors:
        lines.extend(["", "✅ No validation errors found."])
    else:
        lines.extend(["", "<b>ERRORS</b>"])
        used = 0
        for error in errors:
            rendered = f"• {html.escape(str(error)[:600])}"
            if used + len(rendered) > 3000:
                lines.append(
                    "• Additional errors clipped; run <code>/skill invalid</code> after repairs."
                )
                break
            lines.append(rendered)
            used += len(rendered)
    return "\n".join(lines)


def skill_install_help_text() -> str:
    return "\n".join(
        [
            card_title("➕", "Install Skill"),
            "",
            "<b>Current</b> · <b>READY</b>",
            "<b>Scope</b> · this HASHI project; enable state remains per agent",
            "",
            "The source must be a local standard Skill directory containing <code>SKILL.md</code>.",
            "Copied packages are recoverable after uninstall; linked packages keep their source files.",
            "",
            "<b>Use</b>",
            _command("/skill install <directory>", "validate and copy a package"),
            _command(
                "/skill link <directory>", "validate and link a development package"
            ),
        ]
    )


def skill_find_help_text() -> str:
    return setting_card(
        "🔎",
        "Find Skills",
        current="<b>READY</b>",
        facts=["<b>Search fields</b> · ID and description"],
        action=_command("/skill find <text>", "search the current catalog"),
    )


def skill_search_results_text(query: str, skills: Sequence[Any]) -> str:
    lines = [
        card_title("🔎", "Skill search"),
        "",
        f"<b>Query</b> · <code>{html.escape(query)}</code>",
        f"<b>Matches</b> · <code>{len(skills)}</code>",
        "",
    ]
    if not skills:
        lines.append("No matching Skills.")
    else:
        for skill in skills[:30]:
            skill_id = html.escape(str(getattr(skill, "id", "unknown")))
            description = html.escape(str(getattr(skill, "description", ""))[:160])
            lines.append(f"• <code>{skill_id}</code> · {description}")
    return "\n".join(lines)


def skill_disable_confirm_text(
    skill: Any, dependencies: Sequence[dict[str, Any]]
) -> str:
    skill_id = str(getattr(skill, "id", "unknown") or "unknown")
    consequence = "This agent will stop executing the Skill until it is enabled again."
    if dependencies:
        labels = ", ".join(
            html.escape(str(item.get("id") or "unknown")) for item in dependencies[:5]
        )
        consequence += f" Enabled Jobs that would be affected: <code>{labels}</code>."
    return confirm_card(
        "⏸",
        "Disable Skill",
        target=f"<code>{html.escape(skill_id)}</code>",
        consequence=consequence,
    )


def skill_uninstall_confirm_text(
    skill: Any, dependencies: Sequence[dict[str, Any]]
) -> str:
    skill_id = str(getattr(skill, "id", "unknown") or "unknown")
    source_type = str(getattr(skill, "source_type", "project") or "project")
    if dependencies:
        labels = ", ".join(
            html.escape(str(item.get("id") or "unknown")) for item in dependencies[:5]
        )
        consequence = f"Removal is blocked while Jobs reference this package: <code>{labels}</code>."
    elif source_type == "linked":
        consequence = (
            "This removes only the HASHI link. The source directory is preserved."
        )
    elif source_type == "project":
        consequence = (
            "This removes the built-in package for every agent and moves it to "
            "HASHI's recovery area. Usage history is preserved."
        )
    else:
        consequence = (
            "This removes the active copy and moves it to HASHI's recovery area."
        )
    titles = {
        "project": "Delete Skill",
        "linked": "Unlink Skill",
        "installed": "Uninstall Skill",
    }
    return confirm_card(
        "🗑️",
        titles.get(source_type, "Delete Skill"),
        target=f"<code>{html.escape(skill_id)}</code>",
        consequence=consequence,
    )


def hchat_help_text() -> str:
    return "\n".join(
        [
            card_title("💬", "Hashi chat"),
            "",
            "<b>Current</b> · <b>READY</b>",
            "<b>Scope</b> · local agents, remote instances, groups, or all active local agents",
            "",
            "Messages are composed by this agent before delivery. Remote targets use <code>agent@INSTANCE</code>.",
            "",
            "<b>Use</b>",
            _command("/hchat <agent> <intent>", "message one local agent"),
            _command("/hchat <agent>@<INSTANCE> <intent>", "message a remote agent"),
            _command("/hchat @<group> <intent>", "message a local group"),
            _command("/hchat all <intent>", "message all active local agents"),
            "",
            "<b>Example</b>",
            "<code>/hchat arale review the latest test result</code>",
        ]
    )


def cos_menu_text(*, enabled: bool) -> str:
    return setting_card(
        "🌸",
        "Chief of Staff routing",
        current=f"<b>{status_label(enabled)}</b>",
        facts=["<b>Route</b> · human-in-the-loop decisions through Lily"],
        consequence=(
            "Eligible decisions are routed to Lily before the user is asked."
            if enabled
            else "Decisions are sent directly to the user."
        ),
        action="Use <code>/cos on</code> or <code>/cos off</code>. Changes persist in this workspace.",
    )


def safevoice_menu_text(*, enabled: bool) -> str:
    return setting_card(
        "🛡️",
        "Safe voice",
        current=f"<b>{status_label(enabled)}</b>",
        facts=["<b>Scope</b> · incoming Telegram voice messages"],
        consequence=(
            "Voice transcripts require confirmation before being sent to the agent."
            if enabled
            else "Voice transcripts are sent directly to the agent."
        ),
        action="Choose below or use <code>/safevoice on|off</code>. Changes persist in this workspace.",
    )


def safevoice_keyboard(*, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label("On", enabled), callback_data="safevoice:set:on"
                ),
                InlineKeyboardButton(
                    selected_label("Off", not enabled),
                    callback_data="safevoice:set:off",
                ),
            ]
        ]
    )


def timeout_menu_text(
    *,
    agent_name: str,
    backend_name: str,
    idle_minutes: int,
    hard_minutes: int,
    default_idle_minutes: int,
    default_hard_minutes: int,
    idle_source: str,
    hard_source: str,
) -> str:
    return setting_card(
        "⏱️",
        "Backend timeout",
        current=f"idle <code>{idle_minutes} min</code> · hard <code>{hard_minutes} min</code>",
        facts=[
            f"<b>Defaults</b> · idle <code>{default_idle_minutes} min</code> · hard <code>{default_hard_minutes} min</code>",
            f"<b>Agent</b> · <code>{html.escape(agent_name)}</code>",
            f"<b>Backend</b> · <code>{html.escape(backend_name)}</code>",
            f"<b>Sources</b> · idle <code>{html.escape(idle_source)}</code> · hard <code>{html.escape(hard_source)}</code>",
            "<b>Scope</b> · this agent and backend",
        ],
        consequence=(
            "User overrides survive steering, backend recreation, hot reload and restart until /timeout reset. "
            "Dual-brain memory passes are unaffected."
        ),
        action=(
            _command("/timeout 60", "set idle to 60 minutes")
            + "\n"
            + _command("/timeout 60 1440", "set idle and hard limits")
            + "\n"
            + _command("/timeout reset", "clear the user override")
        ),
    )


def wol_targets_text(
    targets: Sequence[dict[str, Any]], *, instance_id: str | None
) -> str:
    lines = [
        card_title("🪄", "Wake-on-LAN targets"),
        "",
        f"<b>Current</b> · <code>{len(targets)}</code> available",
        f"<b>Instance</b> · <code>{html.escape(instance_id or 'local')}</code>",
        "<b>Changes</b> · sending a packet does not change HASHI configuration",
    ]
    if targets:
        lines.extend(["", "<b>TARGETS</b>"])
        for row in targets:
            name = html.escape(str(row.get("name") or "unknown"))
            label = html.escape(str(row.get("label") or name))
            description = html.escape(str(row.get("description") or ""))
            lines.append(
                f"<code>{name}</code> · <b>{label}</b>"
                + (f" · {description}" if description else "")
            )
    else:
        lines.extend(["", "No Wake-on-LAN targets are configured."])
    lines.extend(["", _command("/wol <pc_name>", "send a Wake-on-LAN packet")])
    return "\n".join(lines)
