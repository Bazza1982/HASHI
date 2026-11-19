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

    def frontend_subscribers(self, owner, binding):
        """Freeze optional FC subscribers without exposing them to CallService."""

        del binding
        from orchestrator.frontend_optional_egress import (
            configured_owner_subscribers,
        )

        return configured_owner_subscribers(self.api, owner)

    async def call_output(self, owner, binding, *, generation):
        from orchestrator.frontend_optional_egress import resolve_call_output

        return await resolve_call_output(
            self.api, owner, binding, generation=generation
        )

    def emit_frontend_event(
        self,
        owner,
        binding,
        kind,
        *,
        subscribers=(),
        payload=None,
    ):
        """Project a Call-domain event into the optional FC fan-out."""

        from orchestrator.frontend_optional_egress import (
            schedule_call_frontend_event,
        )

        return schedule_call_frontend_event(
            self.api,
            owner_id=owner,
            binding=binding,
            kind=kind,
            subscribers=subscribers,
            payload=payload,
        )

    async def opening(self, owner, binding, profile, targets, visual_context=None):
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
                "Use time_context to respond appropriately and naturally, together with the "
                "existing Persona, relationship, language and recent conversation context. "
                "Do not mention the timestamps unless they are naturally relevant. "
                "This opening is a natural greeting, not an answer to an earlier question or task. "
                "If source_context contains a current camera observation, treat it as silent context. "
                "Do not announce that video connected or describe ordinary appearance, clothing, "
                "expression or surroundings. Mention visual information only when it is exceptionally noticeable, "
                "immediately relevant and something a polite human would naturally mention. "
                "After greeting, pause and let the caller speak. Let later answers take the "
                "length their substance requires. Do not include stage directions, performance "
                "cues or parenthetical actions; speak the greeting directly."
            ),
            "time_context": context["time_context"],
            "source_context": visual_context or "",
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

    async def admit(
        self,
        owner,
        binding,
        turn_id,
        text,
        observation,
        captured_at,
        frames=(),
    ):
        self.validate(owner, binding)
        runtime = self.api._runtime_map()[binding["agent_id"]]
        # Speech stays verbatim; sealed media facts enter PCM through its typed
        # current-message projection, never through another user instruction.
        execution_text = text
        from orchestrator.message_context import (
            CONNECTOR_EVIDENCE_METADATA_KEY,
            seal_connector_evidence,
        )

        from orchestrator.frontend_call.transient_live_media import (
            TRANSIENT_LIVE_FRAMES_METADATA_KEY,
            encode_live_frames,
            live_frame_facts,
        )

        selected_frames = list(frames)
        frame_limit = binding.get("call_context", {}).get("max_frames_per_turn", 10)
        chat_id = runtime._primary_chat_id()
        idempotency_key = f"call:{binding['call_id']}:{turn_id}"

        def metadata_for(current_frames, *, empty_frame_envelope=False):
            media = {
                "version": 2,
                "call_id": binding["call_id"],
                "turn_id": turn_id,
                **binding.get("call_context", {}),
                "captured_at": captured_at,
                "observation": observation,
                "recent_frames": live_frame_facts(
                    current_frames,
                    window_seconds=binding.get("call_context", {}).get(
                        "sample_window_seconds", 10
                    ),
                ),
            }
            evidence = seal_connector_evidence(
                Path(self.api.config_path).parent,
                claims={"call_media": media},
                prompt=execution_text,
            )
            if evidence is None:
                raise CallError("call_context_unqualified", 503)
            request_metadata = {
                "session_id": binding["session_id"],
                "owner_id": owner,
                "session_surface": "workbench",
                "session_channel_key": binding["client_id"],
                "session_message_text": execution_text,
                "session_message_display_text": text,
                "session_message_content": [{"type": "text", "text": execution_text}],
                "frontend_client": {
                    "kind": "session_api",
                    "client_id": binding["client_id"],
                },
                "session_context_generation": binding["context_generation"],
                CONNECTOR_EVIDENCE_METADATA_KEY: evidence,
            }
            if current_frames:
                request_metadata[TRANSIENT_LIVE_FRAMES_METADATA_KEY] = (
                    encode_live_frames(
                        () if empty_frame_envelope else current_frames,
                        max_frames=frame_limit,
                    )
                )
            return request_metadata

        # Older Workers need the original single-frame fallback. A Worker that
        # advertises bounded logical requests can receive all selected samples
        # without treating the physical frame size as a content limit.
        from orchestrator import function_worker_protocol as ipc
        from orchestrator.function_worker_large_request import LARGE_REQUEST_VERSION
        from orchestrator.function_worker_supervisor import AgentRuntimeHandle

        def worker_envelope(metadata):
            # Keep this in step with AgentRuntimeHandle.enqueue_request. The
            # real protocol uses a 32-character UUID hex request identifier.
            params = {
                "chat_id": int(chat_id),
                "prompt": str(execution_text),
                "source": "session-api",
                "summary": str(text[:160]),
                "silent": False,
                "is_retry": False,
                "deliver_to_telegram": True,
                "skip_memory_injection": False,
                "habit_learning_eligible": True,
                "skill_id": None,
                "scheduler_context": {},
                "request_metadata": metadata,
                "request_content": None,
                "idempotency_key": idempotency_key,
                "replay_only": False,
            }
            return {
                "version": ipc.FUNCTION_WORKER_PROTOCOL_VERSION,
                "kind": ipc.REQUEST,
                "id": "0" * 32,
                "method": "runtime.enqueue_request",
                "params": params,
            }

        advertised = (
            runtime.metadata.get("worker_large_request")
            if isinstance(runtime, AgentRuntimeHandle)
            else None
        )
        supports_large = (
            isinstance(advertised, dict)
            and type(advertised.get("version")) is int
            and advertised["version"] == LARGE_REQUEST_VERSION
        )
        if isinstance(runtime, AgentRuntimeHandle) and not supports_large:

            def row_wire_size(frame):
                placeholder = {**frame, "image": b"x"}
                row = encode_live_frames((placeholder,), max_frames=frame_limit)[
                    "frames"
                ][0]
                base = len(
                    json.dumps(
                        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                )
                return base - 4 + 4 * ((len(frame["image"]) + 2) // 3)

            wire_costs = {id(frame): row_wire_size(frame) for frame in selected_frames}
            while True:
                empty = metadata_for(selected_frames, empty_frame_envelope=True)
                try:
                    base_size = len(ipc.encode_envelope(worker_envelope(empty)))
                except ipc.FunctionWorkerProtocolError as exc:
                    if not str(exc).startswith("IPC frame exceeds"):
                        raise
                    if not selected_frames:
                        raise CallError("call_ipc_text_too_large", 413) from exc
                    base_size = ipc.MAX_FRAME_BYTES + 1
                frame_bytes = sum(wire_costs[id(frame)] for frame in selected_frames)
                frame_bytes += max(0, len(selected_frames) - 1)
                if base_size + frame_bytes <= ipc.MAX_FRAME_BYTES:
                    request_metadata = metadata_for(selected_frames)
                    try:
                        ipc.encode_envelope(worker_envelope(request_metadata))
                        break
                    except ipc.FunctionWorkerProtocolError as exc:
                        if (
                            not str(exc).startswith("IPC frame exceeds")
                            or not selected_frames
                        ):
                            raise
                        # The complete serialized check remains authoritative.
                        frame_bytes = ipc.MAX_FRAME_BYTES - base_size + 1
                if not selected_frames:
                    raise CallError("call_ipc_text_too_large", 413)
                # Batch removals avoid quadratic work for explicitly selected
                # larger sample counts. For equal sizes, retain wider coverage.
                middle = (len(selected_frames) - 1) / 2
                ranked = sorted(
                    range(len(selected_frames)),
                    key=lambda index: (
                        wire_costs[id(selected_frames[index])],
                        -abs(index - middle),
                    ),
                    reverse=True,
                )
                removed = set()
                remaining_count = len(selected_frames)
                for index in ranked:
                    if frame_bytes <= ipc.MAX_FRAME_BYTES - base_size:
                        break
                    frame_bytes -= wire_costs[id(selected_frames[index])]
                    if remaining_count > 1:
                        frame_bytes -= 1
                    remaining_count -= 1
                    removed.add(index)
                selected_frames = [
                    frame
                    for index, frame in enumerate(selected_frames)
                    if index not in removed
                ]
        else:
            request_metadata = metadata_for(selected_frames)
        try:
            request_id = await runtime.enqueue_request(
                chat_id,
                execution_text,
                "session-api",
                text[:160],
                deliver_to_telegram=True,
                idempotency_key=idempotency_key,
                request_metadata=request_metadata,
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
            "_admitted_frame_sequences": [
                frame["frame_sequence"] for frame in selected_frames
            ],
            "_omitted_frames_for_transport": len(frames) - len(selected_frames),
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
            if (owner_class == "answer_preview" and channel == "answer") or (
                owner_class == "user_commentary" and channel == "commentary"
            ):
                events.append(
                    {
                        key: event.get(key)
                        for key in (
                            "sequence",
                            "event_id",
                            "delivery_class",
                            "presentation_channel",
                            "summary",
                            "detail",
                        )
                    }
                )
        return activity_sequence, events

    def _project_result(self, owner, binding, run, activity, *, after_sequence=0):
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
        activity = poll_activity(request_id, after_sequence=after_sequence, limit=256)
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
