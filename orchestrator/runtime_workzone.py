from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from orchestrator import ui_language
from orchestrator.command_ui import setting_card
from orchestrator.workzone import (
    access_root_for_workzone,
    build_workzone_prompt,
    resolve_workzone_input,
)


def workzone_status_text(
    *,
    home_workspace: Any,
    current: Any | None,
    notice: str | None = None,
) -> str:
    active = current is not None
    facts = [
        f"<b>{html.escape(ui_language.tr('workzone.directory'))}</b> · "
        f"<code>{html.escape(str(current or home_workspace))}</code>",
        f"<b>{html.escape(ui_language.tr('workzone.agent_home'))}</b> · "
        f"<code>{html.escape(str(home_workspace))}</code>",
    ]
    if notice:
        facts.insert(0, f"✅ {html.escape(notice)}")
    return setting_card(
        "📁",
        "Workzone",
        current=(
            f"<b>{html.escape(ui_language.tr('common.on' if active else 'common.off'))}</b>"
        ),
        facts=facts,
        consequence=ui_language.tr(
            "workzone.enabled" if active else "workzone.disabled"
        ),
        action=ui_language.tr("workzone.action"),
    )


def sync_workzone_to_backend_config(runtime: Any) -> None:
    if runtime.config.extra is None:
        runtime.config.extra = {}
    if runtime._workzone_dir is not None:
        runtime.config.extra["workzone_dir"] = str(runtime._workzone_dir)
    else:
        runtime.config.extra.pop("workzone_dir", None)
    backend = getattr(getattr(runtime, "backend_manager", None), "current_backend", None)
    if backend is not None and getattr(backend, "config", None) is not None:
        if backend.config.extra is None:
            backend.config.extra = {}
        if runtime._workzone_dir is not None:
            backend.config.extra["workzone_dir"] = str(runtime._workzone_dir)
        else:
            backend.config.extra.pop("workzone_dir", None)
        registry = getattr(backend, "tool_registry", None)
        if registry is not None:
            if runtime._workzone_dir is not None:
                registry.workspace_dir = runtime._workzone_dir
                registry.access_root = access_root_for_workzone(
                    backend.config.resolve_access_root(),
                    runtime._workzone_dir,
                )
            else:
                registry.workspace_dir = runtime.workspace_dir
                registry.access_root = backend.config.resolve_access_root()


def workzone_prompt_section(runtime: Any) -> list[tuple[str, str]]:
    runtime._sync_workzone_to_backend_config()
    backend = getattr(runtime.backend_manager, "current_backend", None)
    can_access_files = bool(
        backend
        and (
            getattr(getattr(backend, "capabilities", None), "supports_files", False)
            or getattr(backend, "tool_registry", None) is not None
        )
    )
    section = build_workzone_prompt(runtime._workzone_dir, runtime.workspace_dir, can_access_files=can_access_files)
    return [section] if section else []


async def cmd_workzone(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = context.args or []
    from orchestrator import runtime_session

    session = runtime_session.current_session_for_update(runtime, update)
    current_value = str(session.get("workzone") or "").strip()
    current = Path(current_value) if current_value else None
    if not args:
        await runtime._reply_text(
            update,
            workzone_status_text(home_workspace=runtime.workspace_dir, current=current),
            parse_mode="HTML",
        )
        return
    arg_text = " ".join(args).strip()
    if runtime._backend_busy():
        await runtime._reply_text(update, ui_language.tr("workzone.busy"))
        return
    if arg_text.lower() == "off":
        runtime_session.ensure_store(runtime).set_workzone(session["session_id"], None)
        runtime._workzone_dir = None
        runtime._sync_workzone_to_backend_config()
        backend = runtime.backend_manager.current_backend
        if backend and getattr(backend.capabilities, "supports_sessions", False):
            await backend.handle_new_session()
        await runtime._reply_text(
            update,
            workzone_status_text(
                home_workspace=runtime.workspace_dir,
                current=None,
                notice=ui_language.tr("workzone.returned"),
            ),
            parse_mode="HTML",
        )
        return
    try:
        zone = resolve_workzone_input(arg_text, runtime.global_config.project_root, runtime.workspace_dir)
    except ValueError as exc:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "workzone.not_changed", reason=html.escape(str(exc))
            ),
            parse_mode="HTML",
        )
        return
    runtime_session.ensure_store(runtime).set_workzone(session["session_id"], str(zone))
    runtime._workzone_dir = zone
    runtime._sync_workzone_to_backend_config()
    backend = runtime.backend_manager.current_backend
    if backend and getattr(backend.capabilities, "supports_sessions", False):
        await backend.handle_new_session()
    await runtime._reply_text(
        update,
        workzone_status_text(
            home_workspace=runtime.workspace_dir,
            current=zone,
            notice=ui_language.tr("workzone.updated"),
        ),
        parse_mode="HTML",
    )
