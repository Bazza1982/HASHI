"""Resolve effective PCM and same-Session history for a Call opening."""

from __future__ import annotations

from pathlib import Path

from .contract import CallError


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
                global_sys_prompt_manager=SysPromptManager.for_instance(api.global_config),
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
                "long stories and detailed explanations are allowed when appropriate."
            ),
            pcm_payload=pcm_payload,
        )
        recent, context_audit = build_live_voice_input(
            pcm_payload, recent_history
        )
        return {
            "instructions": instructions,
            "recent": recent[-4:],
            "context_audit": context_audit,
        }
    except CallError:
        raise
    except Exception as exc:
        raise CallError("call_opening_context_unavailable", 503) from exc
