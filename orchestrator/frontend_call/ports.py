"""Adapter into the existing PAO admission/store, not a second chat archive."""

from __future__ import annotations

import inspect
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
                "length their substance requires. Do not include stage directions, performance "
                "cues or parenthetical actions; speak the greeting directly."
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

    def persist_opening(self, owner, binding, text):
        """Record the audible greeting as one canonical assistant message."""

        self.validate(owner, binding)
        try:
            message = self.api.session_store.append_presentation_message(
                session_id=binding["session_id"],
                owner_id=owner,
                agent_id=binding["agent_id"],
                role="assistant",
                text=str(text or "").strip(),
                source="call",
                idempotency_key=f"call-opening:{binding['call_id']}",
                presentation_channel="call",
                history_eligible=False,
                message_context={
                    "call_opening": {
                        "type": "hashi.call-opening",
                        "version": 1,
                        "call_id": binding["call_id"],
                        "client_id": binding["client_id"],
                    }
                },
            )
        except Exception as exc:
            raise CallError("call_opening_persistence_failed", 503) from exc
        if int(message.get("context_generation") or 0) != int(
            binding["context_generation"]
        ):
            raise CallError("call_scope_changed", 409)
        return message

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
                    "session_surface": "workbench",
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

    async def cancel(self, owner, binding, *, request_id, run_id, reason):
        """Cancel exactly one admitted Call Run through PAO's existing fence."""

        try:
            run = self.api.session_store.get_run_by_request(
                str(request_id), owner_id=owner, agent_id=binding["agent_id"]
            )
        except Exception as exc:
            raise CallError("call_run_cancel_scope_changed", 409) from exc
        if (
            run["run_id"] != str(run_id)
            or run["session_id"] != binding["session_id"]
            or run["context_generation"] != binding["context_generation"]
        ):
            raise CallError("call_run_cancel_scope_changed", 409)
        cancel = getattr(self.api, "_cancel_selected_run_result", None)
        if not callable(cancel):
            raise CallError("call_run_cancel_unavailable", 503)
        try:
            result, status = await cancel(
                run,
                owner_id=owner,
                reason=str(reason or "call_ended")[:160],
                source="call.hangup",
            )
        except Exception as exc:
            raise CallError("call_run_cancel_outcome_unknown", 502) from exc
        if status not in {200, 202} or not result.get("ok"):
            raise CallError("call_run_cancel_rejected", int(status or 409))
        return result

    @staticmethod
    def _project_activity(activity, after_sequence):
        activity_sequence = max(0, int(after_sequence or 0))
        events = []
        if not isinstance(activity, dict) or activity.get("ok") is not True:
            return activity_sequence, events
        activity_events = list(activity.get("events") or ())
        if activity_events:
            activity_sequence = max(
                activity_sequence,
                max(int(event.get("sequence") or 0) for event in activity_events),
            )
        else:
            activity_sequence = max(
                activity_sequence, int(activity.get("latest_sequence") or 0)
            )
        for event in activity_events:
            owner_class = str(event.get("delivery_class") or "")
            channel = str(event.get("presentation_channel") or "")
            if (
                owner_class == "answer_preview"
                and channel == "answer"
            ) or (
                owner_class == "user_commentary"
                and channel == "commentary"
            ):
                events.append({
                    key: event.get(key)
                    for key in (
                        "sequence", "event_id", "delivery_class",
                        "presentation_channel", "summary", "detail",
                    )
                })
        return activity_sequence, events

    def _project_result(
        self, owner, binding, run, activity, *, after_sequence=0
    ):
        activity_sequence, events = self._project_activity(activity, after_sequence)
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
        return {
            "terminal": terminal,
            "state": run["state"],
            "text": text,
            "activity_sequence": activity_sequence,
            "events": events,
        }

    async def _worker_result(
        self, owner, binding, run, poll_activity, request_id, after_sequence
    ):
        activity = poll_activity(
            request_id, after_sequence=after_sequence, limit=256
        )
        if inspect.isawaitable(activity):
            activity = await activity
        return self._project_result(
            owner,
            binding,
            run,
            activity,
            after_sequence=after_sequence,
        )

    def result(self, owner, binding, run_id, after_sequence=0):
        run = self.api.session_store.get_run(run_id, owner_id=owner)
        if (
            run["session_id"] != binding["session_id"]
            or run["context_generation"] != binding["context_generation"]
        ):
            raise CallError("call_scope_changed", 409)
        activity_sequence = max(0, int(after_sequence or 0))
        runtime = self.api._runtime_map().get(binding["agent_id"])
        request_id = str(run.get("request_id") or "")
        poll_activity = getattr(runtime, "poll_request_activity", None)
        if callable(poll_activity) and request_id:
            # Isolated Workers own live request activity. The shared-process
            # compatibility store can be present but stale, so never prefer it.
            return self._worker_result(
                owner,
                binding,
                run,
                poll_activity,
                request_id,
                activity_sequence,
            )
        activity = {}
        activity_store = getattr(runtime, "request_activity", None)
        if activity_store is not None and request_id:
            activity = activity_store.poll(
                request_id, after_sequence=activity_sequence, limit=256
            )
        return self._project_result(
            owner,
            binding,
            run,
            activity,
            after_sequence=activity_sequence,
        )
