from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Awaitable, Callable

from orchestrator import runtime_menu_views
from orchestrator.command_ui import card_title

Reply = Callable[..., Awaitable[Any]]


def _workspace_dir(runtime) -> Path:
    direct = getattr(runtime, "workspace_dir", None)
    if direct is not None:
        return Path(direct)
    return Path(runtime.config.workspace_dir)


async def _render_catalog(runtime, reply: Reply, *, notice: str | None = None) -> None:
    manager = runtime.skill_manager
    workspace = _workspace_dir(runtime)
    skills = manager.list_skills()
    errors = manager.skill_validation_errors()
    enabled_count = sum(
        manager.is_skill_enabled(workspace, skill.id) for skill in skills
    )
    text = runtime_menu_views.skills_menu_text(
        count=len(skills),
        agent_name=runtime.name,
        enabled_count=enabled_count,
        invalid_count=len(errors),
    )
    if notice:
        text += f"\n\n{html.escape(notice)}"
    await reply(text, parse_mode="HTML", reply_markup=runtime._skill_keyboard())


def _help_text(runtime) -> str:
    manager = runtime.skill_manager
    skills = manager.list_skills()
    errors = manager.skill_validation_errors()
    lines = [
        card_title("🧰", "Skills reference"),
        "",
        f"<b>Current</b> · <code>{len(skills)}</code> available",
        f"<b>Agent</b> · <code>{html.escape(runtime.name)}</code>",
        f"<b>Invalid</b> · <code>{len(errors)}</code>",
        "",
        "<b>MAINTENANCE</b>",
        "<code>/skill install &lt;directory&gt;</code> · validate and copy",
        "<code>/skill link &lt;directory&gt;</code> · validate and link",
        "<code>/skill enable|disable &lt;id&gt;</code> · per-agent state",
        "<code>/skill validate [id]</code> · package diagnostics",
        "<code>/skill uninstall &lt;id&gt;</code> · managed packages only",
        "<code>/skill find &lt;text&gt;</code> · search ID and description",
        "<code>/skill rescan</code> · rebuild the visible catalog",
        "",
        "<b>CATALOG</b>",
    ]
    for skill in skills:
        lines.append(
            f"<code>{html.escape(skill.id)}</code> · {html.escape(skill.description)}"
        )
    lines.extend(["", "Jobs: <code>/jobs</code> · EXP: <code>/exp</code>"])
    return "\n".join(lines)


async def handle_standard_skill_command(
    runtime,
    update,
    args: list[str],
    reply: Reply,
) -> None:
    """Handle standard package lifecycle after runtime-only aliases are removed."""

    manager = runtime.skill_manager
    workspace = _workspace_dir(runtime)
    if not args:
        await _render_catalog(runtime, reply)
        return

    sub = args[0].strip().casefold()
    rest = [str(item) for item in args[1:]]

    if sub == "help":
        await reply(_help_text(runtime), parse_mode="HTML")
        return
    if sub in {"rescan", "reload", "refresh"}:
        await _render_catalog(runtime, reply, notice="Catalog rescanned and validated.")
        return
    if sub in {"invalid", "errors", "doctor"}:
        await reply(
            runtime_menu_views.skill_invalid_packages_text(
                manager.skill_validation_errors()
            ),
            parse_mode="HTML",
            reply_markup=runtime._skill_keyboard(),
        )
        return
    if sub in {"install", "link"}:
        source = " ".join(rest).strip()
        if not source:
            await reply(
                runtime_menu_views.skill_install_help_text(),
                parse_mode="HTML",
                reply_markup=runtime._skill_keyboard(),
            )
            return
        ok, message, skill = manager.install_skill(
            source,
            link=(sub == "link"),
            actor=runtime.name,
        )
        if not ok or skill is None:
            await reply(f"❌ {message}")
            return
        await reply(
            runtime_menu_views.skill_detail_text(skill, workspace, manager=manager)
            + f"\n\n✅ {html.escape(message)}",
            parse_mode="HTML",
            reply_markup=runtime._skill_action_keyboard(skill),
        )
        return
    if sub == "find":
        query = " ".join(rest).strip()
        if not query:
            await reply(
                runtime_menu_views.skill_find_help_text(),
                parse_mode="HTML",
                reply_markup=runtime._skill_keyboard(),
            )
            return
        folded = query.casefold()
        matches = [
            skill
            for skill in manager.list_skills()
            if folded in skill.id.casefold() or folded in skill.description.casefold()
        ]
        await reply(
            runtime_menu_views.skill_search_results_text(query, matches),
            parse_mode="HTML",
            reply_markup=runtime._skill_keyboard(),
        )
        return
    if sub == "validate":
        if not rest:
            errors = manager.skill_validation_errors()
            await reply(
                runtime_menu_views.skill_invalid_packages_text(errors),
                parse_mode="HTML",
                reply_markup=runtime._skill_keyboard(),
            )
            return
        skill = manager.get_skill(rest[0])
        if skill is None:
            await reply(f"Unknown Skill: {rest[0]}")
            return
        await reply(
            runtime_menu_views.skill_validation_text(skill, manager=manager),
            parse_mode="HTML",
            reply_markup=runtime._skill_action_keyboard(skill),
        )
        return
    if sub in {"enable", "disable"}:
        if not rest:
            await reply(
                f"Usage: /skill {sub} <id>{' --force' if sub == 'disable' else ''}"
            )
            return
        skill = manager.get_skill(rest[0])
        if skill is None:
            await reply(f"Unknown Skill: {rest[0]}")
            return
        enabled = sub == "enable"
        if not enabled:
            dependencies = manager.skill_dependencies(skill.id, enabled_only=True)
            forced = any(item.casefold() == "--force" for item in rest[1:])
            if dependencies and not forced:
                labels = ", ".join(item["id"] for item in dependencies[:5])
                await reply(
                    f"Skill '{skill.id}' has enabled Job dependencies: {labels}. "
                    f"Use /skill disable {skill.id} --force to confirm."
                )
                return
        ok, message = manager.set_skill_enabled(
            workspace,
            skill.id,
            enabled=enabled,
            actor=runtime.name,
        )
        await reply(("✅ " if ok else "❌ ") + message)
        return
    if sub in {"uninstall", "unlink", "remove", "delete"}:
        if not rest:
            await reply("Usage: /skill uninstall <id>")
            return
        ok, message, recovery_path = manager.uninstall_skill(rest[0])
        suffix = f" Recovery: {recovery_path}" if recovery_path is not None else ""
        await reply(("✅ " if ok else "❌ ") + message + suffix)
        return

    skill = manager.get_skill(sub)
    if skill is None:
        await reply(f"Unknown Skill: {sub}. Use /skill help or /skill find <text>.")
        return
    prompt_text = " ".join(rest).strip()
    if not prompt_text:
        await reply(
            runtime_menu_views.skill_detail_text(skill, workspace, manager=manager),
            parse_mode="HTML",
            reply_markup=runtime._skill_action_keyboard(skill),
        )
        return
    if not manager.is_skill_enabled(workspace, skill.id):
        await reply(
            f"Skill '{skill.id}' is disabled for this agent. "
            f"Use /skill enable {skill.id} first."
        )
        return

    prompt = manager.build_prompt_for_skill(skill, prompt_text)
    await reply(f"Running skill {skill.id}...")
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        f"skill:{skill.id}",
        f"Skill {skill.id}",
    )
