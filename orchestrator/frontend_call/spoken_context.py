"""Resolve effective PCM and same-Session history for a Call opening."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .contract import CallError


def _parse_timestamp(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _display_timestamp(value: datetime) -> str:
    zone = value.tzname()
    rendered = value.isoformat(timespec="seconds")
    return f"{rendered} ({zone})" if zone else rendered


def build_call_time_context(recent_history, *, now: datetime | None = None) -> str:
    """Describe call time and the last same-Session user message without policy."""

    supplied_now = now is not None
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(current.tzinfo if supplied_now else None)
    previous = next(
        (
            _parse_timestamp(item.get("created_at"))
            for item in reversed(recent_history or [])
            if item.get("role") == "user"
        ),
        None,
    )
    lines = [
        "The user is calling you now.",
        f"Current local date and time: {_display_timestamp(current)}.",
    ]
    if previous is None:
        lines.append("There is no earlier user message in this Conversation Session.")
    else:
        previous = previous.astimezone(current.tzinfo if supplied_now else None)
        lines.append(
            "The last user message in this Conversation Session was at "
            f"{_display_timestamp(previous)}."
        )
    return "\n".join(lines)


def resolve_call_spoken_context(api, owner: str, binding: dict, model: str) -> dict:
    """Build private, tool-free speech context without creating a user Message."""

    try:
        runtime = api._runtime_map()[binding["agent_id"]]
        workspace = Path(getattr(runtime, "workspace_dir"))
        display = getattr(runtime, "get_display_name", None)
        display_name = (
            display()
            if callable(display)
            else str(getattr(runtime, "display_name", None) or binding["agent_id"])
        )

        from orchestrator.bridge_memory import (
            BridgeContextAssembler,
            BridgeMemoryStore,
            SysPromptManager,
        )
        from orchestrator.memory_plus_mode import (
            build_memory_plus_context,
            is_memory_plus_enabled,
            memory_plus_config,
            prepare_memory_plus_store,
        )
        from orchestrator.pcm import canonical_agent_md
        from orchestrator.pcm_voice_projection import (
            build_live_voice_input,
            build_live_voice_instructions,
        )
        from orchestrator.ui_language import preferred_locale

        assembler = getattr(runtime, "context_assembler", None)
        if assembler is None:
            assembler = BridgeContextAssembler(
                BridgeMemoryStore(workspace),
                canonical_agent_md(workspace),
                sys_prompt_manager=SysPromptManager(workspace),
                global_sys_prompt_manager=SysPromptManager.for_instance(
                    api.global_config
                ),
            )

        generation = int(binding["context_generation"])
        extra_sections = []
        if is_memory_plus_enabled(workspace):
            session_workspace = api.session_store.session_workspace(
                binding["session_id"], generation
            )
            memory_plus_cfg = memory_plus_config(workspace)
            memory_plus_state = prepare_memory_plus_store(
                session_workspace, memory_plus_cfg
            )
            extra_sections.append(
                (
                    "Memory+ Continuity",
                    build_memory_plus_context(
                        memory_plus_state,
                        cfg=memory_plus_cfg,
                        include_update_contract=False,
                    ),
                    {"key": "memory_plus_continuity", "protected": True},
                )
            )

        recent_history = api.session_store.recent_history_messages(
            binding["session_id"],
            owner_id=owner,
            context_generation=generation,
            limit=128,
        )
        pcm_payload = assembler.build_prompt_payload(
            "",
            str(model or "call-opening"),
            incremental=False,
            extra_sections=extra_sections,
            inject_memory=False,
            recent_exchanges=[],
            explicit_history_context=False,
        )
        interface_language = preferred_locale(runtime, actor_id=owner)
        instructions = build_live_voice_instructions(
            agent_id=binding["agent_id"],
            display_name=display_name,
            language_instruction=(
                "Use the most recent relevant user's language. If none is available, "
                "use the effective Persona default, then the interface language "
                f"({interface_language})."
            ),
            style_instruction=(
                "Speak naturally for a live call. Match the length to the caller's request; "
                "long stories and detailed explanations are allowed when appropriate. "
                "Do not write stage directions, performance cues or parenthetical actions; "
                "speak the intended words directly."
            ),
            pcm_payload=pcm_payload,
        )
        recent, context_audit = build_live_voice_input(pcm_payload, recent_history)
        return {
            "instructions": instructions,
            "recent": recent[-4:],
            "time_context": build_call_time_context(recent_history),
            "context_audit": context_audit,
        }
    except CallError:
        raise
    except Exception as exc:
        raise CallError("call_opening_context_unavailable", 503) from exc
