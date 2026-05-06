from __future__ import annotations

import html
from typing import Any

from orchestrator.workzone import (
    access_root_for_workzone,
    build_workzone_prompt,
    clear_workzone,
    load_workzone,
    resolve_workzone_input,
    save_workzone,
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
    runtime._workzone_dir = load_workzone(runtime.workspace_dir)
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
    current = load_workzone(runtime.workspace_dir)
    if not args:
        if current:
            await runtime._reply_text(
                update,
                f"Workzone is ON:\n<code>{html.escape(str(current))}</code>\n\n"
                "Use <code>/workzone off</code> to return to the agent home workspace.",
                parse_mode="HTML",
            )
        else:
            await runtime._reply_text(
                update,
                f"Workzone is OFF. Agent home workspace:\n<code>{html.escape(str(runtime.workspace_dir))}</code>",
                parse_mode="HTML",
            )
        return
    arg_text = " ".join(args).strip()
    if runtime._backend_busy():
        await runtime._reply_text(update, "Workzone change is blocked while a request is running or queued.")
        return
    if arg_text.lower() == "off":
        clear_workzone(runtime.workspace_dir)
        runtime._workzone_dir = None
        runtime._sync_workzone_to_backend_config()
        backend = runtime.backend_manager.current_backend
        if backend and getattr(backend.capabilities, "supports_sessions", False):
            await backend.handle_new_session()
        await runtime._reply_text(
            update,
            f"Workzone OFF. Working directory reset to agent home workspace:\n<code>{html.escape(str(runtime.workspace_dir))}</code>",
            parse_mode="HTML",
        )
        return
    try:
        zone = resolve_workzone_input(arg_text, runtime.global_config.project_root, runtime.workspace_dir)
    except ValueError as exc:
        await runtime._reply_text(update, f"Workzone not changed: {html.escape(str(exc))}", parse_mode="HTML")
        return
    save_workzone(runtime.workspace_dir, zone)
    runtime._workzone_dir = zone
    runtime._sync_workzone_to_backend_config()
    backend = runtime.backend_manager.current_backend
    if backend and getattr(backend.capabilities, "supports_sessions", False):
        await backend.handle_new_session()
    await runtime._reply_text(
        update,
        f"Workzone ON:\n<code>{html.escape(str(zone))}</code>\n\n"
        "Next request will run from this directory and include a workzone prompt.",
        parse_mode="HTML",
    )
