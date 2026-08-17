from __future__ import annotations

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_menu_views
from orchestrator.command_ui import BACK_LABEL, REFRESH_LABEL


def _workspace_dir(runtime) -> Path:
    direct = getattr(runtime, "workspace_dir", None)
    if direct is not None:
        return Path(direct)
    return Path(runtime.config.workspace_dir)


def _skill_key(manager, skill) -> str:
    callback_key = getattr(manager, "skill_callback_key", None)
    return callback_key(skill.id) if callable(callback_key) else skill.id


def _resolve_skill(manager, reference: str):
    by_key = getattr(manager, "get_skill_by_callback_key", None)
    if callable(by_key):
        skill = by_key(reference)
        if skill is not None:
            return skill
    return manager.get_skill(reference)


def _back_to_catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BACK_LABEL, callback_data="skill:b:all")]]
    )


def _back_to_skill_keyboard(manager, skill) -> InlineKeyboardMarkup:
    key = _skill_key(manager, skill)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BACK_LABEL, callback_data=f"skill:s:{key}")]]
    )


def build_skill_catalog_keyboard(manager, workspace_dir: Path) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for skill in manager.list_skills():
        enabled = manager.is_skill_enabled(workspace_dir, skill.id)
        key = _skill_key(manager, skill)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if enabled else '⏸'} {skill.id}",
                    callback_data=f"skill:s:{key}",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton("➕ Install", callback_data="skill:i:all"),
                InlineKeyboardButton("🔎 Find", callback_data="skill:f:all"),
            ],
            [
                InlineKeyboardButton(REFRESH_LABEL, callback_data="skill:r:all"),
                InlineKeyboardButton(
                    f"⚠️ Invalid {len(manager.skill_validation_errors())}",
                    callback_data="skill:z:all",
                ),
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


def build_skill_action_keyboard(
    manager, workspace_dir: Path, skill
) -> InlineKeyboardMarkup:
    key = _skill_key(manager, skill)
    enabled = manager.is_skill_enabled(workspace_dir, skill.id)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("▶️ Use", callback_data=f"skill:u:{key}"),
            InlineKeyboardButton("🧪 Validate", callback_data=f"skill:v:{key}"),
        ],
        [
            InlineKeyboardButton(
                "⏸ Disable" if enabled else "✅ Enable",
                callback_data=f"skill:{'d' if enabled else 'e'}:{key}",
            )
        ],
    ]
    if manager.can_uninstall_skill(skill):
        labels = {
            "project": "🗑️ Delete",
            "linked": "🔗 Unlink",
            "installed": "🗑️ Uninstall",
        }
        label = labels.get(skill.source_type, "🗑️ Delete")
        rows.append([InlineKeyboardButton(label, callback_data=f"skill:x:{key}")])
    rows.append([InlineKeyboardButton(BACK_LABEL, callback_data="skill:b:all")])
    return InlineKeyboardMarkup(rows)


async def _render_catalog(runtime, query, *, notice: str | None = None) -> None:
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
        text = f"{text}\n\n{notice}"
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=runtime._skill_keyboard(),
    )


async def _render_skill(runtime, query, skill) -> None:
    await query.edit_message_text(
        runtime_menu_views.skill_detail_text(
            skill,
            _workspace_dir(runtime),
            manager=runtime.skill_manager,
        ),
        parse_mode="HTML",
        reply_markup=runtime._skill_action_keyboard(skill),
    )


async def handle_skill_callback(runtime, query, data: str) -> bool:
    if not data.startswith("skill:"):
        return False

    manager = runtime.skill_manager
    if data in {"skill:back:menu", "skill:b:all"}:
        await _render_catalog(runtime, query)
        await query.answer()
        return True

    parts = data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Invalid Skill action", show_alert=True)
        return True
    _, action, reference = parts

    if action in {"toggle", "run", "jobs"}:
        await query.answer(
            "This legacy Skill action is disabled. Use /jobs or the runtime command.",
            show_alert=True,
        )
        return True

    if action in {"r", "rescan"}:
        await _render_catalog(
            runtime, query, notice="✅ Catalog rescanned and validated."
        )
        await query.answer("Skill catalog rescanned")
        return True
    if action in {"z", "invalid"}:
        errors = manager.skill_validation_errors()
        await query.edit_message_text(
            runtime_menu_views.skill_invalid_packages_text(errors),
            parse_mode="HTML",
            reply_markup=_back_to_catalog_keyboard(),
        )
        await query.answer()
        return True
    if action in {"i", "install"}:
        await query.edit_message_text(
            runtime_menu_views.skill_install_help_text(),
            parse_mode="HTML",
            reply_markup=_back_to_catalog_keyboard(),
        )
        await query.answer()
        return True
    if action in {"f", "find"}:
        await query.edit_message_text(
            runtime_menu_views.skill_find_help_text(),
            parse_mode="HTML",
            reply_markup=_back_to_catalog_keyboard(),
        )
        await query.answer()
        return True

    skill = _resolve_skill(manager, reference)
    if skill is None:
        await query.answer("Unknown skill", show_alert=True)
        return True
    action = {"show": "s"}.get(action, action)
    workspace = _workspace_dir(runtime)

    if action == "s":
        await _render_skill(runtime, query, skill)
        await query.answer()
        return True
    if action == "u":
        await query.answer(f"Use /skill {skill.id} <request>", show_alert=True)
        return True
    if action == "v":
        await query.edit_message_text(
            runtime_menu_views.skill_validation_text(skill, manager=manager),
            parse_mode="HTML",
            reply_markup=_back_to_skill_keyboard(manager, skill),
        )
        await query.answer("Validation complete")
        return True
    if action == "e":
        ok, message = manager.set_skill_enabled(workspace, skill.id, enabled=True)
        if not ok:
            await query.answer(message, show_alert=True)
            return True
        await _render_skill(runtime, query, manager.get_skill(skill.id) or skill)
        await query.answer(message)
        return True
    if action == "d":
        dependencies = manager.skill_dependencies(skill.id, enabled_only=True)
        if dependencies:
            key = _skill_key(manager, skill)
            await query.edit_message_text(
                runtime_menu_views.skill_disable_confirm_text(skill, dependencies),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Disable Skill",
                                callback_data=f"skill:dc:{key}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "← Keep enabled",
                                callback_data=f"skill:s:{key}",
                            )
                        ],
                    ]
                ),
            )
            await query.answer()
            return True
        ok, message = manager.set_skill_enabled(workspace, skill.id, enabled=False)
        if not ok:
            await query.answer(message, show_alert=True)
            return True
        await _render_skill(runtime, query, manager.get_skill(skill.id) or skill)
        await query.answer(message)
        return True
    if action == "dc":
        ok, message = manager.set_skill_enabled(workspace, skill.id, enabled=False)
        if not ok:
            await query.answer(message, show_alert=True)
            return True
        await _render_skill(runtime, query, manager.get_skill(skill.id) or skill)
        await query.answer(message)
        return True
    if action == "x":
        if not manager.can_uninstall_skill(skill):
            await query.answer(
                "This Skill source cannot be removed.",
                show_alert=True,
            )
            return True
        dependencies = manager.skill_dependencies(skill.id)
        key = _skill_key(manager, skill)
        rows = []
        if not dependencies:
            labels = {
                "project": "Delete Skill",
                "linked": "Unlink Skill",
                "installed": "Uninstall Skill",
            }
            label = labels.get(skill.source_type, "Delete Skill")
            rows.append([InlineKeyboardButton(label, callback_data=f"skill:xc:{key}")])
        rows.append(
            [InlineKeyboardButton("← Keep Skill", callback_data=f"skill:s:{key}")]
        )
        await query.edit_message_text(
            runtime_menu_views.skill_uninstall_confirm_text(skill, dependencies),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        await query.answer()
        return True
    if action == "xc":
        ok, message, _recovery_path = manager.uninstall_skill(skill.id)
        if not ok:
            await query.answer(message, show_alert=True)
            await _render_skill(runtime, query, manager.get_skill(skill.id) or skill)
            return True
        await _render_catalog(runtime, query, notice=f"✅ {message}")
        await query.answer(message)
        return True

    await query.answer("Unknown Skill action", show_alert=True)
    return True
