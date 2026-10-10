"""Adapter into the existing PAO admission/store, not a second chat archive."""

from __future__ import annotations

import json
from pathlib import Path
from orchestrator.session_store import SESSION_KIND_CONVERSATION
from .contract import CallError
from .spoken_context import resolve_call_spoken_context


class HashiPorts:
    def __init__(self, api):
        self.api = api

    def validate(self, owner, binding):
        if not self.api._persistent_session_v1_ready():
            raise CallError("call_sessions_unavailable", 503)
        try:
            store = self.api.session_store
            session = store.get_session(
                binding["session_id"], owner_id=owner, agent_id=binding["agent_id"]
            )
            if (
                session["session_kind"] != SESSION_KIND_CONVERSATION
                or session["status"] != "active"
                or session["context_generation"] != binding["context_generation"]
            ):
                raise CallError("call_scope_changed", 409)
        except CallError:
            raise
        except Exception as exc:
            raise CallError("call_scope_changed", 409) from exc
        if binding["agent_id"] not in self.api._runtime_map():
            raise CallError("call_agent_unavailable", 503)

    def privacy_level(self, agent):
        runtime = self.api._runtime_map().get(agent)
        manager = getattr(runtime, "backend_manager", None)
        value = getattr(manager, "privacy_level", None)
        if value is None and runtime:
            value = runtime.get_runtime_metadata().get("privacy_level")
        try:
            return int(value)
        except (ValueError, TypeError):
            return None  # Old Worker projections fail closed.

    def phone_busy(self, owner):
        return self.api.live_voice_manager.has_foreground_call(owner)

    async def opening(self, owner, binding, profile, targets):
        """Ask the selected Agent for a tool-free once-only call greeting."""

        self.validate(owner, binding)
        runtime = self.api._runtime_map()[binding["agent_id"]]
        context = resolve_call_spoken_context(
            self.api,
            owner,
            binding,
            str((targets.get("tts") or {}).get("model") or "call-opening"),
        )
        state = {
            "kind": "opening",
            "goal": (
                "The user has just started /call and is calling you now. Answer this call "
                "from this point using the effective Persona, language and form of address. "
                "This opening is a natural greeting, not an answer to an earlier question or task. "
                "After greeting, pause and let the caller speak. Let later answers take the "
                "length their substance requires."
            ),
            "source_context": "",
            "instructions": context["instructions"],
            "recent": context["recent"],
        }
        payload = {
            "owner_id": owner,
            "scope": {
                "type": "hashi.call-speech-scope",
                "version": 1,
                **{
                    key: binding[key]
                    for key in (
                        "call_id",
                        "client_id",
                        "agent_id",
                        "session_id",
                        "context_generation",
                    )
                },
            },
            "state": state,
        }
        try:
            operation = getattr(runtime, "phone_action_operation", None)
            if callable(operation):
                result = await operation("call_speak", payload)
            else:
                from orchestrator.frontend_live_voice.worker_actions import (
                    handle_phone_action_operation,
                )

                result = await handle_phone_action_operation(
                    runtime, "call_speak", payload
                )
        except Exception as exc:
            code = str(getattr(exc, "code", "call_opening_failed"))
            if not code.startswith(("call_", "live_")):
                code = "call_opening_failed"
            raise CallError(code, int(getattr(exc, "status", 502))) from exc
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise CallError("call_opening_invalid", 502)
        return text.strip()

    async def admit(self, owner, binding, turn_id, text, observation, captured_at):
        self.validate(owner, binding)
        runtime = self.api._runtime_map()[binding["agent_id"]]
        # Speech stays verbatim; sealed media facts enter PCM through its typed
        # current-message projection, never through another user instruction.
        execution_text = text
        from orchestrator.message_context import CONNECTOR_EVIDENCE_METADATA_KEY, seal_connector_evidence
        media = {"version": 2, "call_id": binding["call_id"], "turn_id": turn_id,
                 **binding.get("call_context", {}), "captured_at": captured_at, "observation": observation}
        evidence = seal_connector_evidence(Path(self.api.config_path).parent,
                                           claims={"call_media": media}, prompt=execution_text)
        if evidence is None:
            raise CallError("call_context_unqualified", 503)
        content = [{"type": "text", "text": execution_text}]
        try:
            request_id = await runtime.enqueue_request(
                runtime._primary_chat_id(),
                execution_text,
                "session-api",
                text[:160],
                deliver_to_telegram=True,
                idempotency_key=f"call:{binding['call_id']}:{turn_id}",
                request_metadata={
                    "session_id": binding["session_id"],
                    "owner_id": owner,
                    "session_surface": "session-api",
                    "session_channel_key": binding["client_id"],
                    "session_message_text": execution_text,
                    "session_message_display_text": text,
                    "session_message_content": content,
                    "frontend_client": {
                        "kind": "session_api",
                        "client_id": binding["client_id"],
                    },
                    "session_context_generation": binding["context_generation"],
                    CONNECTOR_EVIDENCE_METADATA_KEY: evidence,
                },
            )
            if not request_id:
                raise CallError("call_admission_rejected", 409)
            run = self.api.session_store.get_run_by_request(str(request_id))
        except CallError:
            raise
        except Exception as exc:
            raise CallError("call_admission_outcome_unknown", 502) from exc
        if (
            run["session_id"] != binding["session_id"]
            or run["context_generation"] != binding["context_generation"]
        ):
            raise CallError("call_scope_changed", 409)
        return {
            "request_id": str(request_id),
            "run_id": run["run_id"],
            "message_id": run["user_message_id"],
        }

    def result(self, owner, binding, run_id):
        run = self.api.session_store.get_run(run_id, owner_id=owner)
        if (
            run["session_id"] != binding["session_id"]
            or run["context_generation"] != binding["context_generation"]
        ):
            raise CallError("call_scope_changed", 409)
        terminal = run["state"] in {
            "completed",
            "failed",
            "stopped",
            "superseded",
            "interrupted",
        }
        text = ""
        if terminal and run.get("final_message_id"):
            message = self.api.session_store.get_message(
                run["final_message_id"],
                session_id=binding["session_id"],
                owner_id=owner,
            )
            text = str(message.get("text") or "")
        return {"terminal": terminal, "state": run["state"], "text": text}
