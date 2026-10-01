"""Authoritative Live Voice state, provider sideband, and PAO admission."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
from pathlib import Path
import sqlite3
from time import monotonic
from typing import Any
from uuid import uuid4

import aiohttp

from .audit import LiveVoiceAuditLog, exception_evidence
from .delegation import Proposal, build_proposal
from .delegation_policy import (
    DelegationRoute,
    decision_shape,
    parse_decision,
)
from .actions import PhoneActions
from .provider import VoiceProvider, default_registry, select_provider
from .result_outline import numbered_source_outline
from .opening import CallOpening, OpeningAudioObservation, new_opening, opening_goal
from .ports import AdmissionPort, DurableVoicePort, LiveApplicationPort
from .protocol import CallBinding, Fragment, LiveVoiceError, identifier, normalize_transcript, positive_int, stable_digest
from .service import LiveVoiceEventService
from orchestrator.session_store import SessionConflict, TERMINAL_RUN_STATES
from tools.token_tracker import estimate_tokens

logger = logging.getLogger(__name__)


async def _wait_for_monotonic_deadline(deadline: float, *, clock=monotonic, sleep=asyncio.sleep) -> None:
    # Windows event loops may wake up one timer tick early. An awaited sleep is
    # not proof that the no-output observation window has actually elapsed.
    while (remaining := deadline - clock()) > 0:
        await sleep(max(0.02, remaining))


ACTIVE_PHASES = {"connecting", "active", "ending", "recovering"}
PROVIDER_SESSION_MAX_DURATION_SECONDS = 1800
PROVIDER_SESSION_ROLLOVER_MARGIN_SECONDS = 90
TERMINAL_PHASES = {"ended", "failed", "interrupted"}
CALL_EVENT_SCHEMA = "hashi.live_voice.event.v1"
LIVE_UPDATE_TOKEN_LIMIT = 320
LIVE_UPDATE_BYTE_LIMIT = 512
MAX_RESULT_PAGE_CHARS = 12_000
CLIENT_OBSERVATION_EVENTS = frozenset({
    "client.data_channel_state",
    "client.event_poll_failed",
    "client.event_poll_recovered",
    "client.heartbeat_failed",
    "client.heartbeat_recovered",
    "client.ice_connection_state",
    "client.ice_gathering_state",
    "client.media_track_state",
    "client.media_ready",
    "client.user_speech_started",
    "client.playback_started",
    "client.playback_progress",
    "client.network_state",
    "client.page_visibility",
    "client.peer_connection_state",
    "client.playback_blocked",
    "client.scope_departure",
    "client.session_started",
    "client.start_failed",
    "client.termination_requested",
})
CLIENT_OBSERVATION_DETAIL_KEYS = frozenset({
    "consecutive_failures", "error_code", "initiator", "online", "reason",
    "state", "visible", "input_active", "playback_unlocked", "current_time_ms", "opening_id",
})
TERMINATION_INITIATORS = frozenset({"client_fault", "system", "unknown", "user"})
TERMINATION_REASONS = frozenset({
    "component_dispose", "data_channel_closed", "data_channel_error",
    "lease_expired", "legacy_unspecified", "maximum_duration_reached",
    "media_track_ended", "network_offline",
    "page_unload", "peer_connection_closed", "peer_connection_failed",
    "provider_closed", "scope_departure", "service_shutdown", "sideband_disconnected",
    "start_cancelled", "startup_failed", "user_hangup",
})


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now() -> str:
    return _utc_now_dt().isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _public_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    result = {
        key: item for key, item in value.items()
        if isinstance(key, str) and len(key) <= 80
        and isinstance(item, int) and not isinstance(item, bool) and item >= 0
    }
    return result or None


def _event_usage(event: Mapping[str, Any]) -> dict[str, int] | None:
    direct = _public_usage(event.get("usage"))
    session = event.get("session")
    return direct or (_public_usage(session.get("usage")) if isinstance(session, Mapping) else None)


def _live_text_chunks(
    value: Any,
    *,
    token_limit: int = LIVE_UPDATE_TOKEN_LIMIT,
    byte_limit: int = LIVE_UPDATE_BYTE_LIMIT,
) -> list[str]:
    """Split text conservatively below the provider's 500-token append ceiling.

    The local token estimate is useful for metering but is not the provider's
    tokenizer. The byte ceiling keeps CJK, emoji, paths, and Markdown from
    producing a nominally valid chunk that the provider rejects.
    """

    remaining = str(value or "").strip()
    chunks: list[str] = []

    def fits(text: str) -> bool:
        return (
            estimate_tokens(text) <= token_limit
            and len(text.encode("utf-8")) <= byte_limit
        )

    while remaining:
        if fits(remaining):
            chunks.append(remaining)
            break
        low, high, accepted = 1, len(remaining), 1
        while low <= high:
            middle = (low + high) // 2
            if fits(remaining[:middle]):
                accepted = middle
                low = middle + 1
            else:
                high = middle - 1
        boundary = max(1, accepted)
        preferred = max(
            remaining.rfind("\n", 0, boundary),
            remaining.rfind(" ", 0, boundary),
        )
        if preferred >= max(1, boundary // 2):
            boundary = preferred + 1
        chunks.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    return [chunk for chunk in chunks if chunk]


def _provider_client_event_id(event: Mapping[str, Any]) -> str:
    """Return the outgoing client event correlated by an ack or error."""

    error = event.get("error")
    nested = error if isinstance(error, Mapping) else {}
    for candidate in (
        event.get("client_event_id"),
        nested.get("client_event_id"),
        nested.get("event_id"),
        event.get("event_id"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _provider_error_code(event: Mapping[str, Any]) -> str:
    error = event.get("error")
    candidate = error.get("code") if isinstance(error, Mapping) else None
    value = str(candidate or "provider_error")
    return value if value.replace("_", "").replace("-", "").isalnum() else "provider_error"


def _provider_close_reason(event: Mapping[str, Any]) -> str:
    session = event.get("session")
    nested = session if isinstance(session, Mapping) else {}
    candidate = nested.get("reason") or event.get("reason") or "unspecified"
    value = str(candidate or "unspecified").strip().casefold()
    return value if value.replace("_", "").replace("-", "").isalnum() else "unspecified"


class LiveVoiceManager(DurableVoicePort, AdmissionPort, LiveApplicationPort):
    def __init__(
        self,
        session_store: Any,
        global_config: Any,
        secrets: Mapping[str, Any] | None = None,
        *,
        admit_run: Callable[[CallBinding, Proposal, str], Awaitable[Mapping[str, Any]]] | None = None,
        poll_run_activity: Callable[[CallBinding, str, int, int], Awaitable[Mapping[str, Any]]] | None = None,
        judge_action: Callable[[CallBinding, Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
        inspect_action_results: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        cancel_action_run: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
        resolve_phone_session: Callable[..., Mapping[str, Any]] | None = None,
        proposal_grace_seconds: float = 0.25,
        control_timeout_seconds: float = 5.0,
        close_timeout_seconds: float = 8.0,
        provider_registry: Mapping[str, VoiceProvider] | None = None,
        opening_grace_seconds: float = 0.25,
    ):
        self.session_store = session_store
        self.global_config = global_config
        self.secrets = dict(secrets or {})
        self.providers = dict(default_registry() if provider_registry is None else provider_registry)
        self._connection_providers: dict[str, VoiceProvider] = {}
        self._provider_native_delegations: dict[tuple[str, int], set[str]] = {}
        self.instance_id = str(getattr(global_config, "instance_id", "HASHI") or "HASHI").upper()
        self.instance_generation = str(getattr(global_config, "instance_generation", "1") or "1")
        self._feature_enabled = bool(getattr(global_config, "live_voice_v1", False))
        self._availability_override: bool | None = None
        self._admit_run = admit_run
        self._poll_run_activity = poll_run_activity
        self._judge_action = judge_action
        self._inspect_action_results = inspect_action_results
        self._cancel_action_run = cancel_action_run
        self.actions = PhoneActions(session_store)
        self._action_locks: dict[str, asyncio.Lock] = {}
        self._utterance_tasks: dict[str, asyncio.Task[Any]] = {}
        self._utterance_ends: dict[str, int] = {}
        self._resolve_phone_session = resolve_phone_session
        self._proposal_grace_seconds = max(0.0, float(proposal_grace_seconds))
        self._control_timeout_seconds = max(0.05, float(control_timeout_seconds))
        self._close_timeout_seconds = max(0.05, float(close_timeout_seconds))
        self._opening_grace_seconds = max(0.0, float(opening_grace_seconds))
        self._opening_output_timeout_seconds = 8.0
        self._opening_audio: dict[tuple[str, int, str], OpeningAudioObservation] = {}
        configured_logs = getattr(global_config, "base_logs_dir", None)
        default_logs = Path(self.session_store.db_path).parent.parent / "logs"
        self.audit = LiveVoiceAuditLog(Path(configured_logs or default_logs) / "voice_sessions")
        self.opening = CallOpening(self.session_store, self.audit)
        self._opening_tasks: set[asyncio.Task[Any]] = set()
        self.service = LiveVoiceEventService(self)
        self._sideband_tasks: set[asyncio.Task[Any]] = set()
        self._provider_event_queues: dict[
            str, asyncio.Queue[tuple[CallBinding, Mapping[str, Any]]]
        ] = {}
        self._provider_event_tasks: dict[str, asyncio.Task[Any]] = {}
        self._provider_event_tasks_set: set[asyncio.Task[Any]] = set()
        self._terminal_record_tasks: set[asyncio.Task[Any]] = set()
        self._proposal_tasks: set[asyncio.Task[Any]] = set()
        self._relay_tasks: set[asyncio.Task[Any]] = set()
        self._reply_inflight: set[tuple[str, str]] = set()
        self._action_reply_timeout_seconds = 8.0
        self._foreground_tasks: set[asyncio.Task[Any]] = set()
        self._foreground_call_tasks: dict[str, asyncio.Task[Any]] = {}
        self._recovery_tasks: set[asyncio.Task[Any]] = set()
        self._active_sockets: dict[str, Any] = {}
        self._sideband_ready_events: dict[str, asyncio.Event] = {}
        self._sideband_failures: dict[str, str] = {}
        self._session_closed_events: dict[str, asyncio.Event] = {}
        self._control_waiters: dict[tuple[str, str], asyncio.Future[bool]] = {}
        self._update_waiters: dict[tuple[str, str], asyncio.Future[bool]] = {}
        self._attempt_sdp_answers: dict[str, str] = {}
        self._sweeper_task: asyncio.Task[Any] | None = None
        self._closing = False
        self._sanitize_persisted_attempts()

    @property
    def available(self) -> bool:
        if self._availability_override is not None:
            return self._availability_override
        return self._feature_enabled and any(adapter.credential(self.secrets) for adapter in self.providers.values())

    @available.setter
    def available(self, value: bool) -> None:
        self._availability_override = bool(value)

    def _provider_for(self, binding: CallBinding | None = None, *, provider_session_id: str = "") -> VoiceProvider:
        identity = provider_session_id or (binding.provider_session_id if binding else "")
        if identity in self._connection_providers:
            return self._connection_providers[identity]
        public = None
        if binding is not None:
            with self.session_store._lock, self.session_store._connection() as connection:
                row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
            if row is not None:
                public = self._stored_phone_public(row)
        # Records made before the provider boundary exclusively used this provider.
        provider_id = str((public or {}).get("provider") or "openai")
        adapter = select_provider(self.providers, provider_id)
        version = (public or {}).get("adapter_version")
        if version and version != adapter.capabilities.version:
            raise LiveVoiceError("live_provider_version_unavailable", 503)
        return adapter

    def _get_api_key(self, binding: CallBinding | None = None) -> str:
        return self._provider_for(binding).credential(self.secrets)

    def _phone_session(
        self,
        agent_id: str,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
        context_generation: int | None = None,
        frozen_public: Mapping[str, Any] | None = None,
        frozen_selection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not callable(self._resolve_phone_session):
            raise LiveVoiceError("live_phone_configuration_unavailable", 503)
        try:
            resolver = self._resolve_phone_session
            parameters = inspect.signature(resolver).parameters
            accepts_scope = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ) or all(
                key in parameters
                for key in ("owner_id", "session_id", "context_generation")
            )
            selection_kwargs = {}
            if frozen_selection is not None and (
                "frozen_selection" in parameters
                or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
            ):
                selection_kwargs["frozen_selection"] = frozen_selection
            value = (
                resolver(
                    agent_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    context_generation=context_generation,
                    **selection_kwargs,
                )
                if accepts_scope
                else resolver(agent_id)
            )
        except LiveVoiceError:
            raise
        except Exception as exc:
            code = str(getattr(exc, "code", "live_phone_configuration_invalid"))
            if not code.startswith(("phone_", "pcm_")):
                code = "live_phone_configuration_invalid"
            raise LiveVoiceError(code, 503) from exc
        if not isinstance(value, Mapping):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        value = dict(value)
        if frozen_public is not None:
            for key in ("provider", "model", "voice"):
                value[key] = frozen_public[key]
            value["public"] = dict(frozen_public)
        provider = str(value.get("provider") or "")
        model = str(value.get("model") or "")
        voice = str(value.get("voice") or "")
        instructions = value.get("instructions")
        public = value.get("public")
        adapter = select_provider(self.providers, provider)
        adapter.validate_selection(model, voice)
        if not isinstance(instructions, str) or not instructions:
            raise LiveVoiceError("live_instructions_invalid", 503)
        input_messages = value.get("input", [])
        if not isinstance(input_messages, list):
            raise LiveVoiceError("live_input_invalid", 503)
        adapter.validate_session(instructions, model, voice, input_messages)
        validated = input_messages
        if not isinstance(public, Mapping):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        revision = str(public.get("revision") or "")
        if len(revision) != 64 or any(character not in "0123456789abcdef" for character in revision):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        if any(str(public.get(key) or "") != expected for key, expected in {
            "provider": provider, "model": model, "voice": voice,
        }.items()):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        version = public.get("adapter_version")
        if version and version != adapter.capabilities.version:
            raise LiveVoiceError("live_provider_version_unavailable", 503)
        allowed_public = {
            key: public[key]
            for key in (
                "revision", "provider", "provider_label", "model", "model_label",
                "voice", "voice_label", "voice_presentation", "language", "language_label", "style",
                "style_label", "custom_style", "persona_projected", "adapter_version", "interface_language",
            )
            if key in public
        }
        allowed_public["adapter_version"] = adapter.capabilities.version
        return {
            "adapter": adapter,
            "media": adapter.media_descriptor(),
            "provider": provider,
            "model": model,
            "voice": voice,
            "instructions": instructions,
            "input": validated,
            "context_audit": dict(value.get("context_audit") or {}),
            "public": allowed_public,
            "selection": {
                key: value["selection"][key]
                for key in ("schema_version", "provider", "model", "voice", "language", "style", "style_instructions", "interface_language")
                if isinstance(value.get("selection"), Mapping) and key in value["selection"]
            },
        }

    @staticmethod
    def _stored_phone_public(row: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            value = json.loads(row["phone_config_json"] or "{}")
        except (KeyError, TypeError, ValueError):
            return None
        public = value.get("public") if isinstance(value, Mapping) else None
        return dict(public) if isinstance(public, Mapping) else None

    def _schedule_opening(self, binding: CallBinding) -> None:
        if self._closing:
            return
        ws = self._active_sockets.get(binding.call_id)
        if ws is None or getattr(ws, "closed", True):
            return
        state = self.opening.change(binding, "claim")
        if not state:
            return
        self._track(asyncio.create_task(
            self._request_opening(binding, state["opening_id"]),
            name=f"hashi-live-opening-{binding.call_id}",
        ), self._opening_tasks, binding=binding)

    async def _request_opening(self, binding: CallBinding, opening_id: str) -> None:
        audio_key = (binding.call_id, binding.call_epoch, binding.provider_session_id)
        observation = OpeningAudioObservation(maximum_arrival_gap=min(1.0, self._opening_output_timeout_seconds / 2))
        self._opening_audio[audio_key] = observation
        try:
            for attempt in (1, 2):
                # Both the first request and the single proven-silent
                # continuation yield to the caller before sending anything.
                await asyncio.sleep(self._opening_grace_seconds)
                if not self._is_current_provider_binding(binding):
                    return
                with self.session_store._lock, self.session_store._connection() as connection:
                    row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
                state = self.opening.read(binding)
                if row is None or row["phase"] != "active" or state.get("state") != "requested":
                    return
                if attempt == 2 and observation.silence_proof(elapsed_seconds=self._opening_output_timeout_seconds) is None:
                    self.opening.change(binding, "uncertain", reason="opening_silence_evidence_changed")
                    return
                event_id = opening_id if attempt == 1 else opening_id + "-continuation"
                adapter = self._provider_for(binding)
                payload = adapter.opening(opening_goal(self._stored_phone_public(row) or {}, continuation=attempt == 2), event_id)
                ws = self._active_sockets.get(binding.call_id)
                if ws is None or getattr(ws, "closed", True):
                    self.opening.change(binding, "uncertain", reason="sideband_unavailable")
                    return
                identity = (binding.call_id, event_id)
                waiter = asyncio.get_running_loop().create_future()
                self._update_waiters[identity] = waiter
                try:
                    sent = self.opening.change(binding, "sent", event_id=event_id)
                    if sent.get("state") != "requested" or not sent.get("request_sent"):
                        return
                    await asyncio.wait_for(ws.send_json(payload), timeout=self._control_timeout_seconds)
                    accepted = await asyncio.wait_for(waiter, timeout=self._control_timeout_seconds)
                    self.opening.change(binding, "accepted" if accepted else "rejected")
                    if not accepted:
                        return
                finally:
                    self._update_waiters.pop(identity, None)
                if attempt == 1:
                    observation.acknowledge()
                await _wait_for_monotonic_deadline(monotonic() + self._opening_output_timeout_seconds)
                if not self._is_current_provider_binding(binding):
                    return
                state = self.opening.read(binding)
                if state.get("state") != "accepted" or state.get("output_observed") or state.get("user_started"):
                    return
                proof = observation.silence_proof(elapsed_seconds=self._opening_output_timeout_seconds)
                self.audit.record(binding, "opening.output_window", opening_id=opening_id,
                    attempt=attempt, outcome="continuous_silence" if proof else "unconfirmed",
                    reason="non_silent_audio" if observation.non_silent else "audio_observation_unknown"
                    if observation.uncertain else "observation_window",
                    continuation_evidence=proof)
                if attempt != 1 or proof is None:
                    self.opening.change(binding, "uncertain", reason="opening_output_unconfirmed")
                    return
                if not self.opening.change(binding, "continuation.claim", silence_proof=proof):
                    return
        except asyncio.CancelledError:
            if not self.opening.read(binding).get("output_observed"):
                self.opening.change(binding, "uncertain", reason="opening_interrupted")
            raise
        except Exception as exc:
            self.opening.change(binding, "uncertain", reason="opening_confirmation_unavailable")
            self.audit.record(binding, "opening.failed", **exception_evidence(exc))
        finally:
            if self._opening_audio.get(audio_key) is observation:
                self._opening_audio.pop(audio_key, None)

    def _observe_opening_provider(self, binding: CallBinding, event: Mapping[str, Any]) -> None:
        try:
            kind = event.get("type")
            if kind == "provider.ready":
                self.opening.change(binding, "provider.ready")
                self._schedule_opening(binding)
            elif kind == "conversation.user.delta" and str(event.get("delta") or "").strip():
                self.opening.change(binding, "user.started")
            elif kind == "conversation.assistant.delta":
                fragment = normalize_transcript(event)
                if fragment is not None and fragment.text.strip():
                    self.opening.change(binding, "speech.generated")
            elif kind in {"update.accepted", "provider.error"}:
                state = self.opening.read(binding)
                if state and _provider_client_event_id(event) == state.get("request_event_id", state.get("opening_id")):
                    self.opening.change(binding, "accepted" if kind == "update.accepted" else "rejected")

        except Exception as exc:
            # Opening is optional conversation presentation. Its persistence or
            # provider failure cannot tear down the ongoing audio transport.
            logger.warning("Phone opening observation failed safely: %s", type(exc).__name__)
            with suppress(Exception):
                self.audit.record(binding, "opening.observation_failed", **exception_evidence(exc))

    def _track(
        self,
        task: asyncio.Task[Any],
        collection: set[asyncio.Task[Any]],
        *,
        binding: CallBinding | None = None,
    ) -> asyncio.Task[Any]:
        collection.add(task)
        def finished(done: asyncio.Task[Any]) -> None:
            collection.discard(done)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.warning("Tracked Live Voice task failed safely: %s", type(error).__name__)
                if binding is not None:
                    self.audit.record(
                        binding,
                        "background.task_failed",
                        task_name=done.get_name(),
                        **exception_evidence(error),
                    )
        task.add_done_callback(finished)
        return task

    def _enqueue_provider_event(self, binding: CallBinding, event: Mapping[str, Any]) -> None:
        queue = self._provider_event_queues.setdefault(binding.call_id, asyncio.Queue())
        queue.put_nowait((binding, dict(event)))
        task = self._provider_event_tasks.get(binding.call_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._run_provider_event_queue(binding.call_id, queue),
            name=f"hashi-live-provider-events-{binding.call_id}",
        )
        self._provider_event_tasks[binding.call_id] = task
        self._track(task, self._provider_event_tasks_set, binding=binding)

    async def _run_provider_event_queue(
        self,
        call_id: str,
        queue: asyncio.Queue[tuple[CallBinding, Mapping[str, Any]]],
    ) -> None:
        current: tuple[CallBinding, Mapping[str, Any]] | None = None
        attempts = 0
        try:
            while True:
                if self._closing and queue.empty():
                    return
                try:
                    current = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if queue.empty():
                        with self.session_store._lock, self.session_store._connection() as connection:
                            call = connection.execute(
                                "SELECT phase FROM live_calls WHERE call_id = ?", (call_id,)
                            ).fetchone()
                        if call is None or str(call["phase"]) in TERMINAL_PHASES:
                            return
                    continue

                binding, event = current
                event_type = str(event.get("type") or "unknown")
                event_id = str(event.get("event_id") or "")[:160]
                attempts = 0
                while True:
                    try:
                        await self.service.on_provider_event(binding, event)
                    except asyncio.CancelledError:
                        if attempts:
                            try:
                                self.audit.record(
                                    binding, "provider.event_persistence_interrupted",
                                    provider_event_type=event_type,
                                    provider_event_id=event_id,
                                    failed_attempts=attempts,
                                )
                            except Exception:
                                logger.exception(
                                    "Unable to audit interrupted provider event persistence for %s",
                                    binding.call_id,
                                )
                        raise
                    except (LiveVoiceError, SessionConflict) as exc:
                        self.audit.record(
                            binding, "provider.event_persistence_rejected",
                            provider_event_type=event_type,
                            provider_event_id=event_id,
                            error_code=getattr(exc, "code", "session_conflict"),
                            reason="non_retryable",
                        )
                        break
                    except Exception as exc:
                        attempts += 1
                        retry_delay = min(0.1 * attempts, 5.0)
                        if attempts == 1 or attempts % 12 == 0:
                            try:
                                self.audit.record(
                                    binding, "provider.event_processing_failed",
                                    provider_event_type=event_type,
                                    provider_event_id=event_id,
                                    attempt=attempts,
                                    retry_delay_s=round(retry_delay, 2),
                                    **exception_evidence(exc),
                                )
                            except Exception:
                                logger.exception(
                                    "Unable to audit provider event persistence failure for %s",
                                    binding.call_id,
                                )
                            logger.warning(
                                "Live Voice event persistence is retrying for %s (%s), attempt %s",
                                binding.call_id, type(exc).__name__, attempts,
                            )
                        await asyncio.sleep(retry_delay)
                    else:
                        if attempts:
                            try:
                                self.audit.record(
                                    binding, "provider.event_persistence_recovered",
                                    provider_event_type=event_type,
                                    provider_event_id=event_id,
                                    attempts=attempts + 1,
                                )
                            except Exception:
                                logger.exception(
                                    "Unable to audit provider event persistence recovery for %s",
                                    binding.call_id,
                                )
                        break
                queue.task_done()
                current = None
                attempts = 0
                if queue.empty():
                    with self.session_store._lock, self.session_store._connection() as connection:
                        call = connection.execute(
                            "SELECT * FROM live_calls WHERE call_id = ?", (call_id,)
                        ).fetchone()
                    if call is not None and str(call["phase"]) in TERMINAL_PHASES:
                        self._persist_call_record(self._binding_from_row(call))
                        return
        finally:
            if current is not None:
                with suppress(ValueError):
                    queue.task_done()
            if self._provider_event_tasks.get(call_id) is asyncio.current_task():
                self._provider_event_tasks.pop(call_id, None)
            if queue.empty() and self._provider_event_queues.get(call_id) is queue:
                self._provider_event_queues.pop(call_id, None)

    async def _drain_provider_events(self, call_id: str) -> None:
        queue = self._provider_event_queues.get(call_id)
        if queue is not None:
            await queue.join()

    def _sanitize_persisted_attempts(self) -> None:
        """Remove SDP written by the pre-qualified prototype."""
        with self.session_store._lock, self.session_store._connection() as connection:
            rows = connection.execute(
                "SELECT attempt_id, outcome_json FROM live_call_attempts WHERE outcome_json IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    value = json.loads(row["outcome_json"])
                except (TypeError, ValueError):
                    value = {}
                if not isinstance(value, dict):
                    value = {}
                changed = any(field in value for field in ("sdp", "sdp_answer"))
                value.pop("sdp", None)
                value.pop("sdp_answer", None)
                if changed:
                    connection.execute(
                        "UPDATE live_call_attempts SET outcome_json = ? WHERE attempt_id = ?",
                        (json.dumps(value, separators=(",", ":")), row["attempt_id"]),
                    )

    async def start(self) -> None:
        self._closing = False
        with self.session_store._lock, self.session_store._connection() as connection:
            user_hangups = connection.execute(
                """SELECT * FROM live_calls
                   WHERE termination_initiator = 'user' AND termination_reason = 'user_hangup'
                     AND phase NOT IN ('ended', 'failed', 'interrupted')"""
            ).fetchall()
            user_hangup_ids = {str(row["call_id"]) for row in user_hangups}
            active_rows = connection.execute(
                "SELECT * FROM live_calls WHERE foreground = 1 AND phase IN ('connecting', 'active', 'ending', 'recovering')"
            ).fetchall()
            active_rows = [row for row in active_rows if str(row["call_id"]) not in user_hangup_ids]
            for row in active_rows:
                binding = self._binding_from_row(row)
                self.audit.record(
                    binding,
                    "runtime.recovery_detected",
                    phase=str(row["phase"]),
                    reason="function_generation_replaced",
                )
                connection.execute(
                    "UPDATE live_calls SET phase = 'recovering', ended_at = NULL, provider_close_state = 'unconfirmed' WHERE call_id = ?",
                    (binding.call_id,),
                )
                self._append_call_state(
                    connection, binding, "recovering",
                    "Live call is recovering after Function replacement",
                    reason="function_generation_replaced",
                )
            proposal_rows = connection.execute(
                """SELECT d.delegation_id, d.offset_ms, c.*
                   FROM live_delegations AS d JOIN live_calls AS c ON c.call_id = d.call_id
                   WHERE d.proposal_state = 'pending'
                     AND c.foreground = 1
                     AND c.phase IN ('connecting', 'active', 'recovering')
                     AND NOT (c.termination_initiator = 'user' AND c.termination_reason = 'user_hangup')"""
            ).fetchall()
            decision_rows = connection.execute(
                """SELECT r.idempotency_key, r.request_digest, r.receipt_json, c.*
                   FROM live_control_receipts AS r JOIN live_calls AS c ON c.call_id = r.call_id
                   WHERE r.operation IN ('decision_pending', 'admission_pending')
                     AND c.foreground = 1
                     AND c.phase IN ('connecting', 'active', 'recovering')
                     AND NOT (c.termination_initiator = 'user' AND c.termination_reason = 'user_hangup')"""
            ).fetchall()
        self._requeue_pending_provider_events()
        user_hangup_ids = {str(row["call_id"]) for row in user_hangups}
        for row in user_hangups:
            binding = self._binding_from_row(row)
            self._track(asyncio.create_task(
                self._finish_call(binding, reason="user_hangup", initiator="user"),
                name=f"hashi-live-finish-user-hangup-{binding.call_id}",
            ), self._recovery_tasks, binding=binding)
        for row in active_rows:
            if str(row["call_id"]) in user_hangup_ids:
                continue
            binding = self._binding_from_row(row)
            self._track(asyncio.create_task(
                self._run_sideband(binding, self._get_api_key(binding), binding.provider_session_id),
                name=f"hashi-live-sideband-recover-{binding.call_id}",
            ), self._sideband_tasks, binding=binding)
            self._ensure_foreground_router(binding)
        for row in proposal_rows:
            binding = self._binding_from_row(row)
            self._track(asyncio.create_task(
                self.schedule_proposal(binding, row["delegation_id"], int(row["offset_ms"])),
                name=f"hashi-live-proposal-{row['delegation_id']}",
            ), self._proposal_tasks, binding=binding)
        for row in decision_rows:
            binding = self._binding_from_row(row)
            self._track(asyncio.create_task(
                self._resume_pending_admission(binding, row["idempotency_key"], row["request_digest"], row["receipt_json"]),
                name=f"hashi-live-admission-{row['idempotency_key']}",
            ), self._recovery_tasks, binding=binding)
        self._sweeper_task = asyncio.create_task(self._sweeper_loop(), name="hashi-live-voice-sweeper")
        self._recover_action_relays(settle_orphans=True)

    async def shutdown(self) -> None:
        self._closing = True
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            await asyncio.gather(self._sweeper_task, return_exceptions=True)
            self._sweeper_task = None
        for collection in (self._proposal_tasks, self._relay_tasks, self._foreground_tasks, self._recovery_tasks, self._opening_tasks):
            for task in tuple(collection):
                task.cancel()
            if collection:
                await asyncio.gather(*tuple(collection), return_exceptions=True)
                collection.clear()
        for ws in tuple(self._active_sockets.values()):
            if not getattr(ws, "closed", True):
                await self._close_sideband_socket(ws, operation="function_shutdown_close")
        for task in tuple(self._sideband_tasks):
            task.cancel()
        if self._sideband_tasks:
            await asyncio.gather(*tuple(self._sideband_tasks), return_exceptions=True)
            self._sideband_tasks.clear()
        queues = tuple(self._provider_event_queues.values())
        if queues:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(queue.join() for queue in queues)),
                    timeout=min(5.0, self._close_timeout_seconds),
                )
            except asyncio.TimeoutError:
                pass
        for queue in queues:
            while True:
                try:
                    binding, event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    staged = self._stage_provider_event_for_recovery(binding, event)
                    self.audit.record(
                        binding, "provider.event_persistence_interrupted",
                        provider_event_type=str(event.get("type") or "unknown"),
                        provider_event_id=str(event.get("event_id") or "")[:160],
                        failed_attempts=0,
                        reason="function_shutdown_staged" if staged else "function_shutdown",
                    )
                except Exception as exc:
                    self.audit.record(
                        binding, "provider.event_persistence_interrupted",
                        provider_event_type=str(event.get("type") or "unknown"),
                        provider_event_id=str(event.get("event_id") or "")[:160],
                        failed_attempts=0,
                        reason="function_shutdown_stage_failed",
                        **exception_evidence(exc),
                    )
                queue.task_done()
        for task in tuple(self._provider_event_tasks_set):
            task.cancel()
        if self._provider_event_tasks_set:
            await asyncio.gather(*tuple(self._provider_event_tasks_set), return_exceptions=True)
            self._provider_event_tasks_set.clear()
        self._provider_event_tasks.clear()
        self._provider_event_queues.clear()
        if self._terminal_record_tasks:
            await asyncio.gather(*tuple(self._terminal_record_tasks), return_exceptions=True)
            self._terminal_record_tasks.clear()
        self._active_sockets.clear()
        with self.session_store._lock, self.session_store._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM live_calls WHERE foreground = 1 AND phase IN ('connecting', 'active', 'recovering')"
            ).fetchall()
            for row in rows:
                binding = self._binding_from_row(row)
                connection.execute(
                    "UPDATE live_calls SET phase = 'recovering', ended_at = NULL, provider_close_state = 'unconfirmed' WHERE call_id = ?",
                    (binding.call_id,),
                )
                self._append_call_state(
                    connection, binding, "recovering",
                    "Live call is recovering after Function shutdown",
                    reason="function_shutdown",
                )

    async def _sweep_expiring_transports(self) -> None:
        now_dt = _utc_now_dt()
        now = now_dt.isoformat().replace("+00:00", "Z")
        rollover_before = (now_dt + timedelta(seconds=PROVIDER_SESSION_ROLLOVER_MARGIN_SECONDS)).isoformat().replace("+00:00", "Z")
        with self.session_store._lock, self.session_store._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM live_calls
                   WHERE foreground = 1 AND phase IN ('connecting', 'active')
                     AND (lease_expiry <= ? OR max_ends_at <= ?)""",
                (now, rollover_before),
            ).fetchall()
        for row in rows:
            binding = self._binding_from_row(row)
            lease_expired = str(row["lease_expiry"] or "") <= now
            reason = "client_lease_expired" if lease_expired else "provider_rollover_due"
            summary = ("Phone transport lease expired; logical call remains open" if lease_expired
                       else "Provider transport is approaching its limit; replacing it before expiry")
            self._mark_recovering(binding, reason=reason, summary=summary)

    async def _sweeper_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self._sweep_expiring_transports()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Live Voice sweep failed safely: %s", type(exc).__name__)

    @staticmethod
    def _binding_from_row(row: Mapping[str, Any]) -> CallBinding:
        return CallBinding(
            owner_id=str(row["owner_id"]), instance_id=str(row["instance_id"]),
            instance_generation=str(row["instance_generation"]), agent_id=str(row["agent_id"]),
            session_id=str(row["session_id"]), context_generation=int(row["context_generation"]),
            call_id=str(row["call_id"]), call_epoch=int(row["call_epoch"]),
            provider_session_id=str(row["provider_session_id"]),
        )

    @staticmethod
    def _expected_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
        required = ("instance_id", "instance_generation", "agent_id", "session_id", "context_generation")
        if any(key not in payload for key in required):
            raise LiveVoiceError("live_scope_incomplete", 400)
        try:
            generation = int(payload["context_generation"])
        except (TypeError, ValueError) as exc:
            raise LiveVoiceError("live_generation_invalid", 400) from exc
        positive_int(generation)
        return {
            "instance_id": identifier(payload["instance_id"]),
            "instance_generation": identifier(payload["instance_generation"]),
            "agent_id": identifier(payload["agent_id"]).lower(),
            "session_id": identifier(payload["session_id"]),
            "context_generation": generation,
        }

    def _call_binding(
        self, owner_id: str, payload: Mapping[str, Any], *, require_current_transport: bool = False,
    ) -> tuple[CallBinding, Mapping[str, Any]]:
        call_id = identifier(payload.get("call_id"))
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (call_id,)).fetchone()
        if row is None:
            raise LiveVoiceError("live_not_found", 404)
        binding = self._binding_from_row(row)
        binding.require_scope(self._expected_scope(payload), authenticated_owner=owner_id)
        if require_current_transport:
            epoch = payload.get("call_epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
                raise LiveVoiceError("live_transport_epoch_required", 409)
            if epoch != binding.call_epoch:
                raise LiveVoiceError("live_transport_epoch_changed", 409)
        return binding, row

    def _append_call_state(self, connection: Any, binding: CallBinding, phase: str, summary: str, **detail: Any) -> Mapping[str, Any]:
        event = self.session_store._append_event(
            connection, session_id=binding.session_id, run_id=None,
            kind="voice.live.call.state", summary=summary,
            detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(), "phase": phase, **detail},
        )
        self.audit.record(
            binding,
            "call.phase",
            phase=phase,
            sequence=event.get("sequence"),
            reason=detail.get("reason"),
            provider_close_state=detail.get("provider_close_state"),
        )
        return event

    def _ensure_foreground_router(self, binding: CallBinding) -> None:
        existing = self._foreground_call_tasks.get(binding.call_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_foreground_router(binding),
            name=f"hashi-live-foreground-{binding.call_id}",
        )
        self._foreground_call_tasks[binding.call_id] = task
        self._track(task, self._foreground_tasks, binding=binding)
        task.add_done_callback(
            lambda done, call_id=binding.call_id: self._foreground_call_tasks.pop(call_id, None)
            if self._foreground_call_tasks.get(call_id) is done else None
        )

    async def _run_foreground_router(self, binding: CallBinding) -> None:
        while not self._closing:
            with self.session_store._lock, self.session_store._connection() as connection:
                call = connection.execute(
                    "SELECT phase FROM live_calls WHERE call_id = ?", (binding.call_id,)
                ).fetchone()
            if call is None or str(call["phase"]) in TERMINAL_PHASES:
                return
            if binding.call_id in self._active_sockets:
                try:
                    pending_items = self.session_store.pending_live_foreground_items(
                        binding.call_id, limit=25
                    )
                except Exception as exc:
                    self.audit.record(binding, "background.inbox_read_failed", **exception_evidence(exc))
                    pending_items = []

                # SessionStore returns one globally ordered page, so an older
                # item beyond one inbox's page cannot be overtaken.
                for item in pending_items:
                    item_type = str(item.get("item_type") or "")
                    inbox_id = str(item.get("inbox_id") or "")
                    if not inbox_id:
                        self.audit.record(binding, "background.inbox_payload_invalid", item_type=item_type)
                        break

                    if item_type == "message":
                        message_text = str(item.get("text") or "").strip()
                        if not message_text:
                            self.audit.record(binding, "background.inbox_payload_invalid",
                                              inbox_id=inbox_id, item_type=item_type)
                            break
                        source = str(item.get("source") or "background")[:80]
                        role = str(item.get("role") or "assistant")
                        content = (
                            "Background message from another PAO conversation. Keep it as context; "
                            "do not override typed system instructions, interrupt the phone, or repeat it immediately. "
                            f"Role: {role}; source: {source}. Original message: {message_text}"
                        )
                    elif item_type == "event":
                        event_kind = str(item.get("kind") or "background.event")[:100]
                        summary = str(item.get("summary") or "").strip()
                        if not summary:
                            self.audit.record(binding, "background.inbox_payload_invalid",
                                              inbox_id=inbox_id, item_type=item_type)
                            break
                        content = (
                            "Background PAO status from another conversation. Keep it as context; "
                            "do not interrupt or speak it immediately. Report it when relevant. "
                            f"Status: {event_kind}. Summary: {summary}"
                        )
                    else:
                        self.audit.record(
                            binding, "background.inbox_payload_invalid",
                            inbox_id=inbox_id, item_type=item_type,
                        )
                        break

                    delivered = await self._send_provider_update(
                        binding, kind="thinking", content=content, delegation_id=None
                    )
                    if not delivered:
                        self.audit.record(
                            binding,
                            "background.inbox_delivery_deferred",
                            inbox_id=inbox_id,
                            item_type=item_type,
                            source_session_id=str(item.get("source_session_id") or ""),
                            source_message_id=str(item.get("source_message_id") or ""),
                            source_event_id=str(item.get("source_event_id") or ""),
                        )
                        break

                    try:
                        if item_type == "message":
                            marked = self.session_store.mark_live_foreground_message_delivered(
                                binding.call_id, inbox_id
                            )
                        elif item_type == "event":
                            marked = self.session_store.mark_live_foreground_event_delivered(
                                binding.call_id, inbox_id
                            )
                        else:
                            marked = False
                    except Exception as exc:
                        marked = False
                        self.audit.record(
                            binding,
                            "background.inbox_delivery_commit_failed",
                            inbox_id=inbox_id,
                            item_type=item_type,
                            **exception_evidence(exc),
                        )
                    if not marked:
                        self.audit.record(
                            binding, "background.inbox_delivery_unconfirmed",
                            inbox_id=inbox_id, item_type=item_type,
                        )
                        break

                    if item_type == "message":
                        self.audit.record(
                            binding, "background.inbox_delivered", inbox_id=inbox_id,
                            source_session_id=str(item.get("source_session_id") or ""),
                            source_message_id=str(item.get("source_message_id") or ""),
                        )
                    else:
                        self.audit.record(
                            binding, "background.event_inbox_delivered", inbox_id=inbox_id,
                            source_session_id=str(item.get("source_session_id") or ""),
                            source_event_id=str(item.get("source_event_id") or ""),
                            event_kind=event_kind,
                        )
            await asyncio.sleep(0.5)

    # Durable transcript and proposal state ---------------------------------
    def _requeue_pending_provider_events(self) -> None:
        for item in self.session_store.pending_live_provider_events():
            binding = CallBinding(
                owner_id=str(item["owner_id"]),
                instance_id=str(item["instance_id"]),
                instance_generation=str(item["instance_generation"]),
                agent_id=str(item["agent_id"]),
                session_id=str(item["session_id"]),
                context_generation=int(item["context_generation"]),
                call_id=str(item["call_id"]),
                call_epoch=int(item["call_epoch"]),
                provider_session_id=str(item["provider_session_id"]),
            )
            if str(item["event_type"]) == "transcript":
                speaker = str(item["speaker"])
                event = {
                    "type": (
                        "conversation.user.delta"
                        if speaker == "user" else "conversation.assistant.delta"
                    ),
                    "event_id": str(item["provider_event_id"]),
                    "delta": str(item["text"]),
                    "start_ms": int(item["start_ms"]),
                    "end_ms": int(item["end_ms"]),
                }
            else:
                event = {
                    "type": "action.proposed",
                    "event_id": str(item["provider_event_id"]),
                    "delegation": {"target": "client", "id": str(item["delegation_id"])},
                    "offset_ms": int(item["offset_ms"]),
                }
            self._enqueue_provider_event(binding, event)

    async def stage_fragment_once(self, binding: CallBinding, fragment: Fragment) -> None:
        self.session_store.stage_live_provider_fragment(
            owner_id=binding.owner_id,
            provider_event_id=fragment.provider_event_id,
            call_id=binding.call_id,
            call_epoch=binding.call_epoch,
            session_id=binding.session_id,
            speaker=fragment.speaker,
            text=fragment.text,
            start_ms=fragment.start_ms,
            end_ms=fragment.end_ms,
        )

    async def stage_delegation_once(
        self, binding: CallBinding, event_id: str, delegation_id: str, offset_ms: int
    ) -> None:
        self.session_store.stage_live_provider_delegation(
            owner_id=binding.owner_id,
            provider_event_id=event_id,
            call_id=binding.call_id,
            call_epoch=binding.call_epoch,
            session_id=binding.session_id,
            delegation_id=delegation_id,
            offset_ms=offset_ms,
        )

    @staticmethod
    def _validate_provider_event_for_recovery(event: Mapping[str, Any]) -> bool:
        """Validate content before storage retries; return false for irrelevant events."""
        if normalize_transcript(event) is not None:
            return True
        if event.get("type") != "action.proposed":
            return False
        delegation = event.get("delegation")
        if not isinstance(delegation, Mapping) or delegation.get("target") != "client":
            return False
        offset = event.get("offset_ms")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LiveVoiceError("live_delegation_invalid")
        identifier(event.get("event_id"))
        identifier(delegation.get("id"))
        return True

    def _stage_provider_event_for_recovery(
        self, binding: CallBinding, event: Mapping[str, Any]
    ) -> bool:
        fragment = normalize_transcript(event)
        if fragment is not None:
            self.session_store.stage_live_provider_fragment(
                owner_id=binding.owner_id,
                provider_event_id=fragment.provider_event_id,
                call_id=binding.call_id,
                call_epoch=binding.call_epoch,
                session_id=binding.session_id,
                speaker=fragment.speaker,
                text=fragment.text,
                start_ms=fragment.start_ms,
                end_ms=fragment.end_ms,
            )
            return True
        if event.get("type") != "action.proposed":
            return False
        delegation = event.get("delegation")
        offset = event.get("offset_ms")
        if (
            not isinstance(delegation, Mapping)
            or delegation.get("target") != "client"
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            return False
        event_id = identifier(event.get("event_id"))
        delegation_id = identifier(delegation.get("id"))
        self.session_store.stage_live_provider_delegation(
            owner_id=binding.owner_id,
            provider_event_id=event_id,
            call_id=binding.call_id,
            call_epoch=binding.call_epoch,
            session_id=binding.session_id,
            delegation_id=delegation_id,
            offset_ms=offset,
        )
        return True

    async def append_fragment_once(self, binding: CallBinding, fragment: Fragment) -> Mapping[str, Any]:
        identity = (binding.owner_id, binding.session_id, binding.call_id, binding.call_epoch, fragment.provider_event_id)
        with self.session_store._lock, self.session_store._connection() as connection:
            existing = connection.execute(
                """SELECT f.*, e.detail_json FROM live_fragments AS f
                JOIN run_events AS e ON e.event_id = f.event_id
                WHERE f.owner_id = ? AND f.session_id = ? AND f.call_id = ?
                  AND f.call_epoch = ? AND f.provider_event_id = ?""", identity,
            ).fetchone()
            if existing is not None:
                detail = json.loads(existing["detail_json"] or "{}")
                expected = {"speaker": fragment.speaker, "text": fragment.text,
                            "start_ms": fragment.start_ms, "end_ms": fragment.end_ms}
                if any(detail.get(key) != value for key, value in expected.items()):
                    raise LiveVoiceError("live_event_identity_conflict", 409)
                connection.execute(
                    """DELETE FROM live_provider_event_inbox
                       WHERE owner_id = ? AND session_id = ? AND call_id = ?
                         AND call_epoch = ? AND provider_event_id = ?
                         AND event_type = 'transcript'""",
                    identity,
                )
                return {}
            event = self.session_store._append_event(
                connection, session_id=binding.session_id, run_id=None,
                kind="voice.live.transcript.fragment", summary=f"[{fragment.speaker}] {fragment.text}",
                detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(),
                        "provider_event_id": fragment.provider_event_id, "speaker": fragment.speaker,
                        "text": fragment.text, "start_ms": fragment.start_ms, "end_ms": fragment.end_ms},
            )
            connection.execute(
                """INSERT INTO live_fragments(
                owner_id, provider_event_id, call_id, call_epoch, session_id, speaker,
                start_ms, end_ms, event_id, sequence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (binding.owner_id, fragment.provider_event_id, binding.call_id, binding.call_epoch,
                 binding.session_id, fragment.speaker, fragment.start_ms, fragment.end_ms,
                 event["event_id"], event["sequence"], event["created_at"]),
            )
            connection.execute(
                """DELETE FROM live_provider_event_inbox
                   WHERE owner_id = ? AND session_id = ? AND call_id = ?
                     AND call_epoch = ? AND provider_event_id = ?
                     AND event_type = 'transcript'""",
                identity,
            )
            connection.execute(
                "UPDATE live_calls SET latest_session_event_sequence = ? WHERE call_id = ?",
                (event["sequence"], binding.call_id),
            )
            return event

    async def register_delegation_once(self, binding: CallBinding, delegation_id: str, offset_ms: int) -> bool:
        now = _utc_now_dt()
        with self.session_store._lock, self.session_store._connection() as connection:
            existing = connection.execute(
                "SELECT 1 FROM live_delegations WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?",
                (binding.call_id, binding.call_epoch, delegation_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """DELETE FROM live_provider_event_inbox
                       WHERE owner_id = ? AND session_id = ? AND call_id = ?
                         AND call_epoch = ? AND delegation_id = ?
                         AND event_type = 'delegation'""",
                    (
                        binding.owner_id, binding.session_id, binding.call_id,
                        binding.call_epoch, delegation_id,
                    ),
                )
                return False
            prior = connection.execute(
                "SELECT COALESCE(MAX(cutoff_ms), 0) AS watermark FROM live_delegations WHERE call_id = ? AND call_epoch = ? AND decision IS NOT NULL AND decision NOT IN ('incomplete','superseded','judgment_failed')",
                (binding.call_id, binding.call_epoch),
            ).fetchone()
            after_ms = min(int(prior["watermark"]), offset_ms)
            connection.execute(
                """INSERT INTO live_delegations(
                call_id, call_epoch, delegation_id, offset_ms, after_ms, cutoff_ms,
                proposal_state, proposal_ready_after, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (binding.call_id, binding.call_epoch, delegation_id, offset_ms, after_ms, offset_ms,
                 (now + timedelta(seconds=self._proposal_grace_seconds)).isoformat().replace("+00:00", "Z"),
                 (now + timedelta(seconds=300)).isoformat().replace("+00:00", "Z"),
                 now.isoformat().replace("+00:00", "Z")),
            )
            connection.execute(
                """DELETE FROM live_provider_event_inbox
                   WHERE owner_id = ? AND session_id = ? AND call_id = ?
                     AND call_epoch = ? AND delegation_id = ?
                     AND event_type = 'delegation'""",
                (
                    binding.owner_id, binding.session_id, binding.call_id,
                    binding.call_epoch, delegation_id,
                ),
            )
            return True

    async def schedule_proposal(self, binding: CallBinding, delegation_id: str, offset_ms: int) -> None:
        del offset_ms
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM live_delegations WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?",
                (binding.call_id, binding.call_epoch, delegation_id),
            ).fetchone()
        if row is None or row["proposal_state"] != "pending":
            return
        ready_at = _parse_utc(row["proposal_ready_after"])
        delay = max(0.0, (ready_at - _utc_now_dt()).total_seconds()) if ready_at is not None else 0.0
        if delay:
            self._track(asyncio.create_task(
                self._materialize_proposal(binding, delegation_id, delay),
                name=f"hashi-live-proposal-{delegation_id}",
            ), self._proposal_tasks, binding=binding)
            return
        await self._materialize_proposal(binding, delegation_id, 0.0)

    async def _materialize_proposal(self, binding: CallBinding, delegation_id: str, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM live_delegations WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?",
                (binding.call_id, binding.call_epoch, delegation_id),
            ).fetchone()
            if row is None or row["proposal_state"] != "pending":
                return
            fragment_rows = connection.execute(
                """SELECT f.provider_event_id, f.start_ms, f.end_ms, e.detail_json
                FROM live_fragments AS f JOIN run_events AS e ON e.event_id = f.event_id
                WHERE f.owner_id = ? AND f.session_id = ? AND f.call_id = ?
                  AND f.call_epoch = ? AND f.speaker = 'user' AND f.start_ms < ?
                ORDER BY f.start_ms, f.end_ms, f.provider_event_id""",
                (binding.owner_id, binding.session_id, binding.call_id, binding.call_epoch, int(row["cutoff_ms"])),
            ).fetchall()
            fragments = []
            for item in fragment_rows:
                detail = json.loads(item["detail_json"] or "{}")
                fragments.append(Fragment(item["provider_event_id"], detail.get("speaker"), detail.get("text"),
                                          int(item["start_ms"]), int(item["end_ms"])))
            proposal = build_proposal(
                binding, delegation_id, fragments, after_ms=int(row["after_ms"]),
                cutoff_ms=int(row["cutoff_ms"]), expires_at=str(row["expires_at"]),
                version=int(row["proposal_version"]),
            )
            connection.execute(
                """UPDATE live_delegations SET source_event_ids_json = ?, proposal_digest = ?,
                proposal_text = ?, ambiguous = ?, proposal_state = 'ready'
                WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?""",
                (json.dumps(list(proposal.source_event_ids)), proposal.digest, proposal.text,
                 1 if proposal.ambiguous else 0, binding.call_id, binding.call_epoch, delegation_id),
            )
            self.session_store._append_event(
                connection, session_id=binding.session_id, run_id=None,
                kind="voice.live.delegation.proposed", summary=f"Live delegation proposed: {proposal.text[:60]}",
                detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(),
                        "delegation_id": proposal.delegation_id, "version": proposal.version,
                        "digest": proposal.digest, "text": proposal.text, "ambiguous": proposal.ambiguous,
                        "cutoff_ms": proposal.cutoff_ms, "expires_at": proposal.expires_at,
                        "source_event_ids": list(proposal.source_event_ids)},
            )
        await self._admit_ready_delegation(binding, proposal)

    def _action_event(self, binding: CallBinding, kind: str, detail: Mapping[str, Any],
                      *, run_id: str | None = None) -> None:
        with self.session_store._lock, self.session_store._connection() as connection:
            self.session_store._append_event(
                connection, session_id=binding.session_id, run_id=run_id,
                kind="voice.live.action." + kind, summary="Phone action " + kind,
                detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(), **detail},
            )

    def _action_text(self, binding: CallBinding, key: str) -> str:
        from orchestrator.ui_language import tr
        locale = "en"
        with suppress(Exception):
            public = self._phone_session(binding.agent_id, owner_id=binding.owner_id,
                session_id=binding.session_id, context_generation=binding.context_generation).get("public", {})
            locale = public.get("language") or "auto"
            if locale == "auto":
                locale = public.get("interface_language") or "en"
        return tr("phone.action." + key, locale=locale)

    def _unconfirmed_effect_text(self, binding: CallBinding, kind: str) -> str:
        key = ("write_unconfirmed" if kind in {"write", "modify"}
               else "execute_unconfirmed" if kind in {"execute", "cancel"}
               else "query_unconfirmed")
        return self._action_text(binding, key)

    def _progress_enabled(self, binding: CallBinding) -> bool:
        """Read the latest call-scoped spoken-progress choice from durable events."""
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                """SELECT detail_json FROM run_events WHERE session_id=?
                AND kind='voice.live.progress.preference'
                AND json_extract(detail_json, '$.scope.call_id')=?
                ORDER BY sequence DESC LIMIT 1""",
                (binding.session_id, binding.call_id),
            ).fetchone()
        return row is None or json.loads(row["detail_json"]).get("enabled") is True

    def _set_progress_preference(self, binding: CallBinding, preference: str) -> None:
        if preference == "unchanged":
            return
        enabled = preference == "on"
        if self._progress_enabled(binding) == enabled:
            return
        with self.session_store._lock, self.session_store._connection() as connection:
            self.session_store._append_event(
                connection, session_id=binding.session_id, run_id=None,
                kind="voice.live.progress.preference", summary="Phone progress preference changed",
                detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(),
                        "enabled": enabled},
            )

    def _semantic_context(self, binding: CallBinding, proposal: Proposal) -> dict[str, Any]:
        with self.session_store._lock, self.session_store._connection() as connection:
            rows = connection.execute(
                """SELECT f.speaker, e.detail_json FROM live_fragments f
                JOIN run_events e ON e.event_id = f.event_id
                WHERE f.call_id = ? ORDER BY f.call_epoch DESC, f.start_ms DESC LIMIT 60""",
                (binding.call_id,),
            ).fetchall()
            result_rows = connection.execute(
                """SELECT a.action_id, r.final_message_id, r.state AS run_state FROM live_actions a
                LEFT JOIN runs r ON r.run_id=a.run_id WHERE a.call_id=?""",
                (binding.call_id,),
            ).fetchall()
            route_rows = connection.execute(
                """SELECT e.run_id, e.detail_json FROM run_events e
                JOIN live_actions a ON a.run_id=e.run_id
                WHERE a.call_id=? AND e.kind='voice.live.action.model_route'
                ORDER BY e.sequence DESC LIMIT 24""",
                (binding.call_id,),
            ).fetchall()
            offered_result_rows = connection.execute(
                """SELECT detail_json FROM run_events
                   WHERE session_id=? AND kind='voice.live.action.reply_offered'
                     AND json_extract(detail_json, '$.scope.call_id')=?
                   ORDER BY sequence DESC LIMIT 256""",
                (binding.session_id, binding.call_id),
            ).fetchall()
        results_available = {row["action_id"]: bool(row["final_message_id"]) for row in result_rows}
        run_states = {row["action_id"]: row["run_state"] for row in result_rows}
        routes_by_run = {}
        for row in route_rows:
            detail = json.loads(row["detail_json"] or "{}")
            routes_by_run.setdefault(row["run_id"], {
                key: detail[key] for key in ("engine", "model_provider", "model", "route_status", "attempt")
                if key in detail})
        next_offsets = {}
        for row in offered_result_rows:
            detail = json.loads(row["detail_json"] or "{}")
            for page in detail.get("result_pages") or []:
                if isinstance(page, Mapping) and page.get("message_id") not in next_offsets:
                    next_offsets[str(page["message_id"])] = int(page.get("next_offset") or 0)
        conversation = []
        for row in reversed(rows):
            detail = json.loads(row["detail_json"] or "{}")
            text = str(detail.get("text") or "")
            if conversation and conversation[-1]["speaker"] == row["speaker"]:
                conversation[-1]["text"] += text
            else:
                conversation.append({"speaker": row["speaker"], "text": text})
        action_rows = self.actions.rows(binding)[-12:]
        known_results = self.session_store.recent_phone_result_references(
            owner_id=binding.owner_id,
            agent_id=binding.agent_id,
            session_id=binding.session_id,
        )
        state = {
            "utterance": proposal.text,
            "progress_updates": self._progress_enabled(binding),
            "conversation": [{**item, "text": item["text"][-800:],
                              "truncated": len(item["text"]) > 800} for item in conversation[-8:]],
            "actions": [{**PhoneActions.public(item),
                         "summary": item["request"][:700],
                         "summary_truncated": len(item["request"]) > 700,
                         "spoken_receipt": item["receipt"][:400],
                         "effect_status": item["status"],
                         "run_state": run_states.get(item["action_id"]),
                         "answer_available": results_available.get(item["action_id"], False),
                         "full_result_available": results_available.get(item["action_id"], False),
                         "model_route": routes_by_run.get(item["run_id"], {})}
                        for item in action_rows],
            "known_results": [
                {"result_id": row["message_id"], "session_id": row["session_id"],
                 "completed_at": row["created_at"], "source": row["session_kind"],
                 "request": str(row.get("request_text") or "")[:160],
                 "characters": len(row["text"]), "excerpt": row["text"][:360],
                 "excerpt_truncated": len(row["text"]) > 360,
                 "next_offset": next_offsets.get(row["message_id"], 0)}
                for row in known_results
            ],
        }
        # Derive this small judgment input from PCM's existing projection. Keep
        # reference context (including HCC) explicitly represented before recent
        # history; never silently substitute a guessed referent for omitted text.
        with suppress(Exception):
            phone = self._phone_session(binding.agent_id, owner_id=binding.owner_id,
                session_id=binding.session_id, context_generation=binding.context_generation)
            contexts, history = [], []
            for item in phone.get("input", []):
                text = item.get("text")
                if text is None:
                    text = "\n".join(str(part.get("text") or "") for part in item.get("content", []) if isinstance(part, Mapping))
                if text:
                    destination = contexts if item.get("role") in {"developer", "system"} else history
                    destination.append({"role": item.get("role"), "text": str(text)})
            selected = []
            for source, budget in ((contexts, 5200), (history[-4:], 2800)):
                share = max(1, budget // max(1, len(source)))
                for item in source:
                    text = item["text"]
                    excerpt = text if len(text) <= share else text[:share // 2] + "\n[omitted]\n" + text[-share // 2:]
                    selected.append({**item, "text": excerpt, "truncated": len(text) > share})
            state["known_context"] = selected
            state["omitted_history"] = len(history) > 4
            public = phone.get("public") or {}
            state["reply_language"] = public.get("language")
            state["fallback_language"] = public.get("interface_language") or "en"
        return state

    async def note_user_fragment(self, binding: CallBinding, fragment: Fragment) -> None:
        """A silence window groups fragments; only a semantic complete decision admits work."""
        if not callable(self._judge_action) or self._closing:
            return
        self._utterance_ends[binding.call_id] = fragment.end_ms
        task = asyncio.create_task(self._inspect_stable_utterance(binding, fragment.end_ms))
        self._utterance_tasks[binding.call_id] = task
        self._track(task, self._proposal_tasks, binding=binding)

    async def _inspect_stable_utterance(self, binding: CallBinding, end_ms: int) -> None:
        await asyncio.sleep(0.9)
        if self._utterance_ends.get(binding.call_id) != end_ms:
            return
        with self.session_store._lock, self.session_store._connection() as connection:
            handled = connection.execute(
                """SELECT COALESCE(MAX(cutoff_ms),0) FROM live_delegations
                WHERE call_id=? AND call_epoch=?
                AND decision IS NOT NULL AND decision NOT IN ('incomplete','superseded','judgment_failed')""",
                (binding.call_id, binding.call_epoch),
            ).fetchone()[0]
        if int(handled) >= end_ms:
            return
        delegation_id = f"utterance-{binding.call_epoch}-{end_ms}"
        if await self.register_delegation_once(binding, delegation_id, end_ms + 1):
            await self.schedule_proposal(binding, delegation_id, end_ms + 1)

    async def _admit_ready_delegation(self, binding: CallBinding, proposal: Proposal) -> None:
        # This lock serializes decisions about one evolving conversation; independent
        # admitted actions keep their own IDs and may run/queue normally.
        lock = self._action_locks.setdefault(binding.call_id, asyncio.Lock())
        async with lock:
            with self.session_store._lock, self.session_store._connection() as connection:
                row = connection.execute(
                    "SELECT decision FROM live_delegations WHERE call_id=? AND call_epoch=? AND delegation_id=?",
                    (binding.call_id, binding.call_epoch, proposal.delegation_id),
                ).fetchone()
                if row is None or row["decision"] is not None:
                    return
                # Provider proposals and the silence observer may cover the same
                # source. Reuse the earlier decision rather than executing twice.
                duplicate = connection.execute(
                    """SELECT delegation_id,decision FROM live_delegations WHERE call_id=? AND call_epoch=?
                    AND delegation_id!=? AND source_event_ids_json=? AND decision IS NOT NULL
                    AND decision NOT IN ('incomplete','superseded') LIMIT 1""",
                    (binding.call_id, binding.call_epoch, proposal.delegation_id,
                     json.dumps(list(proposal.source_event_ids))),
                ).fetchone()
            if duplicate:
                # A duplicate failed fragment is neither a new inference request
                # nor consumed speech. Only additional words can advance it.
                status = "judgment_failed" if duplicate["decision"] == "judgment_failed" else "reused"
                await self._finish_delegation_without_run(binding, proposal, decision=status,
                    reason="same_source_already_processed", provider_reply="")
                return
            if proposal.ambiguous or not proposal.text.strip():
                await self._finish_delegation_without_run(binding, proposal, decision="incomplete",
                    reason="incomplete_source", provider_reply="")
                return
            self._action_event(binding, "guard", {"active": True, "source_id": proposal.delegation_id})
            reply = ""
            raw = None
            interpreted = False
            try:
                if not callable(self._judge_action):
                    raise LiveVoiceError("live_semantic_service_unavailable", 503)
                state = self._semantic_context(binding, proposal)
                for attempt in range(2):
                    raw = await asyncio.wait_for(self._judge_action(binding, state), timeout=8.0)
                    # New speech wins over either a valid or malformed old judgment.
                    if self._proposal_has_newer_speech(binding, proposal):
                        self._action_event(binding, "judgment_discarded", {"source_id": proposal.delegation_id,
                            "reason": "new_speech", "shape": decision_shape(raw)})
                        await self._finish_delegation_without_run(binding, proposal, decision="superseded",
                            reason="new_speech_during_judgment", provider_reply="")
                        return
                    try:
                        decision = parse_decision(raw,
                            known_action_ids={item["action_id"] for item in state["actions"]},
                            known_result_ids={item["result_id"] for item in state["known_results"]},
                            known_result_next_offsets={item["result_id"]: item["next_offset"]
                                                       for item in state["known_results"]})
                        break
                    except LiveVoiceError as exc:
                        if (attempt or isinstance(raw, Mapping) and raw.get("complete") is False or
                                exc.code not in {"live_semantic_result_invalid", "live_semantic_target_invalid"}):
                            raise
                        self._action_event(binding, "judgment_repair", {"source_id": proposal.delegation_id,
                            "shape": decision_shape(raw), "error_code": exc.code})
                        state = {**state, "contract_repair": {
                            "invalid_shape": decision_shape(raw),
                            "requirement": "Return a valid complete judgment. route=act requires at least one fully resolved action."}}
                interpreted = True
                if not decision.complete:
                    await self._finish_delegation_without_run(binding, proposal, decision="incomplete",
                        reason="semantic_incomplete", provider_reply="")
                    return
                self._set_progress_preference(binding, decision.progress_preference)
                if decision.route is DelegationRoute.RECALL:
                    for result_id in decision.result_ids:
                        original = self.session_store.get_phone_result_text(
                            owner_id=binding.owner_id,
                            agent_id=binding.agent_id,
                            current_session_id=binding.session_id,
                            message_id=result_id,
                        )
                        if not original:
                            raise LiveVoiceError("live_saved_result_unavailable", 409)
                    reply = (
                        "The caller asked: " + proposal.text + "\n"
                        "Complete saved originals from this Agent were supplied as context. "
                        "Answer from them now. If all items were requested, cover every item "
                        "in spoken sections; do not invent a smaller total or repeat the work."
                    )
                    await self._finish_delegation_without_run(binding, proposal,
                        decision="recalled", reason="saved_agent_result", provider_reply=reply,
                        result_sources=[{
                            "message_id": result_id,
                            "start": next(item["next_offset"] for item in state["known_results"]
                                          if item["result_id"] == result_id)
                                     if decision.result_continuation else 0,
                        } for result_id in decision.result_ids])
                    return
                if decision.route is not DelegationRoute.EXECUTE:
                    reply = decision.reply if decision.reply_needed else ""
                    await self._finish_delegation_without_run(binding, proposal,
                        decision="handled_locally" if decision.route is DelegationRoute.DIRECT else "clarification_requested",
                        reason="contextual_semantic_judgment", provider_reply=reply)
                    return
                existing = {item["action_id"]: item for item in self.actions.rows(binding)}
                admitted = []
                references = []
                reused_reports = []
                for intent in decision.actions:
                    target = existing.get(intent.target_action_id or "")
                    if intent.relation == "reuse":
                        references.append(PhoneActions.public(target))
                        if target["kind"] == "query" and target.get("run_id"):
                            with suppress(Exception):
                                prior_run = self.session_store.get_run(str(target["run_id"]), owner_id=binding.owner_id)
                                report = self._canonical_run_report(binding, prior_run)
                                if report:
                                    reused_reports.append(report)
                        continue
                    if intent.relation in {"revise", "cancel"} and target:
                        active_unknown = False
                        if target["status"] == "unknown" and target.get("run_id"):
                            with suppress(Exception):
                                prior = self.session_store.get_run(str(target["run_id"]), owner_id=binding.owner_id)
                                active_unknown = prior.get("state") not in TERMINAL_RUN_STATES
                        if target["status"] in {"accepted", "running"} or active_unknown:
                            stopped = await self._cancel_existing_action(binding, target)
                            references.append(stopped)
                            if stopped["status"] not in {"cancelled", "verified"}:
                                # Stop/ack loss can leave a committed write. Reconcile,
                                # not replay, and keep the user's requested change.
                                if intent.relation == "revise":
                                    admitted.append(intent)
                                continue
                        if intent.relation == "cancel":
                            if target["status"] in {"verified", "unknown"}:
                                references.append(PhoneActions.public(target))
                            continue
                    admitted.append(intent)
                if not admitted:
                    reply = "\n".join(f"{fact['summary']}: {fact['spoken_receipt']}" for fact in references)
                    if reused_reports:
                        reply += "\n" + self._action_text(binding, "result_reported")
                        reply += "\n" + "\n".join(reused_reports)
                    await self._finish_delegation_without_run(binding, proposal, decision="reused",
                        reason="existing_action_state", provider_reply=reply)
                    return
                actions = self.actions.create(binding, proposal.delegation_id, admitted)
                for item in actions:
                    self.actions.transition(binding, item["action_id"], "accepted")
                await self._admit_action_rows(binding, proposal, actions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(getattr(exc, "code", "live_semantic_service_unavailable"))
                if not interpreted:
                    superseded = self._proposal_has_newer_speech(binding, proposal)
                    self._action_event(binding, "judgment_rejected", {"source_id": proposal.delegation_id,
                        "reason": reason, "shape": decision_shape(raw), "superseded": superseded})
                    # Nothing was admitted. Keep the entire spoken prefix for a
                    # later complete statement; do not retry a failed model call.
                    unfinished = isinstance(raw, Mapping) and raw.get("complete") is False
                    await self._finish_delegation_without_run(binding, proposal,
                        decision="superseded" if superseded else "judgment_failed", reason=reason,
                        provider_reply="" if superseded or unfinished else self._action_text(binding, "judgment_unavailable"))
                    return
                for item in self.actions.rows(binding, delegation_id=proposal.delegation_id):
                    self.actions.transition(binding, item["action_id"], "unknown",
                        receipt=self._unconfirmed_effect_text(binding, item["kind"]))
                reply = self._action_text(binding, "judgment_unavailable")
                await self._finish_delegation_without_run(binding, proposal, decision="needs_attention",
                    reason=reason, provider_reply=reply)
                self.audit.record(binding, "action.judgment_or_admission_failed",
                    delegation_id=proposal.delegation_id, error_code=reason)
            finally:
                self._action_event(binding, "guard", {"active": False, "source_id": proposal.delegation_id})

    def _proposal_has_newer_speech(self, binding: CallBinding, proposal: Proposal) -> bool:
        with self.session_store._lock, self.session_store._connection() as connection:
            latest = connection.execute(
                "SELECT MAX(end_ms) FROM live_fragments WHERE call_id=? AND call_epoch=? AND speaker='user'",
                (binding.call_id, binding.call_epoch),
            ).fetchone()[0]
        return latest is not None and int(latest) > proposal.cutoff_ms

    async def _cancel_existing_action(self, binding: CallBinding, action: Mapping[str, Any]) -> dict[str, Any]:
        if self._stop_was_requested(binding, str(action["action_id"])):
            current = next((row for row in self.actions.rows(binding)
                            if row["action_id"] == action["action_id"]), None)
            if current is not None:
                return PhoneActions.public(current)
        result = {}
        if action.get("run_id"):
            self._action_event(binding, "stop_intended", {
                "action_id": str(action["action_id"]), "run_id": str(action["run_id"])})
        try:
            if callable(self._cancel_action_run) and action.get("run_id"):
                result = await self._cancel_action_run(binding, str(action["run_id"]))
        except Exception as exc:
            self.audit.record(binding, "action.stop_request_unconfirmed",
                action_id=str(action["action_id"]),
                error_code=str(getattr(exc, "code", "stop_request_unavailable")))
        removed = result.get("cancelled_before_start") is True
        interrupt_sent = result.get("interrupted") is True
        status = "cancelled" if removed else "running" if interrupt_sent else "unknown"
        if removed or interrupt_sent:
            self._action_event(binding, "stop_requested", {
                "action_id": str(action["action_id"]), "run_id": action.get("run_id"),
                "request_accepted": True, "removed_before_start": removed,
                "interrupt_sent": interrupt_sent,
                "evidence_ref": result.get("evidence_ref"),
            })
        elif action.get("run_id"):
            self._action_event(binding, "stop_unconfirmed", {
                "action_id": str(action["action_id"]), "run_id": str(action["run_id"])})
        current = next((row for row in self.actions.rows(binding)
                        if row["action_id"] == action["action_id"]), None)
        if current and current["status"] in {"verified", "failed", "cancelled"}:
            return PhoneActions.public(current)
        if current and current["status"] == "unknown" and interrupt_sent:
            status = "unknown"
        return self.actions.transition(binding, str(action["action_id"]), status,
            evidence_refs=[str(result["evidence_ref"])] if result.get("evidence_ref") else [],
            receipt=self._action_text(binding, "cancelled" if removed else
                                      "stop_requested" if interrupt_sent else "stop_unconfirmed"))

    async def _admit_action_rows(self, binding: CallBinding, proposal: Proposal,
                                 actions: list[dict[str, Any]]) -> None:
        """Each independently cancellable action has exactly one ordinary Run."""
        children = []
        for item in actions:
            child_id = "action-request-" + stable_digest({"action_id": item["action_id"]})[:28]
            child_digest = stable_digest({"source": proposal.digest, "action_id": item["action_id"]})
            child = replace(proposal, delegation_id=child_id, digest=child_digest)
            with self.session_store._lock, self.session_store._connection() as connection:
                # Child authorization and its action association commit together;
                # a crash must never leave a free-standing pending proposal that
                # can reinterpret the entire multi-action spoken request.
                connection.execute(
                    """INSERT OR IGNORE INTO live_delegations(call_id, call_epoch,
                    delegation_id, offset_ms, cutoff_ms, proposal_state,
                    proposal_ready_after, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?)""",
                    (binding.call_id, binding.call_epoch, child_id, proposal.cutoff_ms,
                     proposal.cutoff_ms, _utc_now(), proposal.expires_at, _utc_now()),
                )
                connection.execute(
                    """UPDATE live_delegations SET source_event_ids_json=?, proposal_digest=?,
                    proposal_text=?, proposal_version=?, proposal_state='ready', ambiguous=0
                    WHERE call_id=? AND call_epoch=? AND delegation_id=? AND decision IS NULL""",
                    (json.dumps(list(proposal.source_event_ids)), child.digest, child.text, child.version,
                     binding.call_id, binding.call_epoch, child_id),
                )
                connection.execute("UPDATE live_actions SET delegation_id=? WHERE call_id=? AND action_id=?",
                                   (child_id, binding.call_id, item["action_id"]))
            children.append((child, item["action_id"]))
        await self._finish_delegation_without_run(binding, proposal, decision="actions_admitted",
                                                reason="independent_action_requests", provider_reply="")
        for child, action_id in children:
            digest = stable_digest({"delegation_id": child.delegation_id,
                                    "proposal_version": child.version,
                                    "proposal_digest": child.digest, "mode": "semantic_agent_turn"})
            try:
                await self.admit_delegation(binding, child, idempotency_key=f"live-auto-{digest[:32]}",
                                            request_digest=digest)
            except Exception as exc:
                self.actions.transition(binding, action_id, "unknown",
                    receipt=self._action_text(binding, "admission_unconfirmed"))
                cause = exc
                for _ in range(4):
                    if cause.__cause__ is None:
                        break
                    cause = cause.__cause__
                evidence = exception_evidence(cause)
                evidence.pop("exception_message", None)
                self._action_event(binding, "admission_unconfirmed", {"action_id": action_id,
                    "error_code": str(getattr(exc, "code", "live_outcome_unknown")), **evidence})
                self.audit.record(binding, "action.admission_unconfirmed", action_id=action_id,
                                  error_code=str(getattr(exc, "code", "live_outcome_unknown")), **evidence)
                await self._send_provider_update(binding, kind="commentary",
                    content=self._action_text(binding, "admission_unconfirmed"),
                    delegation_id=None)

    async def _offer_action_reply(self, binding: CallBinding, reply_id: str, content: str,
                                  *, run_id: str | None = None,
                                  result_sources: list[dict[str, Any]] | None = None) -> bool:
        """Persist a recoverable delivery plan, then stage originals before speech."""
        sources = list(result_sources or [])
        originals = []
        result_pages = []
        for source in sources:
            message_id = identifier(source.get("message_id"))
            original = self.session_store.get_phone_result_text(
                owner_id=binding.owner_id, agent_id=binding.agent_id,
                current_session_id=binding.session_id, message_id=message_id,
            )
            if not original:
                raise LiveVoiceError("live_saved_result_unavailable", 409)
            start = source.get("start", 0)
            if isinstance(start, bool) or not isinstance(start, int) or start < 0 or start >= len(original):
                raise LiveVoiceError("live_saved_result_position_invalid", 409)
            end = min(len(original), start + MAX_RESULT_PAGE_CHARS)
            if end < len(original):
                boundary = original.rfind("\n", start + MAX_RESULT_PAGE_CHARS // 2, end)
                if boundary > start:
                    end = boundary + 1
            next_offset = end if end < len(original) else 0
            outline = numbered_source_outline(original) if start == 0 else ""
            result_pages.append({"message_id": message_id, "start": start,
                                 "end": end, "total_chars": len(original),
                                 "next_offset": next_offset})
            originals.append(
                f"Saved original {message_id}; characters {start + 1}-{end} of {len(original)}; "
                + (f"continuation begins at offset {end}; this is not the full report"
                   if next_offset else "complete through the end")
                + ":\n" + (outline + "\n\n" if outline else "")
                + original[start:end]
            )
        original_context = "\n\n".join(originals)
        provider_content = content
        if any(page["next_offset"] for page in result_pages):
            provider_content += ("\nThe saved original continues beyond the material in this turn. "
                                 "Explain the page boundary and continuation to the caller; "
                                 "do not claim that the full report has been spoken.")
        token = (binding.call_id, reply_id)
        if token in self._reply_inflight:
            return False
        self._reply_inflight.add(token)
        monitoring = False
        try:
            with self.session_store._lock, self.session_store._connection() as connection:
                previous = connection.execute(
                    """SELECT kind, detail_json FROM run_events WHERE session_id=?
                    AND kind LIKE 'voice.live.action.reply_%'
                    AND json_extract(detail_json, '$.reply_id')=?
                    AND json_extract(detail_json, '$.scope.call_id')=?""",
                    (binding.session_id, reply_id, binding.call_id),
                ).fetchall()
                if any(row["kind"] == "voice.live.action.reply_observed" for row in previous):
                    return True
                if any(row["kind"] == "voice.live.action.reply_unconfirmed" for row in previous):
                    return False
                if not previous:
                    self.session_store._append_event(connection, session_id=binding.session_id, run_id=run_id,
                        kind="voice.live.action.reply_pending", summary="Phone response pending",
                        detail={"scope": binding.public_scope(), "reply_id": reply_id,
                                "content": content, "result_sources": sources})
                attempt = sum(row["kind"] == "voice.live.action.reply_attempted" for row in previous) + 1
                baseline = int(connection.execute("SELECT next_event_sequence-1 FROM sessions WHERE session_id=?",
                                                  (binding.session_id,)).fetchone()[0])
                if attempt <= 2:
                    self.session_store._append_event(connection, session_id=binding.session_id, run_id=run_id,
                        kind="voice.live.action.reply_attempted", summary="Phone response delivery attempted",
                        detail={"scope": binding.public_scope(), "reply_id": reply_id, "attempt": attempt,
                                "acceptance": "unverified", "audible_delivery": "unverified"})
            if attempt > 2:
                self._action_event(binding, "reply_unconfirmed", {"reply_id": reply_id,
                    "reason": "delivery_attempt_limit", "generated_output": "unconfirmed",
                    "audible_delivery": "unverified"})
                return False
            if original_context and not await self._send_provider_update(
                    binding, kind="thinking", content=original_context, delegation_id=None):
                return False
            relayed = await self._send_provider_update(binding, kind="commentary",
                                                       content=provider_content, delegation_id=None)
            if not relayed:
                return False
            with self.session_store._lock, self.session_store._connection() as connection:
                self.session_store._append_event(connection, session_id=binding.session_id, run_id=run_id,
                    kind="voice.live.action.reply_offered", summary="Phone response accepted for generation",
                    detail={"scope": binding.public_scope(), "reply_id": reply_id, "attempt": attempt,
                            "generated_output": "unverified", "audible_delivery": "unverified",
                            "result_source_count": len(sources),
                            "result_pages": result_pages,
                            "result_context_sha256": stable_digest({"text": original_context}) if sources else None})
            self._track(asyncio.create_task(
                self._observe_action_reply(binding, reply_id, content, baseline, attempt, run_id, sources),
                name="hashi-live-reply-" + reply_id), self._relay_tasks, binding=binding)
            monitoring = True
            return True
        finally:
            if not monitoring:
                self._reply_inflight.discard(token)

    async def _observe_action_reply(self, binding: CallBinding, reply_id: str, content: str,
                                    after_sequence: int, attempt: int, run_id: str | None,
                                    result_sources: list[dict[str, Any]]) -> None:
        token = (binding.call_id, reply_id)
        handed_off = False
        try:
            await asyncio.sleep(self._action_reply_timeout_seconds)
            with self.session_store._lock, self.session_store._connection() as connection:
                rows = connection.execute(
                    """SELECT f.speaker FROM live_fragments f JOIN run_events e ON e.event_id=f.event_id
                    WHERE f.call_id=? AND e.sequence>?""", (binding.call_id, after_sequence),
                ).fetchall()
            output_observed = any(row["speaker"] == "assistant" for row in rows)
            user_spoke = any(row["speaker"] == "user" for row in rows)
            if not output_observed and not user_spoke and attempt < 2 and not self._closing:
                self._reply_inflight.discard(token)
                handed_off = await self._offer_action_reply(binding, reply_id, content, run_id=run_id,
                                                            result_sources=result_sources)
                return
            self._action_event(binding, "reply_observed" if output_observed else "reply_unconfirmed", {
                "reply_id": reply_id, "attempts": attempt,
                "generated_output": "observed" if output_observed else "unconfirmed",
                # A transcript is not proof of complete, audible playback.
                "content_delivery": "unverified", "audible_delivery": "unverified",
                "reason": "user_priority" if user_spoke and not output_observed else "observation_window",
            })
        finally:
            if not handed_off:
                self._reply_inflight.discard(token)

    def _recover_pending_action_replies(self, call_id: str | None) -> None:
        with self.session_store._lock, self.session_store._connection() as connection:
            rows = connection.execute(
                """SELECT e.run_id, e.detail_json, c.* FROM run_events e
                JOIN live_calls c ON c.call_id=json_extract(e.detail_json, '$.scope.call_id')
                WHERE e.kind='voice.live.action.reply_pending' AND c.phase IN ('connecting','active','recovering')
                AND (? IS NULL OR c.call_id=?) AND NOT EXISTS (
                    SELECT 1 FROM run_events done WHERE done.session_id=e.session_id
                    AND done.kind IN ('voice.live.action.reply_observed','voice.live.action.reply_unconfirmed')
                    AND json_extract(done.detail_json, '$.scope.call_id')=c.call_id
                    AND json_extract(done.detail_json, '$.reply_id')=json_extract(e.detail_json, '$.reply_id'))""",
                (call_id, call_id),
            ).fetchall()
        for row in rows:
            detail = json.loads(row["detail_json"])
            binding = self._binding_from_row(row)
            if (binding.call_id, detail["reply_id"]) in self._reply_inflight:
                continue
            self._track(asyncio.create_task(self._offer_action_reply(
                binding, detail["reply_id"], detail["content"], run_id=row["run_id"],
                result_sources=detail.get("result_sources")),
                name="hashi-live-reply-recovery-" + detail["reply_id"]), self._relay_tasks, binding=binding)

    async def _finish_delegation_without_run(self, binding: CallBinding, proposal: Proposal, *,
                                             decision: str, reason: str, provider_reply: str,
                                             result_sources: list[dict[str, Any]] | None = None) -> None:
        applied = False
        with self.session_store._lock, self.session_store._connection() as connection:
            cursor = connection.execute(
                """UPDATE live_delegations SET decision=?, decided_at=? WHERE call_id=?
                AND call_epoch=? AND delegation_id=? AND decision IS NULL""",
                (decision, _utc_now(), binding.call_id, binding.call_epoch, proposal.delegation_id),
            )
            if cursor.rowcount:
                self.session_store._append_event(connection, session_id=binding.session_id, run_id=None,
                    kind="voice.live.delegation.routed", summary=f"Phone request: {decision}",
                    detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(),
                            "delegation_id": proposal.delegation_id, "decision": decision, "reason": reason})
                applied = True
        if applied and provider_reply:
            # Commentary delivers actual content; instruction insertion alone
            # neither completes a turn nor proves audible playback.
            await self._offer_action_reply(binding, "decision-" + proposal.delegation_id,
                                           provider_reply, result_sources=result_sources)

    async def read_proposal(self, binding: CallBinding, delegation_id: str) -> Proposal:
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM live_delegations WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?",
                (binding.call_id, binding.call_epoch, delegation_id),
            ).fetchone()
            if row is None:
                raise LiveVoiceError("live_proposal_not_found", 404)
            if row["proposal_state"] == "pending":
                raise LiveVoiceError("live_proposal_pending", 409)
            if row["proposal_state"] == "expired" or (row["expires_at"] and row["expires_at"] < _utc_now()):
                if row["proposal_state"] != "expired":
                    connection.execute(
                        "UPDATE live_delegations SET proposal_state = 'expired' WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?",
                        (binding.call_id, binding.call_epoch, delegation_id),
                    )
                    self.session_store._append_event(
                        connection, session_id=binding.session_id, run_id=None,
                        kind="voice.live.delegation.expired", summary="Live delegation expired",
                        detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(), "delegation_id": delegation_id},
                    )
                raise LiveVoiceError("live_proposal_expired", 409)
            return Proposal(
                delegation_id=row["delegation_id"], version=int(row["proposal_version"]),
                text=row["proposal_text"], source_event_ids=tuple(json.loads(row["source_event_ids_json"] or "[]")),
                digest=row["proposal_digest"], ambiguous=bool(row["ambiguous"]),
                cutoff_ms=int(row["cutoff_ms"]), expires_at=row["expires_at"],
            )

    # Automatic delegation admission ---------------------------------------
    async def find_admission(self, binding: CallBinding, *, idempotency_key: str, request_digest: str) -> Mapping[str, Any] | None:
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT request_digest, operation, receipt_json FROM live_control_receipts WHERE call_id = ? AND idempotency_key = ?",
                (binding.call_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise LiveVoiceError("live_idempotency_conflict", 409)
        return None if row["operation"] in {"decision_pending", "admission_pending"} else json.loads(row["receipt_json"])

    async def admit_delegation(
        self, binding: CallBinding, proposal: Proposal, *,
        idempotency_key: str, request_digest: str,
    ) -> Mapping[str, Any]:
        prior = await self.find_admission(binding, idempotency_key=idempotency_key, request_digest=request_digest)
        if prior is not None:
            return prior
        session = self.session_store.get_session(binding.session_id, owner_id=binding.owner_id, agent_id=binding.agent_id)
        if int(session["context_generation"]) != binding.context_generation:
            raise LiveVoiceError("live_scope_changed", 409)
        if self._admit_run is None:
            raise LiveVoiceError("live_admission_unavailable", 503)
        with self.session_store._lock, self.session_store._connection() as connection:
            call = connection.execute("SELECT phase FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
            if call is None:
                raise LiveVoiceError("live_not_found", 404)
            if call["phase"] in TERMINAL_PHASES:
                raise LiveVoiceError("live_call_terminal", 409)
            row = connection.execute(
                "SELECT decision, decision_key, decision_digest FROM live_delegations WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?",
                (binding.call_id, binding.call_epoch, proposal.delegation_id),
            ).fetchone()
            if row is None:
                raise LiveVoiceError("live_proposal_not_found", 404)
            if row["decision"] not in (None, "confirming", "admitting"):
                raise LiveVoiceError("live_delegation_already_decided", 409)
            if row["decision"] in {"confirming", "admitting"} and (
                row["decision_key"] != idempotency_key or row["decision_digest"] != request_digest
            ):
                raise LiveVoiceError("live_delegation_already_decided", 409)
            first_attempt = row["decision"] is None
            pending = {"ok": False, "pending": True, "delegation_id": proposal.delegation_id,
                       "proposal_version": proposal.version, "proposal_digest": proposal.digest}
            connection.execute(
                "UPDATE live_delegations SET decision = 'admitting', decision_key = ?, decision_digest = ? WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?",
                (idempotency_key, request_digest, binding.call_id, binding.call_epoch, proposal.delegation_id),
            )
            connection.execute(
                """INSERT OR REPLACE INTO live_control_receipts(
                call_id, idempotency_key, request_digest, operation, receipt_json, created_at
                ) VALUES (?, ?, ?, 'admission_pending', ?, ?)""",
                (binding.call_id, idempotency_key, request_digest, json.dumps(pending), _utc_now()),
            )
        return await self._complete_pending_admission(binding, proposal, idempotency_key, request_digest,
                                                     first_attempt=first_attempt)

    async def _complete_pending_admission(
        self, binding: CallBinding, proposal: Proposal, idempotency_key: str, request_digest: str,
        *, first_attempt: bool = False,
    ) -> Mapping[str, Any]:
        actions = self.actions.rows(binding, delegation_id=proposal.delegation_id)
        if actions:
            existing = {item["action_id"]: item for item in self.actions.rows(binding)}
            tasks = [{"action_id": item["action_id"], "kind": item["kind"], "request": item["request"],
                      "prior_action": existing.get(item.get("target_action_id") or "")}
                     for item in actions]
            proposal = replace(proposal, execution_text=(
                "Carry out ONLY resolved_actions below in this Run through normal authorized tools. "
                "The original spoken_request is quoted context and permission constraints, not additional "
                "work for this Run. Independent actions are handled separately. Use the existing record destination and "
                "normal permission rules. For a modification first locate and update the original record; "
                "if its prior result is uncertain, reconcile it before any write. Prefer the existing narrow "
                "file/read/write/query tools so the effect is observable. Verify saved contents with a read. "
                "Report the concrete result and record reference; an answer without an actual tool effect "
                "does not complete an action. Do not repeat an uncertain write.\n"
                + json.dumps({"spoken_request": proposal.text, "resolved_actions": tasks}, ensure_ascii=False)
            ))
        try:
            accepted = await self._admit_run(  # type: ignore[misc]
                binding, proposal, f"live-delegation-{idempotency_key}"
            )
        except LiveVoiceError as exc:
            if first_attempt and exc.code == "live_admission_scope_changed":
                # Only this locally generated pre-Worker error proves no initial
                # admission happened. Recovery after an earlier uncertain RPC
                # remains unknown; a later scope error cannot undo prior effects.
                result = {"ok": False, "accepted": False, "rejected": True, "error_code": exc.code}
                now = _utc_now()
                with self.session_store._lock, self.session_store._connection() as connection:
                    connection.execute(
                        "UPDATE live_delegations SET decision='rejected',decided_at=? WHERE call_id=? AND call_epoch=? AND delegation_id=?",
                        (now, binding.call_id, binding.call_epoch, proposal.delegation_id))
                    connection.execute(
                        "UPDATE live_control_receipts SET operation='admission',receipt_json=?,created_at=? WHERE call_id=? AND idempotency_key=? AND request_digest=?",
                        (json.dumps(result), now, binding.call_id, idempotency_key, request_digest))
                for item in actions:
                    self.actions.transition(binding, item["action_id"], "failed",
                        receipt=self._action_text(binding, "admission_rejected"))
                self._action_event(binding, "admission_rejected", {"delegation_id": proposal.delegation_id,
                    "error_code": exc.code, "worker_invoked": False})
                self.audit.record(binding, "action.admission_rejected", delegation_id=proposal.delegation_id,
                                  error_code=exc.code)
                await self._offer_action_reply(binding, "admission-" + proposal.delegation_id,
                                               self._action_text(binding, "admission_rejected"))
                return result
            raise
        except Exception as exc:
            raise LiveVoiceError("live_outcome_unknown", 502) from exc
        run_id = identifier(str(accepted.get("run_id") or ""))
        message_id = identifier(str(accepted.get("message_id") or ""))
        request_id = identifier(str(accepted.get("request_id") or ""))
        result = {"ok": True, "accepted": True, "applied": True, "run_id": run_id,
                  "message_id": message_id, "request_id": request_id, "admission": "automatic"}
        now = _utc_now()
        with self.session_store._lock, self.session_store._connection() as connection:
            current = connection.execute(
                "SELECT operation, request_digest, receipt_json FROM live_control_receipts WHERE call_id = ? AND idempotency_key = ?",
                (binding.call_id, idempotency_key),
            ).fetchone()
            if current is None or current["request_digest"] != request_digest:
                raise LiveVoiceError("live_idempotency_conflict", 409)
            if current["operation"] in {"decision", "admission"}:
                return json.loads(current["receipt_json"])
            connection.execute(
                """UPDATE live_delegations SET decision = 'admitted', accepted_message_id = ?,
                accepted_run_id = ?, decided_at = ? WHERE call_id = ? AND call_epoch = ? AND delegation_id = ?""",
                (message_id, run_id, now, binding.call_id, binding.call_epoch, proposal.delegation_id),
            )
            connection.execute(
                "UPDATE live_control_receipts SET operation = 'admission', receipt_json = ?, created_at = ? WHERE call_id = ? AND idempotency_key = ?",
                (json.dumps(result), now, binding.call_id, idempotency_key),
            )
            self.session_store._append_event(
                connection, session_id=binding.session_id, run_id=run_id,
                kind="voice.live.delegation.admitted", summary=f"Live delegation admitted: {run_id}",
                detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(),
                        "delegation_id": proposal.delegation_id, "admission": "automatic",
                        "run_id": run_id, "message_id": message_id, "request_id": request_id},
            )
        self._track(
            asyncio.create_task(
                self._relay_run(binding, proposal.delegation_id, run_id, request_id),
                name=f"hashi-live-relay-{proposal.delegation_id}",
            ),
            self._relay_tasks,
            binding=binding,
        )
        for item in actions:
            self.actions.transition(binding, item["action_id"], "accepted", run_id=run_id)
        self.audit.record(
            binding,
            "background.run_admitted",
            delegation_id=proposal.delegation_id,
            run_id=run_id,
            request_id=request_id,
            outcome="accepted",
        )
        return result

    async def _resume_pending_admission(self, binding: CallBinding, idempotency_key: str, request_digest: str, receipt_json: str) -> None:
        try:
            pending = json.loads(receipt_json or "{}")
            proposal = await self.read_proposal(binding, identifier(pending.get("delegation_id")))
            if proposal.version != int(pending.get("proposal_version")) or proposal.digest != pending.get("proposal_digest"):
                raise LiveVoiceError("live_proposal_changed", 409)
            await self._complete_pending_admission(binding, proposal, idempotency_key, request_digest)
        except Exception as exc:
            logger.warning("Pending Live Voice admission remains recoverable (%s)", type(exc).__name__)

    async def _send_provider_update(
        self,
        binding: CallBinding,
        *,
        kind: str,
        content: str,
        delegation_id: str | None,
    ) -> bool:
        """Append bounded context/results through the selected provider sideband."""

        ws = self._active_sockets.get(binding.call_id)
        if ws is None or getattr(ws, "closed", True):
            self.audit.record(
                binding,
                "background.update_unavailable",
                operation=kind,
                delegation_id=delegation_id,
                reason="sideband_disconnected",
            )
            return False
        chunks = _live_text_chunks(content)
        if not chunks:
            return False
        for index, chunk in enumerate(chunks, start=1):
            event_id = f"live-update-{uuid4().hex}"
            # PAO action/utterance IDs are not native provider task IDs. Only a
            # task observed on this exact connection may cross that namespace.
            native_ids = self._provider_native_delegations.get((binding.call_id, binding.call_epoch), set())
            provider_delegation_id = delegation_id if delegation_id in native_ids else None
            payload = self._provider_for(binding).update(kind, chunk, provider_delegation_id, event_id)
            waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            identity = (binding.call_id, event_id)
            self._update_waiters[identity] = waiter
            try:
                self.audit.record(
                    binding,
                    "background.update_sent",
                    operation=kind,
                    delegation_id=delegation_id,
                    client_event_id=event_id,
                    chunk_index=index,
                    chunk_count=len(chunks),
                    text_bytes=len(chunk.encode("utf-8")),
                )
                await ws.send_json(payload)
                if await asyncio.wait_for(
                    waiter, timeout=self._control_timeout_seconds
                ) is not True:
                    self.audit.record(
                        binding,
                        "background.update_rejected",
                        operation=kind,
                        delegation_id=delegation_id,
                        client_event_id=event_id,
                        acknowledged=False,
                    )
                    return False
                self.audit.record(
                    binding,
                    "background.update_acknowledged",
                    operation=kind,
                    delegation_id=delegation_id,
                    client_event_id=event_id,
                    acknowledged=True,
                )
            except Exception as exc:
                self.audit.record(
                    binding,
                    "background.update_failed",
                    operation=kind,
                    delegation_id=delegation_id,
                    client_event_id=event_id,
                    **exception_evidence(exc),
                )
                return False
            finally:
                self._update_waiters.pop(identity, None)
        return True

    @staticmethod
    def _activity_update(event: Mapping[str, Any], *, phone_progress_enabled: bool) -> tuple[str, str] | None:
        channel = str(event.get("presentation_channel") or "").strip().casefold()
        # The Worker already projects which commentary is intended for users.
        # Reasoning, tool chatter and status telemetry are never spoken as filler.
        if channel not in {"commentary", "control"}:
            return None
        phone_commentary = (phone_progress_enabled and channel == "commentary"
                            and event.get("delivery_class") == "user_commentary")
        if (event.get("presentation_enabled") is not True and event.get("required") is not True
                and not phone_commentary):
            return None
        summary = str(event.get("summary") or "").strip()
        detail = event.get("detail")
        detail_text = str(detail).strip() if isinstance(detail, str) else ""
        content = summary or detail_text
        if not content or len(content) > 320:
            return None
        return (channel, content)

    @staticmethod
    def _model_route_activity(event: Mapping[str, Any]) -> dict[str, Any] | None:
        if event.get("kind") != "model_route" or event.get("engine") != "her-v3":
            return None
        status = event.get("route_status")
        provider, model = event.get("model_provider"), event.get("model")
        attempt = event.get("attempt")
        if (status not in {"selected", "returned"} or not isinstance(provider, str)
                or not isinstance(model, str) or not provider or not model
                or len(provider) > 160 or len(model) > 240 or not isinstance(attempt, int)
                or isinstance(attempt, bool) or not 1 <= attempt <= 1_000_000
                or any(ch.isspace() or ord(ch) < 32 for ch in provider + model)):
            return None
        return {"engine": "her-v3", "model_provider": provider, "model": model,
                "route_status": status, "attempt": attempt}

    async def _record_model_route(self, binding: CallBinding, run_id: str,
                                  epoch: str, event: Mapping[str, Any]) -> bool:
        route = self._model_route_activity(event)
        if route is None:
            return False
        sequence = int(event.get("sequence") or 0)
        with self.session_store._lock, self.session_store._connection() as connection:
            previous = connection.execute(
                """SELECT 1 FROM run_events WHERE session_id=? AND run_id=?
                AND kind='voice.live.action.model_route'
                AND json_extract(detail_json, '$.source_epoch')=?
                AND json_extract(detail_json, '$.source_sequence')=? LIMIT 1""",
                (binding.session_id, run_id, epoch, sequence),
            ).fetchone()
        if previous is not None:
            return True
        self._action_event(binding, "model_route", {
            "run_id": run_id, "source_epoch": epoch,
            "source_sequence": sequence, **route}, run_id=run_id)
        route_fact = (
            f"Internal task route: provider {route['model_provider']}, "
            f"model {route['model']} was selected for attempt {route['attempt']}; "
            "a response is not yet established."
            if route["route_status"] == "selected" else
            f"Internal task route: the selected {route['model_provider']} "
            f"adapter returned a response under model {route['model']} "
            f"on attempt {route['attempt']}. This route label comes from "
            "the configured adapter, not independent vendor attestation "
            "or proof that the user's task succeeded."
        )
        await self._send_provider_update(binding, kind="thinking",
            content=route_fact, delegation_id=None)
        return True

    def _recover_action_relays(self, *, settle_orphans: bool = False,
                              call_id: str | None = None) -> None:
        self._recover_pending_action_replies(call_id)
        if settle_orphans:
            with self.session_store._lock, self.session_store._connection() as connection:
                orphans = connection.execute(
                    """SELECT a.action_id, a.delegation_id, c.* FROM live_actions a
                    JOIN live_calls c ON c.call_id=a.call_id
                    LEFT JOIN live_delegations d ON d.call_id=a.call_id AND d.delegation_id=a.delegation_id
                    WHERE a.status='accepted' AND a.run_id IS NULL AND NOT EXISTS (
                        SELECT 1 FROM live_control_receipts r WHERE r.call_id=a.call_id
                        AND r.idempotency_key=d.decision_key AND r.operation='admission_pending')"""
                ).fetchall()
                for row in orphans:
                    connection.execute("UPDATE live_delegations SET decision='needs_attention', decided_at=? "
                        "WHERE call_id=? AND delegation_id=? AND decision IS NULL",
                        (_utc_now(), row["call_id"], row["delegation_id"]))
            for row in orphans:
                binding = self._binding_from_row(row)
                self.actions.transition(binding, row["action_id"], "unknown",
                    receipt=self._action_text(binding, "admission_unconfirmed"))
                self.audit.record(binding, "action.interrupted_before_admission", action_id=row["action_id"],
                                  delegation_id=row["delegation_id"])
        with self.session_store._lock, self.session_store._connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT a.delegation_id, a.run_id, r.request_id, c.*
                FROM live_actions a JOIN runs r ON r.run_id=a.run_id
                JOIN live_calls c ON c.call_id=a.call_id
                WHERE (? IS NULL OR c.call_id=?) AND (a.status IN ('accepted','running') OR
                (c.phase IN ('active','connecting','recovering') AND NOT EXISTS (
                    SELECT 1 FROM run_events e WHERE e.run_id=a.run_id
                    AND e.kind='voice.live.delegation.result_relayed')))""", (call_id, call_id)
            ).fetchall()
        for row in rows:
            binding = self._binding_from_row(row)
            name = f"hashi-live-relay-{row['delegation_id']}"
            if any(task.get_name() == name and not task.done() for task in self._relay_tasks):
                continue
            self._track(asyncio.create_task(self._relay_run(binding, row["delegation_id"],
                row["run_id"], row["request_id"]), name=name), self._relay_tasks, binding=binding)

    def _stop_was_requested(self, binding: CallBinding, action_id: str) -> bool:
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                """SELECT kind FROM run_events WHERE session_id=?
                AND kind IN ('voice.live.action.stop_intended',
                             'voice.live.action.stop_requested', 'voice.live.action.stop_unconfirmed')
                AND json_extract(detail_json, '$.scope.call_id')=?
                AND json_extract(detail_json, '$.action_id')=?
                ORDER BY sequence DESC LIMIT 1""",
                (binding.session_id, binding.call_id, action_id),
            ).fetchone()
        return row is not None and row["kind"] != "voice.live.action.stop_unconfirmed"

    def _progress_checkpoint(self, binding: CallBinding, run_id: str) -> tuple[bool, str, int]:
        with self.session_store._lock, self.session_store._connection() as connection:
            started = connection.execute(
                """SELECT 1 FROM run_events WHERE session_id=?
                AND kind='voice.live.action.progress_started'
                AND json_extract(detail_json, '$.scope.call_id')=?
                AND json_extract(detail_json, '$.run_id')=? LIMIT 1""",
                (binding.session_id, binding.call_id, run_id),
            ).fetchone() is not None
            row = connection.execute(
                """SELECT detail_json FROM run_events WHERE session_id=?
                AND kind IN ('voice.live.action.progress_observed', 'voice.live.action.progress_relayed')
                AND json_extract(detail_json, '$.scope.call_id')=?
                AND json_extract(detail_json, '$.run_id')=?
                ORDER BY sequence DESC LIMIT 1""",
                (binding.session_id, binding.call_id, run_id),
            ).fetchone()
        relayed = json.loads(row["detail_json"]) if row else {}
        return started, str(relayed.get("source_epoch") or ""), int(relayed.get("source_sequence") or 0)

    def _canonical_run_report(self, binding: CallBinding, run: Mapping[str, Any]) -> str:
        """Read the full PAO answer; it is information, never an effect receipt."""
        message_id = run.get("final_message_id")
        if not message_id or run.get("session_id") != binding.session_id or run.get("agent_id") != binding.agent_id:
            return ""
        try:
            message = self.session_store.get_message(str(message_id),
                session_id=binding.session_id, owner_id=binding.owner_id)
        except Exception:
            return ""
        return str(message.get("text") or "") if message.get("role") == "assistant" else ""

    async def _relay_run(self, binding: CallBinding, delegation_id: str,
                         run_id: str, request_id: str) -> None:
        """Observe execution and concrete effects even while audio is disconnected."""
        last_state = ""
        progress_started, activity_epoch, activity_cursor = self._progress_checkpoint(binding, run_id)
        pending_progress: tuple[int, str] | None = None
        last_progress_at = monotonic() if progress_started or activity_cursor else 0.0
        next_activity_poll_at = 0.0
        while not self._closing:
            try:
                run = self.session_store.get_run(run_id, owner_id=binding.owner_id)
                with self.session_store._lock, self.session_store._connection() as connection:
                    current = connection.execute("SELECT * FROM live_calls WHERE call_id=? AND owner_id=?",
                        (binding.call_id, binding.owner_id)).fetchone()
                if current is None:
                    return
                binding = self._binding_from_row(current)
            except Exception:
                return
            state = str(run.get("state") or "")
            actions = self.actions.rows(binding, delegation_id=delegation_id)
            stopping = any(self._stop_was_requested(binding, item["action_id"]) for item in actions)
            if state == "running" and last_state != state:
                for item in actions:
                    if not stopping:
                        self.actions.transition(binding, item["action_id"], "running", run_id=run_id)
                if self._progress_enabled(binding) and not progress_started and not stopping:
                    if await self._send_provider_update(binding, kind="commentary",
                            content=self._action_text(binding, "progress_started"), delegation_id=None):
                        last_progress_at = monotonic()
                        progress_started = True
                        self._action_event(binding, "progress_started", {"run_id": run_id})
            if state == "awaiting_approval" and last_state != state:
                await self._send_provider_update(binding, kind="commentary",
                    content=self._action_text(binding, "approval"),
                    delegation_id=None)
            last_state = state
            if state not in TERMINAL_RUN_STATES:
                if (callable(self._poll_run_activity) and state == "running" and not stopping
                        and monotonic() >= next_activity_poll_at):
                    next_activity_poll_at = monotonic() + 2.0
                    try:
                        activity = await asyncio.wait_for(
                            self._poll_run_activity(binding, request_id, activity_cursor, 64), timeout=2.0)
                        if activity.get("ok") is True:
                            epoch = str(activity.get("ephemeral_epoch") or "")
                            before = activity_cursor
                            if activity_epoch and epoch != activity_epoch:
                                # A replaced Worker has a different ephemeral
                                # stream. Do not replay its old activity page.
                                activity_cursor = int(activity.get("latest_sequence") or 0)
                                pending_progress = None
                            else:
                                for event in activity.get("events") or []:
                                    if not isinstance(event, Mapping):
                                        continue
                                    sequence = int(event.get("sequence") or 0)
                                    activity_cursor = max(activity_cursor, sequence)
                                    if await self._record_model_route(binding, run_id, epoch, event):
                                        continue
                                    update = self._activity_update(event,
                                        phone_progress_enabled=self._progress_enabled(binding))
                                    if update is None:
                                        continue
                                    channel, content = update
                                    if channel == "control" and event.get("required") is True:
                                        await self._send_provider_update(binding, kind="commentary",
                                            content=content, delegation_id=None)
                                    elif channel == "commentary":
                                        pending_progress = (sequence, content)
                                if activity_cursor == before:
                                    epoch = activity_epoch
                            if epoch != activity_epoch or activity_cursor != before:
                                self._action_event(binding, "progress_observed", {
                                    "run_id": run_id, "source_epoch": epoch,
                                    "source_sequence": activity_cursor})
                            activity_epoch = epoch
                    except Exception as exc:
                        self.audit.record(binding, "action.progress_poll_failed", run_id=run_id,
                            error_code=str(getattr(exc, "code", "progress_unavailable")))
                if stopping or not self._progress_enabled(binding):
                    pending_progress = None
                elif pending_progress and monotonic() - last_progress_at >= 25.0:
                    source_sequence, content = pending_progress
                    if await self._send_provider_update(binding, kind="commentary",
                            content=content, delegation_id=None):
                        last_progress_at = monotonic()
                        pending_progress = None
                        self._action_event(binding, "progress_relayed", {
                            "run_id": run_id, "source_epoch": activity_epoch,
                            "source_sequence": source_sequence})
                await asyncio.sleep(0.5)
                continue
            terminal_activity = None
            if callable(self._poll_run_activity):
                terminal_activity = asyncio.create_task(
                    self._poll_run_activity(binding, request_id, activity_cursor, 64))
            results = {}
            tool_observations = []
            unresolved = [item for item in actions if item["status"] not in {"verified", "cancelled", "failed"}]
            if callable(self._inspect_action_results) and unresolved:
                try:
                    inspection = await asyncio.wait_for(
                        self._inspect_action_results(binding, request_id, unresolved), timeout=9.0)
                    results = {str(item.get("action_id")): item for item in inspection.get("actions", [])
                               if isinstance(item, Mapping)}
                    tool_observations = [row for row in inspection.get("tool_observations", [])
                                         if isinstance(row, Mapping)]
                except Exception as exc:
                    self.audit.record(binding, "action.effect_inspection_failed", run_id=run_id,
                                      error_code=str(getattr(exc, "code", "effect_inspection_unavailable")))
            if terminal_activity is not None:
                try:
                    # Request activity is bounded. Catch up across its pages
                    # without delaying the final answer beyond two seconds.
                    cursor = activity_cursor
                    async with asyncio.timeout(2.0):
                        for page in range(16):
                            activity = (await terminal_activity if page == 0 else
                                        await self._poll_run_activity(binding, request_id, cursor, 64))
                            if activity.get("ok") is not True:
                                break
                            epoch = str(activity.get("ephemeral_epoch") or "")
                            if activity_epoch and epoch != activity_epoch:
                                break
                            before_page = cursor
                            for event in activity.get("events") or []:
                                if isinstance(event, Mapping):
                                    cursor = max(cursor, int(event.get("sequence") or 0))
                                    await self._record_model_route(binding, run_id, epoch, event)
                            latest = int(activity.get("latest_sequence") or cursor)
                            if cursor <= before_page or cursor >= latest:
                                break
                except Exception as exc:
                    self.audit.record(binding, "action.terminal_activity_unavailable", run_id=run_id,
                                      error_code=str(getattr(exc, "code", "progress_unavailable")))
            report = self._canonical_run_report(binding, run)
            facts = []
            for item in actions:
                if item["status"] in {"verified", "cancelled", "failed"}:
                    facts.append(PhoneActions.public(item))
                    continue
                result = results.get(item["action_id"], {})
                refs = result.get("evidence_refs", [])
                verified = (result.get("status") == "verified" and isinstance(refs, list) and bool(refs)
                            and (state == "completed" or item["kind"] in {"write", "modify"}))
                stop_requested = self._stop_was_requested(binding, item["action_id"])
                stop_confirmed = stop_requested and state in {"stopped", "interrupted"}
                if stop_confirmed:
                    self._action_event(binding, "stop_confirmed", {
                        "action_id": item["action_id"], "run_id": run_id,
                        "run_state": state, "effect_verified": verified})
                if verified:
                    receipt = str(result.get("receipt") or "")
                    if stop_confirmed:
                        receipt = self._action_text(binding, "stop_confirmed") + " " + receipt
                    elif state == "stopped":
                        receipt = self._action_text(binding, "stopped_unattributed") + " " + receipt
                    elif state == "interrupted":
                        receipt = self._action_text(binding, "interrupted_unattributed") + " " + receipt
                    elif stop_requested and state == "completed":
                        receipt = self._action_text(binding, "stop_too_late") + " " + receipt
                    status = "verified"
                elif stop_confirmed:
                    status = "cancelled"
                    receipt = self._action_text(binding, "stop_confirmed")
                    if item["kind"] == "query" and not report:
                        read_count = sum(row.get("action_id") == item["action_id"] and row.get("kind") == "read"
                                         for row in tool_observations)
                        receipt += " " + (self._action_text(binding, "partial_reads").format(count=read_count)
                                          if read_count else self._action_text(binding, "no_final_result"))
                    if item["kind"] in {"write", "modify", "execute"}:
                        if item["kind"] == "execute" and not report:
                            receipt += " " + self._action_text(binding, "no_final_result")
                        receipt += " " + self._unconfirmed_effect_text(binding, item["kind"])
                elif state in {"stopped", "interrupted"}:
                    status = "unknown"
                    receipt = self._action_text(binding, "stopped_unattributed" if state == "stopped"
                                                else "interrupted_unattributed")
                    if item["kind"] == "query" and not report:
                        read_count = sum(row.get("action_id") == item["action_id"] and row.get("kind") == "read"
                                         for row in tool_observations)
                        receipt += " " + (self._action_text(binding, "partial_reads").format(count=read_count)
                                          if read_count else self._action_text(binding, "no_final_result"))
                    if item["kind"] in {"write", "modify", "execute"}:
                        receipt += " " + self._unconfirmed_effect_text(binding, item["kind"])
                elif state == "failed":
                    status = "failed" if item["kind"] == "query" else "unknown"
                    receipt = self._action_text(binding, "run_failed")
                    if item["kind"] == "query" and not report:
                        receipt += " " + self._action_text(binding, "no_final_result")
                    if item["kind"] in {"write", "modify", "execute"}:
                        receipt += " " + self._unconfirmed_effect_text(binding, item["kind"])
                else:
                    status = "unknown"
                    receipt = (self._action_text(binding, "query_result_unverified")
                               if report and item["kind"] == "query" else
                               self._unconfirmed_effect_text(binding, item["kind"]))
                    if stop_requested and state == "completed":
                        receipt = self._action_text(binding, "stop_too_late") + " " + receipt
                facts.append(self.actions.transition(binding, item["action_id"],
                    status, run_id=run_id,
                    evidence_refs=refs if verified else [], receipt=receipt))
            # The canonical final Message carries the complete answer. It is not
            # evidence that any write succeeded or that every source was verified.
            content = "\n".join(f"{fact['summary']}: {fact['spoken_receipt']}" for fact in facts)
            result_sources = []
            if report and (any(item["kind"] == "query" for item in actions)
                           or any(fact["status"] == "unknown" for fact in facts)):
                content += "\n" + self._action_text(binding, "result_reported")
                result_sources = [{"message_id": str(run["final_message_id"])}]
            if not content:
                content = self._action_text(binding, "judgment_unavailable")
            relayed = await self._offer_action_reply(binding, "result-" + delegation_id, content,
                                                     run_id=run_id, result_sources=result_sources)
            if relayed:
                with self.session_store._lock, self.session_store._connection() as connection:
                    self.session_store._append_event(connection, session_id=binding.session_id, run_id=run_id,
                        kind="voice.live.delegation.result_relayed", status=state, phase="terminal",
                        summary="Verified action facts returned to the voice connection",
                        detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(),
                                "delegation_id": delegation_id, "request_id": request_id,
                                "actions": facts, "run_state": state,
                                "run_final_message_id": run.get("final_message_id"),
                                "full_result_available": bool(report),
                                "audible_delivery": "unverified"})
            self.audit.record(binding, "background.result_relayed" if relayed else "background.result_relay_failed",
                              delegation_id=delegation_id, run_id=run_id, request_id=request_id, outcome=state)
            return

    # Public operations -----------------------------------------------------
    async def invoke(self, operation: str, authority: Any, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        owner_id = authority.get("owner_id") if isinstance(authority, Mapping) else str(authority or "")
        if not owner_id:
            raise LiveVoiceError("live_not_authenticated", 401)
        handler = getattr(self, f"_op_{operation}", None)
        if not callable(handler):
            raise LiveVoiceError("live_operation_unsupported", 404)
        return await handler(owner_id, payload)

    async def _op_context(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        session_id = identifier(payload.get("session_id"))
        agent_id = identifier(payload.get("agent_id")).lower()
        try:
            generation = int(payload.get("context_generation"))
        except (TypeError, ValueError) as exc:
            raise LiveVoiceError("live_generation_invalid", 400) from exc
        positive_int(generation)
        session = self.session_store.get_session(session_id, owner_id=owner_id, agent_id=agent_id)
        if int(session["context_generation"]) != generation:
            raise LiveVoiceError("live_scope_changed", 409)
        phone = None
        configuration_error = None
        try:
            resolved_phone = self._phone_session(
                agent_id,
                owner_id=owner_id,
                session_id=session_id,
                context_generation=generation,
            )
            phone = resolved_phone["public"]
        except LiveVoiceError as exc:
            configuration_error = exc.code
        foreground_call = None
        with self.session_store._lock, self.session_store._connection() as connection:
            active_call = connection.execute(
                """SELECT * FROM live_calls
                   WHERE owner_id = ? AND foreground = 1
                     AND phase IN ('recovering', 'ending')
                   ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (owner_id,),
            ).fetchone()
        if active_call is not None:
            active_binding = self._binding_from_row(active_call)
            foreground_call = {
                "call_id": active_binding.call_id,
                "phase": str(active_call["phase"]),
                "binding": active_binding.public_scope(),
                "phone": self._stored_phone_public(active_call),
                "media": self._provider_for(active_binding).media_descriptor(),
            }
        return {
            "ok": True,
            "binding": {"instance_id": self.instance_id, "instance_generation": self.instance_generation,
                        "agent_id": agent_id, "session_id": session_id, "context_generation": generation},
            "foreground_call": foreground_call,
            "capability": {"protocol_version": "1.0", "available": bool(self.available and phone and resolved_phone["adapter"].credential(self.secrets)),
                           "phone": phone, "configuration_error": configuration_error,
                           "media": resolved_phone["media"] if phone else None,
                           "heartbeat_interval_seconds": 10, "lease_expiry_seconds": 45,
                           "provider_session_max_duration_seconds": (resolved_phone["adapter"].capabilities.max_session_seconds if phone else None)},
        }

    async def _op_start(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.available:
            raise LiveVoiceError("live_not_enabled", 503)
        expected = self._expected_scope(payload)
        if expected["instance_id"] != self.instance_id or expected["instance_generation"] != self.instance_generation:
            raise LiveVoiceError("live_scope_changed", 409)
        session = self.session_store.get_session(expected["session_id"], owner_id=owner_id, agent_id=expected["agent_id"])
        if int(session["context_generation"]) != expected["context_generation"]:
            raise LiveVoiceError("live_scope_changed", 409)
        phone_session = self._phone_session(
            expected["agent_id"],
            owner_id=owner_id,
            session_id=expected["session_id"],
            context_generation=expected["context_generation"],
        )
        adapter = phone_session["adapter"]
        key = adapter.credential(self.secrets)
        expected_phone_revision = str(payload.get("phone_revision") or "")
        if expected_phone_revision != phone_session["public"]["revision"]:
            raise LiveVoiceError("live_phone_configuration_changed", 409)
        phone_record = {
            "opening": new_opening(),
            "public": phone_session["public"],
            "selection": phone_session["selection"],
            "instructions_sha256": stable_digest({"instructions": phone_session["instructions"]}),
            "input_sha256": stable_digest({"input": phone_session["input"]}),
            "input_messages": len(phone_session["input"]),
            "input_tokens_est": int(phone_session["context_audit"].get("tokens_est") or 0),
            "context_manifest": {
                "included_context_keys": list(phone_session["context_audit"].get("included_context_keys") or []),
                "result_reference_ids": list(phone_session["context_audit"].get("result_reference_ids") or []),
                "result_references_omitted": int(phone_session["context_audit"].get("result_references_omitted") or 0),
                "result_references_omitted_ids": list(phone_session["context_audit"].get("result_references_omitted_ids") or []),
                "result_reference_omission_reason": phone_session["context_audit"].get("result_reference_omission_reason"),
            },
        }
        phone_record_json = json.dumps(phone_record, ensure_ascii=False, separators=(",", ":"))
        sdp = payload.get("sdp")
        attempt_id = identifier(payload.get("idempotency_key"))
        self.audit.record_attempt(
            attempt_id,
            "dial.request_received",
            source="workbench",
        )
        request_digest = stable_digest(dict(payload))
        with self.session_store._lock, self.session_store._connection() as connection:
            attempt = connection.execute("SELECT * FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if attempt is not None:
                if attempt["owner_id"] != owner_id:
                    raise LiveVoiceError("live_not_found", 404)
                if attempt["request_digest"] != request_digest:
                    raise LiveVoiceError("live_idempotency_conflict", 409)
                if attempt["cancel_requested"]:
                    raise LiveVoiceError("live_attempt_cancelled", 409)
                if attempt["state"] == "completed" and attempt["outcome_json"]:
                    answer = self._attempt_sdp_answers.get(attempt_id)
                    if answer is None:
                        raise LiveVoiceError("live_outcome_unknown", 502)
                    return {**json.loads(attempt["outcome_json"]), "sdp_answer": answer}
                if attempt["state"] in {"failed", "cancelled"} and attempt["outcome_json"]:
                    prior = json.loads(attempt["outcome_json"])
                    code = str(prior.get("error_code") or "live_start_failed")
                    raise LiveVoiceError(code, 409 if attempt["state"] == "cancelled" else 502)
                if attempt["state"] in {"reserved", "provider_created"}:
                    raise LiveVoiceError("live_attempt_in_progress", 409)
                raise LiveVoiceError("live_outcome_unknown", 502)
            now = _utc_now()
            active = connection.execute(
                "SELECT * FROM live_calls WHERE owner_id = ? AND foreground = 1 AND phase IN ('connecting', 'active', 'ending', 'recovering')",
                (owner_id,),
            ).fetchone()
            if active is not None:
                code = "live_call_recovery_required" if active["phase"] == "recovering" else "live_call_already_active"
                raise LiveVoiceError(code, 409)
            connection.execute(
                """INSERT INTO live_call_attempts(
                attempt_id, owner_id, session_id, agent_id, instance_id, instance_generation,
                context_generation, request_digest, phone_config_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                (attempt_id, owner_id, expected["session_id"], expected["agent_id"], self.instance_id,
                 self.instance_generation, expected["context_generation"], request_digest,
                 phone_record_json, now, now),
            )
        self.audit.record_attempt(
            attempt_id,
            "dial.reserved",
            phase="connecting",
        )
        try:
            async with adapter.http_session() as http:
                context_audit = phone_session["context_audit"]
                required_message_count = context_audit.get(
                    "required_message_count", len(phone_session["input"])
                )
                history_unit_message_counts = context_audit.get(
                    "history_unit_message_counts", []
                )
                fit_kwargs = {
                    "key": key,
                    "model": phone_session["model"],
                    "input_messages": phone_session["input"],
                    "required_message_count": required_message_count,
                    "history_unit_message_counts": history_unit_message_counts,
                }
                optional_units = int(
                    context_audit.get("optional_reference_units", 0)
                )
                if optional_units:
                    fit_kwargs["optional_prefix_unit_count"] = optional_units
                provider_input, exact_audit = await adapter.fit_input(
                    http, **fit_kwargs
                )
                phone_record.update(
                    {
                        "input_sha256": stable_digest({"input": provider_input}),
                        "input_messages": len(provider_input),
                        **exact_audit,
                    }
                )
                phone_record_json = json.dumps(
                    phone_record, ensure_ascii=False, separators=(",", ":")
                )
                with self.session_store._lock, self.session_store._connection() as connection:
                    connection.execute(
                        "UPDATE live_call_attempts SET phone_config_json = ?, updated_at = ? WHERE attempt_id = ?",
                        (phone_record_json, _utc_now(), attempt_id),
                    )
                logger.info(
                    "GPT-Live startup input fitted messages=%s exact_tokens=%s omitted_history_units=%s",
                    len(provider_input),
                    exact_audit["input_tokens_exact"],
                    exact_audit["history_omitted_units"],
                )
                self.audit.record_attempt(
                    attempt_id,
                    "dial.context_fitted",
                    input_messages=len(provider_input),
                    input_tokens=exact_audit["input_tokens_exact"],
                    history_omitted_units=exact_audit["history_omitted_units"],
                )
                provider = await adapter.create(
                    http, key=key,
                        sdp=sdp,
                        instructions=phone_session["instructions"],
                        model=phone_session["model"],
                        voice=phone_session["voice"],
                        input_messages=provider_input,
                        input_token_count_exact=exact_audit["input_tokens_exact"],
                )
        except LiveVoiceError as exc:
            state = "unknown" if exc.code == "live_provider_create_unknown" else "failed"
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = ?, outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                    (state, json.dumps({"ok": False, "error_code": exc.code}), _utc_now(), attempt_id),
                )
            self.audit.record_attempt(
                attempt_id,
                "dial.provider_create_failed",
                phase=state,
                error_code=exc.code,
            )
            raise
        provider_id, answer = provider["provider_session_id"], provider["sdp_answer"]
        self._connection_providers[provider_id] = adapter
        self.audit.record_attempt(
            attempt_id,
            "dial.provider_created",
            phase="provider_created",
        )
        binding = CallBinding(owner_id, self.instance_id, self.instance_generation, expected["agent_id"],
                              expected["session_id"], expected["context_generation"],
                              f"call-{uuid4().hex[:16]}", 1, provider_id)
        now_dt = _utc_now_dt()
        cancelled = False
        try:
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'provider_created', provider_id = ?, updated_at = ? WHERE attempt_id = ?",
                    (provider_id, _utc_now(), attempt_id),
                )
                check = connection.execute("SELECT cancel_requested FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
                cancelled = bool(check and check["cancel_requested"])
                if not cancelled:
                    connection.execute(
                        """INSERT INTO live_calls(
                        call_id, owner_id, session_id, agent_id, instance_id, instance_generation,
                        context_generation, call_epoch, provider_session_id, phase, controller_lease,
                        lease_expiry, started_at, max_ends_at, provider_close_state, phone_config_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'connecting', ?, ?, ?, ?, 'pending', ?)""",
                        (binding.call_id, owner_id, binding.session_id, binding.agent_id, self.instance_id,
                         self.instance_generation, binding.context_generation, provider_id, attempt_id,
                         (now_dt + timedelta(seconds=45)).isoformat().replace("+00:00", "Z"),
                         now_dt.isoformat().replace("+00:00", "Z"),
                         (now_dt + timedelta(seconds=adapter.capabilities.max_session_seconds)).isoformat().replace("+00:00", "Z"),
                         phone_record_json),
                    )
                    self._append_call_state(connection, binding, "connecting", "Live call connecting")
        except Exception as exc:
            close_state, usage = await self._close_provider_session(key, provider_id, binding=binding)
            error_code = "live_call_already_active" if isinstance(exc, sqlite3.IntegrityError) else "live_start_failed"
            self.audit.record_attempt(
                attempt_id,
                "dial.call_persistence_failed",
                phase="failed",
                cleanup_state=close_state,
                error_code=error_code,
                **exception_evidence(exc),
            )
            with suppress(Exception), self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'failed', cleanup_state = ?, outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                    (close_state, json.dumps({"ok": False, "error_code": error_code, "usage": usage}),
                     _utc_now(), attempt_id),
                )
            if isinstance(exc, sqlite3.IntegrityError):
                raise LiveVoiceError(error_code, 409) from exc
            raise
        if not cancelled:
            self.audit.record(
                binding,
                "call.created",
                phase="connecting",
                attempt_id=attempt_id,
                input_messages=phone_record.get("input_messages"),
                input_tokens=phone_record.get("input_tokens_exact") or phone_record.get("input_tokens_est"),
                history_omitted_units=phone_record.get("history_omitted_units"),
            )
        if cancelled:
            await self._close_provider_session(key, provider_id, binding=binding)
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'cancelled', cleanup_state = 'closed', updated_at = ? WHERE attempt_id = ?",
                    (_utc_now(), attempt_id),
                )
            raise LiveVoiceError("live_attempt_cancelled", 409)
        ready = asyncio.Event()
        self._sideband_ready_events[binding.call_id] = ready
        self._session_closed_events[binding.call_id] = asyncio.Event()
        self._track(asyncio.create_task(
            self._run_sideband(binding, key, provider_id),
            name=f"hashi-live-sideband-{binding.call_id}",
        ), self._sideband_tasks, binding=binding)
        self._ensure_foreground_router(binding)
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            self._sideband_failures[binding.call_id] = "live_sideband_timeout"
        failure = self._sideband_failures.pop(binding.call_id, None)
        if failure:
            close_state, _usage = await self._close_provider_session(key, provider_id, binding=binding)
            self._mark_recovering(binding, reason=failure,
                                  summary="Initial sideband failed; logical phone remains open",
                                  provider_close_state=close_state)
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'failed', outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                    (json.dumps({"ok": False, "error_code": failure}), _utc_now(), attempt_id),
                )
            raise LiveVoiceError(failure, 502)
        outcome = {"ok": True, "accepted": True, "call_id": binding.call_id, "sideband_ready": True,
                   "provider_session_id": provider_id, "binding": binding.public_scope(),
                   "phone": phone_session["public"], "media": adapter.media_descriptor(),
                   "opening": self.opening.read(binding)}
        with self.session_store._lock, self.session_store._connection() as connection:
            check = connection.execute("SELECT cancel_requested FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if check is not None and check["cancel_requested"]:
            await self._finish_call(binding, reason="user_hangup", initiator="user")
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'cancelled', cleanup_state = 'closed', updated_at = ? WHERE attempt_id = ?",
                    (_utc_now(), attempt_id),
                )
            raise LiveVoiceError("live_attempt_cancelled", 409)
        self._attempt_sdp_answers[attempt_id] = answer
        with self.session_store._lock, self.session_store._connection() as connection:
            connection.execute(
                "UPDATE live_call_attempts SET state = 'completed', call_id = ?, outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                (binding.call_id, json.dumps(outcome, separators=(",", ":")), _utc_now(), attempt_id),
            )
        self.audit.record_attempt(
            attempt_id,
            "dial.completed",
            phase="active",
            outcome="accepted",
        )
        return {**outcome, "sdp_answer": answer}

    async def _op_resume(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Replace only the provider/browser transport while preserving one logical call."""
        binding, _current = self._call_binding(owner_id, payload)
        sdp = payload.get("sdp")
        if not isinstance(sdp, str) or not sdp.strip():
            raise LiveVoiceError("live_sdp_invalid", 400)
        attempt_id = identifier(payload.get("idempotency_key"))
        digest = stable_digest(dict(payload))
        with self.session_store._lock, self.session_store._connection() as connection:
            prior = connection.execute(
                "SELECT * FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if prior is not None:
                if prior["owner_id"] != owner_id or prior["request_digest"] != digest:
                    raise LiveVoiceError("live_idempotency_conflict", 409)
                if prior["state"] == "completed" and prior["outcome_json"]:
                    answer = self._attempt_sdp_answers.get(attempt_id)
                    if answer is None:
                        raise LiveVoiceError("live_outcome_unknown", 502)
                    return {**json.loads(prior["outcome_json"]), "sdp_answer": answer}
                if prior["state"] in {"reserved", "provider_created"}:
                    raise LiveVoiceError("live_attempt_in_progress", 409)
                if prior["state"] == "failed" and prior["outcome_json"]:
                    failure = json.loads(prior["outcome_json"])
                    raise LiveVoiceError(str(failure.get("error_code") or "live_provider_replacement_failed"), 502)
                raise LiveVoiceError("live_outcome_unknown", 502)
        binding, current = self._call_binding(owner_id, payload, require_current_transport=True)
        if str(current["phase"]) != "recovering":
            raise LiveVoiceError("live_call_not_recovering", 409)
        frozen_public = self._stored_phone_public(current)
        stored_selection = json.loads(current["phone_config_json"] or "{}").get("selection")
        if not stored_selection and frozen_public:
            stored_selection = {
                key: frozen_public[key]
                for key in ("provider", "model", "voice", "language", "style", "interface_language")
                if key in frozen_public
            }
        phone_session = self._phone_session(
            binding.agent_id, owner_id=owner_id, session_id=binding.session_id,
            context_generation=binding.context_generation, frozen_public=frozen_public,
            frozen_selection=stored_selection,
        )
        adapter = phone_session["adapter"]
        key = adapter.credential(self.secrets)
        expected_revision = str(payload.get("phone_revision") or "")
        if expected_revision != str(phone_session["public"].get("revision") or ""):
            raise LiveVoiceError("live_phone_configuration_changed", 409)
        with self.session_store._lock, self.session_store._connection() as connection:
            prior = connection.execute(
                "SELECT * FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if prior is not None:
                if prior["owner_id"] != owner_id or prior["request_digest"] != digest:
                    raise LiveVoiceError("live_idempotency_conflict", 409)
                if prior["state"] == "completed" and prior["outcome_json"]:
                    answer = self._attempt_sdp_answers.get(attempt_id)
                    if answer is None:
                        raise LiveVoiceError("live_outcome_unknown", 502)
                    return {**json.loads(prior["outcome_json"]), "sdp_answer": answer}
                if prior["state"] in {"reserved", "provider_created"}:
                    raise LiveVoiceError("live_attempt_in_progress", 409)
                raise LiveVoiceError("live_outcome_unknown", 502)
            row = connection.execute(
                "SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)
            ).fetchone()
            if row is None or row["phase"] != "recovering":
                raise LiveVoiceError("live_call_not_recovering", 409)
            previous_provider_id = str(row["provider_session_id"] or "")
            now = _utc_now()
            config = {
                **json.loads(row["phone_config_json"] or "{}"),
                "public": phone_session["public"],
                "selection": phone_session["selection"],
                "instructions_sha256": stable_digest({"instructions": phone_session["instructions"]}),
                "input_sha256": stable_digest({"input": phone_session["input"]}),
                "input_messages": len(phone_session["input"]),
                "input_tokens_est": int(phone_session["context_audit"].get("tokens_est") or 0),
            }
            attempt_inserted = False
            try:
                connection.execute(
                    """INSERT INTO live_call_attempts(
                       attempt_id, owner_id, session_id, agent_id, instance_id, instance_generation,
                       context_generation, request_digest, phone_config_json, state, created_at, updated_at,
                       call_id
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)""",
                    (attempt_id, owner_id, binding.session_id, binding.agent_id,
                     self.instance_id, self.instance_generation, binding.context_generation,
                     digest, json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                     now, now, binding.call_id),
                )
                attempt_inserted = True
                updated = connection.execute(
                    """UPDATE live_calls
                       SET phase = 'connecting', foreground = 1, call_epoch = call_epoch + 1,
                           controller_lease = ?, lease_expiry = ?, provider_close_state = 'pending'
                       WHERE call_id = ? AND phase = 'recovering'
                         AND call_epoch = ? AND provider_session_id = ?""",
                    (attempt_id, (_utc_now_dt() + timedelta(seconds=45)).isoformat().replace("+00:00", "Z"),
                     binding.call_id, binding.call_epoch, binding.provider_session_id),
                )
                if updated.rowcount != 1:
                    raise LiveVoiceError("live_call_resume_in_progress", 409)
                reserved_row = connection.execute(
                    "SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)
                ).fetchone()
                if reserved_row is None:
                    raise LiveVoiceError("live_call_resume_in_progress", 409)
                binding = self._binding_from_row(reserved_row)
                self._append_call_state(
                    connection, binding, "connecting", "Replacing phone transport",
                    reason="transport_resume_requested",
                )
            except sqlite3.IntegrityError as exc:
                raise LiveVoiceError("live_call_resume_in_progress", 409) from exc
        if not attempt_inserted:
            raise LiveVoiceError("live_call_resume_in_progress", 409)
        self.audit.record(binding, "transport.resume_reserved", attempt_id=attempt_id)
        previous_router = self._foreground_call_tasks.pop(binding.call_id, None)
        if previous_router is not None and not previous_router.done():
            previous_router.cancel()
            await asyncio.gather(previous_router, return_exceptions=True)
        old_socket = self._active_sockets.get(binding.call_id)
        if old_socket is not None and not getattr(old_socket, "closed", True):
            try:
                await self._close_sideband_socket(
                    old_socket, binding=binding, operation="transport_resume_close"
                )
            except Exception as exc:
                self.audit.record(binding, "sideband.old_socket_close_failed", **exception_evidence(exc))
        if previous_provider_id:
            await self._close_provider_session(
                key, previous_provider_id, binding=binding
            )
        old_sideband_tasks = [
            task for task in tuple(self._sideband_tasks)
            if binding.call_id in task.get_name()
        ]
        if old_sideband_tasks:
            _done, pending_tasks = await asyncio.wait(old_sideband_tasks, timeout=2)
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._sideband_failures.pop(binding.call_id, None)
        # Include every event already read from the old Provider in recovered history.
        try:
            await self._drain_provider_events(binding.call_id)
        except asyncio.CancelledError:
            self._mark_recovering(
                binding, reason="provider_event_drain_interrupted",
                summary="Transport replacement was cancelled while saving received speech",
            )
            raise
        history = []
        for segment in self.session_store.live_transcript_segments(
            binding.session_id,
            owner_id=binding.owner_id,
            context_generation=binding.context_generation,
            call_id=binding.call_id,
            call_epoch=None,
        ):
            text = str(segment.get("text") or "").strip()
            role = str(segment.get("role") or "")
            if text and role in {"user", "assistant"}:
                history.append({"role": role, "text": text})
        for item in self.session_store.live_foreground_history(binding.call_id):
            text = str(item.get("content") or "").strip()
            role = str(item.get("role") or "assistant")
            if not text:
                continue
            if item.get("item_type") == "event":
                text = f"Background PAO status from another conversation: {text}"
            else:
                source = str(item.get("source") or "background")[:80]
                text = f"Background message from another PAO conversation, source {source}: {text}"
            history.append({"role": role if role in {"user", "assistant"} else "assistant",
                            "text": text})
        input_messages = list(phone_session["input"])
        context_audit = phone_session["context_audit"]
        required_count = int(context_audit.get("required_message_count", len(input_messages)))
        unit_counts = list(context_audit.get("history_unit_message_counts", []))
        optional_units = int(context_audit.get("optional_reference_units", 0))
        input_messages.extend(history)
        unit_counts.extend([1] * len(history))
        try:
            async with adapter.http_session() as http:
                fit_kwargs = {
                    "key": key,
                    "model": phone_session["model"],
                    "input_messages": input_messages,
                    "required_message_count": required_count,
                    "history_unit_message_counts": unit_counts,
                }
                if optional_units:
                    fit_kwargs["optional_prefix_unit_count"] = optional_units
                provider_input, exact_audit = await adapter.fit_input(
                    http, **fit_kwargs
                )
                config.update({"input_sha256": stable_digest({"input": provider_input}),
                               "input_messages": len(provider_input), **exact_audit})
                provider = await adapter.create(
                    http, key=key,
                        sdp=sdp, instructions=phone_session["instructions"],
                        model=phone_session["model"], voice=phone_session["voice"],
                        input_messages=provider_input,
                        input_token_count_exact=exact_audit["input_tokens_exact"],
                )
        except LiveVoiceError as exc:
            self._mark_recovering(binding, reason=exc.code,
                                  summary="Provider replacement failed; logical phone remains open")
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'failed', outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                    (json.dumps({"ok": False, "error_code": exc.code}), _utc_now(), attempt_id),
                )
            self.audit.record(binding, "transport.resume_failed", error_code=exc.code)
            raise
        except Exception as exc:
            self._mark_recovering(binding, reason="provider_replacement_failed",
                                  summary="Provider replacement failed; logical phone remains open")
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'failed', outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                    (json.dumps({"ok": False, "error_code": "live_provider_replacement_failed"}), _utc_now(), attempt_id),
                )
            self.audit.record(binding, "transport.resume_failed",
                              error_code="live_provider_replacement_failed", **exception_evidence(exc))
            raise LiveVoiceError("live_provider_replacement_failed", 502) from exc
        provider_id, answer = provider["provider_session_id"], provider["sdp_answer"]
        self._connection_providers[provider_id] = adapter
        new_epoch = binding.call_epoch
        current_row = None
        with self.session_store._lock, self.session_store._connection() as connection:
            latest = connection.execute("SELECT phone_config_json FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
            if latest is not None:
                latest_config = json.loads(latest["phone_config_json"] or "{}")
                if "opening" in latest_config:
                    config["opening"] = latest_config["opening"]
            updated = connection.execute(
                """UPDATE live_calls SET provider_session_id = ?, phase = 'connecting',
                   provider_close_state = 'pending', phone_config_json = ?, max_ends_at = ?
                   WHERE call_id = ? AND phase = 'connecting' AND controller_lease = ?
                     AND call_epoch = ? AND provider_session_id = ?""",
                (provider_id, json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                 (_utc_now_dt() + timedelta(seconds=adapter.capabilities.max_session_seconds)).isoformat().replace("+00:00", "Z"),
                 binding.call_id, attempt_id, new_epoch, previous_provider_id),
            )
            if updated.rowcount == 1:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'provider_created', provider_id = ?, updated_at = ? WHERE attempt_id = ?",
                    (provider_id, _utc_now(), attempt_id),
                )
                current_row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
                self._append_call_state(connection, self._binding_from_row(current_row), "connecting",
                                        "Replacement Provider transport created", reason="provider_replaced")
        if current_row is None:
            await self._close_provider_session(key, provider_id, binding=binding)
            raise LiveVoiceError("live_call_resume_superseded", 409)
        new_binding = self._binding_from_row(current_row)
        self._session_closed_events[binding.call_id] = asyncio.Event()
        self._sideband_ready_events[binding.call_id] = asyncio.Event()
        self._sideband_failures.pop(binding.call_id, None)
        self._track(asyncio.create_task(
            self._run_sideband(new_binding, key, provider_id),
            name=f"hashi-live-sideband-resume-{binding.call_id}-{new_epoch}",
        ), self._sideband_tasks, binding=new_binding)
        self._ensure_foreground_router(new_binding)
        ready = self._sideband_ready_events[binding.call_id]
        failure = None
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            failure = "live_sideband_timeout"
            self._mark_recovering(new_binding, reason=failure,
                                  summary="Replacement sideband did not connect")
        if failure is None:
            failure = self._sideband_failures.pop(binding.call_id, None)
        if failure:
            await self._close_provider_session(key, provider_id, binding=new_binding)
            self._mark_recovering(new_binding, reason=failure,
                                  summary="Replacement sideband failed; logical phone remains open")
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'failed', outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                    (json.dumps({"ok": False, "error_code": failure}), _utc_now(), attempt_id),
                )
            raise LiveVoiceError(failure, 502)
        outcome = {"ok": True, "accepted": True, "call_id": binding.call_id,
                   "provider_session_id": provider_id, "sideband_ready": True,
                   "binding": new_binding.public_scope(), "phone": phone_session["public"],
                   "media": adapter.media_descriptor()}
        with self.session_store._lock, self.session_store._connection() as connection:
            connection.execute(
                "UPDATE live_call_attempts SET state = 'completed', outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                (json.dumps(outcome, separators=(",", ":")), _utc_now(), attempt_id),
            )
        self._attempt_sdp_answers[attempt_id] = answer
        self.audit.record(binding, "transport.resume_completed", call_epoch=new_epoch,
                          input_messages=len(provider_input), input_tokens=exact_audit["input_tokens_exact"],
                          omitted_history_units=exact_audit["history_omitted_units"])
        return {**outcome, "sdp_answer": answer}

    async def _op_cancel_start(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        attempt_id = identifier(payload.get("attempt_id") or payload.get("idempotency_key"))
        initiator = str(payload.get("termination_initiator") or "unknown")
        reason = str(payload.get("termination_reason") or "legacy_unspecified")
        if initiator not in TERMINATION_INITIATORS or reason not in TERMINATION_REASONS:
            raise LiveVoiceError("live_termination_invalid", 400)
        if (initiator == "user") != (reason == "user_hangup"):
            raise LiveVoiceError("live_termination_invalid", 400)
        expected = self._expected_scope(payload)
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None or row["owner_id"] != owner_id:
                raise LiveVoiceError("live_attempt_not_found", 404)
            for key in ("instance_id", "instance_generation", "agent_id", "session_id", "context_generation"):
                if str(row[key]) != str(expected[key]):
                    raise LiveVoiceError("live_scope_changed", 409)
            call_id, provider_id = row["call_id"], row["provider_id"]
            if initiator == "user" or not call_id:
                connection.execute(
                    "UPDATE live_call_attempts SET cancel_requested = 1, updated_at = ? WHERE attempt_id = ?",
                    (_utc_now(), attempt_id),
                )
        close_state = "not_created"
        if call_id:
            with self.session_store._lock, self.session_store._connection() as connection:
                call = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (call_id,)).fetchone()
            if call is not None:
                binding = self._binding_from_row(call)
                if initiator == "user":
                    receipt = await self._finish_call(binding, reason="user_hangup", initiator="user")
                    close_state = str(receipt.get("provider_close_state") or "unconfirmed")
                else:
                    self._mark_recovering(
                        binding, reason=reason,
                        summary="Non-user startup cancellation retained as a recoverable call",
                    )
                    self.audit.record(binding, "termination.rejected_as_end", initiator=initiator, reason=reason)
                    return {"ok": True, "applied": False, "cancelled": False,
                            "phase": "recovering", "call_id": call_id,
                            "provider_close_state": str(call["provider_close_state"] or "unconfirmed")}
        elif provider_id:
            # There is no durable logical call yet. Close this incomplete dial attempt
            # so a passive browser fault cannot leave an unowned Provider session.
            adapter = select_provider(self.providers, str((self._stored_phone_public(row) or {}).get("provider") or "openai"))
            self._connection_providers[str(provider_id)] = adapter
            close_state, _usage = await self._close_provider_session(adapter.credential(self.secrets), provider_id)
        return {"ok": True, "applied": initiator == "user", "cancelled": initiator == "user",
                "provider_close_state": close_state}

    async def _op_attempt(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        attempt_id = identifier(payload.get("attempt_id") or payload.get("idempotency_key"))
        expected = self._expected_scope(payload)
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if row is None or row["owner_id"] != owner_id:
            raise LiveVoiceError("live_attempt_not_found", 404)
        for key in ("instance_id", "instance_generation", "agent_id", "session_id", "context_generation"):
            if str(row[key]) != str(expected[key]):
                raise LiveVoiceError("live_scope_changed", 409)
        return {"ok": True, "attempt": {"state": row["state"], "call_id": row["call_id"],
                "cancel_requested": bool(row["cancel_requested"]),
                "outcome_known": row["state"] not in {"reserved", "provider_created", "unknown"}}}

    async def _op_snapshot(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        binding, row = self._call_binding(owner_id, payload)
        usage = _public_usage(json.loads(row["usage_json"])) if row["usage_json"] else None
        return {"ok": True, "snapshot": {"scope": binding.public_scope(), "phase": row["phase"],
                "latest_sequence": int(row["latest_session_event_sequence"]), "started_at": row["started_at"],
                "ended_at": row["ended_at"], "provider_close_state": row["provider_close_state"], "usage": usage,
                "resume_required": str(row["phase"]) == "recovering",
                "phone": self._stored_phone_public(row), "actions": self.actions.snapshot(binding),
                "media": self._provider_for(binding).media_descriptor(),
                "opening": self.opening.read(binding)}}

    async def _op_events(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        binding, _call_row = self._call_binding(owner_id, payload)
        try:
            after, limit = int(payload.get("after", 0)), int(payload.get("limit", 200))
        except (TypeError, ValueError) as exc:
            raise LiveVoiceError("live_page_invalid", 400) from exc
        if after < 0 or not 1 <= limit <= 500:
            raise LiveVoiceError("live_page_invalid", 400)
        scan_limit = min(2000, max(limit * 4, limit))
        with self.session_store._lock, self.session_store._connection() as connection:
            connection.execute("BEGIN")
            call = connection.execute(
                "SELECT phase FROM live_calls WHERE call_id = ?", (binding.call_id,)
            ).fetchone()
            current_phase = str(call["phase"]) if call is not None else "interrupted"
            rows = connection.execute(
                "SELECT sequence, kind, detail_json FROM run_events WHERE session_id = ? AND kind LIKE 'voice.live.%' AND sequence > ? ORDER BY sequence LIMIT ?",
                (binding.session_id, after, scan_limit),
            ).fetchall()
        events = []
        next_after = after
        for row in rows:
            next_after = int(row["sequence"])
            detail = json.loads(row["detail_json"] or "{}")
            if detail.get("scope") != binding.public_scope():
                continue
            # A recovering logical call supersedes historical passive terminal
            # events. Keep those rows in the audit history, but do not let an
            # older browser apply one before it sees the recovery state.
            event_phase = str(detail.get("phase") or "")
            if (
                current_phase not in TERMINAL_PHASES
                and row["kind"] == "voice.live.call.state"
                and event_phase in TERMINAL_PHASES
            ):
                continue
            events.append({"schema": detail.get("schema", CALL_EVENT_SCHEMA), "scope": binding.public_scope(),
                           "sequence": int(row["sequence"]), "kind": str(row["kind"]).removeprefix("voice.live."),
                           "detail": {key: value for key, value in detail.items() if key not in {"schema", "scope"}}})
            if len(events) >= limit:
                break
        return {"ok": True, "events": events,
                "next_after": next_after, "reset_required": False}

    async def _op_observe(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Append bounded browser lifecycle evidence outside Session events."""

        binding, _row = self._call_binding(owner_id, payload, require_current_transport=True)
        event = str(payload.get("event") or "")
        if event not in CLIENT_OBSERVATION_EVENTS:
            raise LiveVoiceError("live_observation_invalid", 400)
        event_id = identifier(payload.get("event_id"))
        observed_at = str(payload.get("observed_at") or "")
        if _parse_utc(observed_at) is None:
            raise LiveVoiceError("live_observation_invalid", 400)
        sequence = payload.get("client_sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 <= sequence <= 2**53 - 1
        ):
            raise LiveVoiceError("live_observation_invalid", 400)
        raw_detail = payload.get("detail")
        if raw_detail is None:
            raw_detail = {}
        if not isinstance(raw_detail, Mapping) or any(
            key not in CLIENT_OBSERVATION_DETAIL_KEYS for key in raw_detail
        ):
            raise LiveVoiceError("live_observation_invalid", 400)
        detail: dict[str, Any] = {}
        for key, value in raw_detail.items():
            if isinstance(value, bool):
                detail[key] = value
            elif isinstance(value, int) and not isinstance(value, bool):
                detail[key] = max(0, min(value, 1_000_000))
            elif isinstance(value, str) and len(value) <= 160:
                detail[key] = value
            else:
                raise LiveVoiceError("live_observation_invalid", 400)
        accepted = self.audit.record(
            binding,
            event,
            source="workbench",
            client_event_id=event_id,
            observed_at=observed_at,
            client_sequence=sequence,
            **detail,
        )
        if accepted:
            if event == "client.media_ready":
                self.opening.change(binding, event, **detail)
                self._schedule_opening(binding)
            elif event == "client.user_speech_started":
                self.opening.change(binding, "user.started")
            elif event in {"client.playback_started", "client.playback_progress"}:
                self.opening.change(binding, "playback", **detail)
            elif event == "client.playback_blocked":
                self.opening.change(binding, "uncertain", reason="playback_blocked")
        return {"ok": True, "accepted": accepted}

    async def _op_control(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        action = str(payload.get("action") or "")
        if action not in {"heartbeat", "mute", "unmute", "end"}:
            raise LiveVoiceError("live_action_invalid", 400)
        initiator = str(payload.get("termination_initiator") or "unknown") if action == "end" else ""
        reason = str(payload.get("termination_reason") or "legacy_unspecified") if action == "end" else ""
        allow_user_hangup_from_prior_epoch = (
            action == "end" and initiator == "user" and reason == "user_hangup"
        )
        binding, row = self._call_binding(
            owner_id, payload, require_current_transport=not allow_user_hangup_from_prior_epoch,
        )
        key = identifier(payload.get("idempotency_key"))
        digest = stable_digest(dict(payload))
        with self.session_store._lock, self.session_store._connection() as connection:
            prior = connection.execute(
                "SELECT request_digest, receipt_json FROM live_control_receipts WHERE call_id = ? AND idempotency_key = ?",
                (binding.call_id, key),
            ).fetchone()
        if prior is not None:
            if prior["request_digest"] != digest:
                raise LiveVoiceError("live_idempotency_conflict", 409)
            return json.loads(prior["receipt_json"])
        if action == "end":
            if initiator not in TERMINATION_INITIATORS or reason not in TERMINATION_REASONS:
                raise LiveVoiceError("live_termination_invalid", 400)
            if (initiator == "user") != (reason == "user_hangup"):
                raise LiveVoiceError("live_termination_invalid", 400)
            if initiator == "user":
                receipt = await self._finish_call(binding, reason="user_hangup", initiator="user")
                result = {"ok": True, "applied": True, **receipt}
            else:
                self._mark_recovering(
                    binding, reason=reason,
                    summary="Phone transport fault recorded; logical call remains open",
                )
                self.audit.record(binding, "termination.rejected_as_end", initiator=initiator, reason=reason)
                result = {"ok": True, "applied": False, "phase": "recovering",
                          "termination_initiator": initiator, "termination_reason": reason}
        elif row["phase"] in TERMINAL_PHASES or row["phase"] == "ending":
            raise LiveVoiceError("live_call_terminal", 409)
        elif action == "heartbeat":
            now = _utc_now_dt()
            expiry = (now + timedelta(seconds=45)).isoformat().replace("+00:00", "Z")
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute("UPDATE live_calls SET lease_expiry = ? WHERE call_id = ?", (expiry, binding.call_id))
            self.audit.record(
                binding,
                "client.heartbeat_received",
                expires_at=expiry,
                acknowledged=True,
            )
            result = {"ok": True, "applied": True, "action": "heartbeat", "renewed": True, "expires_at": expiry}
        else:
            result = {"ok": True, "applied": await self._send_control(binding, action, key), "action": action}
        with self.session_store._lock, self.session_store._connection() as connection:
            connection.execute(
                "INSERT INTO live_control_receipts(call_id, idempotency_key, request_digest, operation, receipt_json, created_at) VALUES (?, ?, ?, 'control', ?, ?)",
                (binding.call_id, key, digest, json.dumps(result), _utc_now()),
            )
        return result

    # Provider controls and lifetime ---------------------------------------
    async def _send_control(self, binding: CallBinding, action: str, event_id: str) -> bool:
        ws = self._active_sockets.get(binding.call_id)
        if ws is None or getattr(ws, "closed", True):
            self.audit.record(
                binding,
                "client.control_unavailable",
                action=action,
                client_event_id=event_id,
                reason="sideband_disconnected",
            )
            return False
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        identity = (binding.call_id, event_id)
        self._control_waiters[identity] = waiter
        try:
            self.audit.record(
                binding,
                "client.control_sent",
                action=action,
                client_event_id=event_id,
            )
            await ws.send_json(self._provider_for(binding).control(action, event_id))
            applied = await asyncio.wait_for(waiter, timeout=self._control_timeout_seconds)
            self.audit.record(
                binding,
                "client.control_acknowledged",
                action=action,
                client_event_id=event_id,
                acknowledged=bool(applied),
            )
            return applied
        except Exception as exc:
            self.audit.record(
                binding,
                "client.control_failed",
                action=action,
                client_event_id=event_id,
                **exception_evidence(exc),
            )
            return False
        finally:
            self._control_waiters.pop(identity, None)

    async def _finish_call(
        self,
        binding: CallBinding,
        *,
        reason: str,
        initiator: str | None = None,
    ) -> dict[str, Any]:
        initiator = str(initiator or "unknown")
        self.audit.record(binding, "termination.requested", initiator=initiator, reason=reason)
        if initiator != "user" or reason != "user_hangup":
            self._mark_recovering(
                binding, reason=reason,
                summary="Non-user termination request retained as a recoverable call",
            )
            return {"phase": "recovering", "provider_close_state": "unconfirmed",
                    "usage": None, "usage_finalization": "unavailable"}
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
            if row is None:
                raise LiveVoiceError("live_not_found", 404)
            if row["phase"] in TERMINAL_PHASES:
                usage = _public_usage(json.loads(row["usage_json"])) if row["usage_json"] else None
                return {"phase": row["phase"], "provider_close_state": row["provider_close_state"],
                        "usage": usage, "usage_finalization": "final" if usage else "unavailable"}
            if row["phase"] != "ending":
                connection.execute(
                    """UPDATE live_calls SET phase = 'ending', termination_initiator = 'user',
                       termination_reason = 'user_hangup' WHERE call_id = ?""",
                    (binding.call_id,),
                )
                self._append_call_state(connection, binding, "ending", "User requested hangup",
                                        termination_initiator="user", termination_reason="user_hangup")
        self.opening.change(binding, "ending")
        ws = self._active_sockets.get(binding.call_id)
        closed = self._session_closed_events.setdefault(binding.call_id, asyncio.Event())
        if ws is not None and not getattr(ws, "closed", True):
            try:
                await self._await_bounded(
                    ws.send_json(self._provider_for(binding).control("close", f"close-{uuid4().hex[:16]}")),
                    operation="user_hangup_send",
                )
                self.audit.record(binding, "provider.close_requested", initiator="user",
                                  reason="user_hangup", close_sent=True)
            except Exception as exc:
                self.audit.record(binding, "provider.close_request_failed", initiator="user",
                                  reason="user_hangup", close_sent=False, **exception_evidence(exc))
            try:
                await asyncio.wait_for(closed.wait(), timeout=self._close_timeout_seconds)
            except asyncio.TimeoutError:
                self.audit.record(binding, "provider.close_timeout", initiator="user",
                                  reason="user_hangup", provider_close_state="unconfirmed")
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
        close_state, usage = "unconfirmed", None
        if row is not None and row["phase"] == "ended":
            close_state = str(row["provider_close_state"] or "unconfirmed")
            usage = _public_usage(json.loads(row["usage_json"])) if row["usage_json"] else None
        else:
            if ws is not None and not getattr(ws, "closed", True):
                if await self._close_sideband_socket(
                    ws, binding=binding, operation="user_hangup_sideband_close"
                ):
                    self.audit.record(
                        binding, "provider.sideband_closed_for_cleanup",
                        initiator="user", reason="user_hangup",
                    )
            close_state, usage = await self._close_provider_session(
                self._get_api_key(binding), binding.provider_session_id, binding=binding
            )
            self._mark_terminal(binding, "ended", provider_close_state=close_state,
                                summary="User ended the live phone call", usage=usage)
            with self.session_store._lock, self.session_store._connection() as connection:
                row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
            if row is not None:
                close_state = str(row["provider_close_state"] or close_state)
                usage = _public_usage(json.loads(row["usage_json"])) if row["usage_json"] else usage
        public_usage = _public_usage(usage)
        return {"phase": "ended", "provider_close_state": close_state,
                "usage": public_usage, "usage_finalization": "final" if public_usage else "unavailable"}

    def _persist_call_record(self, binding: CallBinding) -> None:
        """Project the complete durable transcript as one visible chat record."""

        try:
            with self.session_store._lock, self.session_store._connection() as connection:
                call = connection.execute(
                    "SELECT started_at, ended_at FROM live_calls WHERE call_id = ?",
                    (binding.call_id,),
                ).fetchone()
            if call is None:
                return
            if self.session_store.has_pending_live_provider_fragments(binding.call_id):
                self.audit.record(
                    binding, "call.transcript_projection_deferred",
                    reason="provider_fragment_pending",
                )
                return
            segments = self.session_store.live_transcript_segments(
                binding.session_id,
                owner_id=binding.owner_id,
                context_generation=binding.context_generation,
                call_id=binding.call_id,
                call_epoch=None,
            )

            def timestamp(milliseconds: int) -> str:
                seconds = max(0, int(milliseconds) // 1000)
                return f"{seconds // 60}:{seconds % 60:02d}"

            lines = ["☎ Live call transcript"]
            if segments:
                for segment in segments:
                    marker = "🎙️" if segment["role"] == "user" else "🔊"
                    lines.extend(("", f"[?? {segment['call_epoch']} ? {timestamp(segment['start_ms'])}] {marker} {segment['text']}"))
            else:
                lines.extend(("", "(No speech was transcribed.)"))
            self.session_store.append_presentation_message(
                session_id=binding.session_id,
                owner_id=binding.owner_id,
                agent_id=binding.agent_id,
                role="assistant",
                text="\n".join(lines),
                source="live-phone",
                idempotency_key=f"live-call-record:{binding.call_id}:{binding.call_epoch}",
                content_format="plain-text",
                presentation_channel="final",
                history_eligible=False,
                message_context={
                    "live_call_record": {
                        "schema": "hashi.live_voice.transcript.v1",
                        "call_id": binding.call_id,
                        "call_epoch": binding.call_epoch,
                        "started_at": str(call["started_at"] or ""),
                        "ended_at": str(call["ended_at"] or ""),
                        "segment_count": len(segments),
                    }
                },
            )
            self.audit.record(
                binding,
                "call.transcript_projected",
                outcome="completed",
            )
        except Exception as exc:
            logger.warning(
                "Live Voice transcript record could not be projected for %s (%s)",
                binding.call_id,
                type(exc).__name__,
            )
            self.audit.record(
                binding,
                "call.transcript_projection_failed",
                **exception_evidence(exc),
            )

    def _is_current_provider_binding(self, binding: CallBinding) -> bool:
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT call_epoch, provider_session_id FROM live_calls WHERE call_id = ?",
                (binding.call_id,),
            ).fetchone()
        return bool(row and int(row["call_epoch"]) == binding.call_epoch
                    and str(row["provider_session_id"] or "") == binding.provider_session_id)

    def _mark_recovering(
        self,
        binding: CallBinding,
        *,
        reason: str,
        summary: str,
        provider_close_state: str | None = None,
    ) -> bool:
        changed = False
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT phase, termination_initiator, termination_reason, provider_close_state, "
                "call_epoch, provider_session_id FROM live_calls WHERE call_id = ?",
                (binding.call_id,),
            ).fetchone()
            if row is None or row["phase"] in TERMINAL_PHASES:
                return False
            if (int(row["call_epoch"]) != binding.call_epoch
                    or str(row["provider_session_id"] or "") != binding.provider_session_id):
                self.audit.record(binding, "call.stale_transport_state_ignored", requested_phase="recovering")
                return False
            if (row["phase"] == "ending" and row["termination_initiator"] == "user"
                    and row["termination_reason"] == "user_hangup"):
                return False
            close_state = provider_close_state or str(row["provider_close_state"] or "pending")
            if row["phase"] == "recovering" and row["provider_close_state"] == close_state:
                return False
            connection.execute(
                "UPDATE live_calls SET phase = 'recovering', ended_at = NULL, provider_close_state = ? WHERE call_id = ?",
                (close_state, binding.call_id),
            )
            self._append_call_state(connection, binding, "recovering", summary,
                                    reason=reason, provider_close_state=close_state)
            changed = True
        if changed:
            self.audit.record(binding, "call.recovery_required", reason=reason,
                              provider_close_state=close_state)
        return changed

    def _mark_terminal(
        self, binding: CallBinding, phase: str, *, provider_close_state: str,
        summary: str, usage: Mapping[str, Any] | None = None,
    ) -> None:
        public_usage = _public_usage(usage)
        became_terminal = False
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT phase, provider_close_state, usage_json, termination_initiator, termination_reason, "
                "call_epoch, provider_session_id FROM live_calls WHERE call_id = ?",
                (binding.call_id,),
            ).fetchone()
            if row is None:
                return
            if (int(row["call_epoch"]) != binding.call_epoch
                    or str(row["provider_session_id"] or "") != binding.provider_session_id):
                self.audit.record(binding, "call.stale_transport_state_ignored", requested_phase=phase)
                return
            if phase == "ended" and not (
                row["termination_initiator"] == "user"
                and row["termination_reason"] == "user_hangup"
            ):
                self.audit.record(binding, "provider.close_without_user_hangup",
                                  requested_phase=phase, provider_close_state=provider_close_state)
                return
            if row["phase"] in TERMINAL_PHASES:
                if provider_close_state == "confirmed" and (
                    row["provider_close_state"] != "confirmed" or (public_usage and not row["usage_json"])
                ):
                    connection.execute(
                        "UPDATE live_calls SET provider_close_state = 'confirmed', usage_json = COALESCE(?, usage_json) WHERE call_id = ?",
                        (json.dumps(public_usage) if public_usage is not None else None, binding.call_id),
                    )
                    self.session_store._append_event(
                        connection,
                        session_id=binding.session_id,
                        run_id=None,
                        kind="voice.live.call.finalized",
                        summary="Live call provider close finalized",
                        detail={"schema": CALL_EVENT_SCHEMA, "scope": binding.public_scope(),
                                "provider_close_state": "confirmed", "usage": public_usage},
                    )
                    self.audit.record(
                        binding,
                        "call.provider_close_finalized",
                        provider_close_state="confirmed",
                        usage=public_usage,
                    )
                return
            connection.execute(
                "UPDATE live_calls SET phase = ?, ended_at = ?, provider_close_state = ?, usage_json = ? WHERE call_id = ?",
                (phase, _utc_now(), provider_close_state,
                 json.dumps(public_usage) if public_usage is not None else None, binding.call_id),
            )
            self._append_call_state(connection, binding, phase, summary,
                                    provider_close_state=provider_close_state, usage=public_usage)
            became_terminal = True
        if became_terminal:
            queue = self._provider_event_queues.get(binding.call_id)
            if queue is not None and queue._unfinished_tasks:
                task = asyncio.create_task(
                    self._project_terminal_record_after_events(binding, queue),
                    name=f"hashi-live-call-record-{binding.call_id}",
                )
                self._track(task, self._terminal_record_tasks, binding=binding)
            else:
                self._persist_call_record(binding)

    async def _project_terminal_record_after_events(
        self,
        binding: CallBinding,
        queue: asyncio.Queue[tuple[CallBinding, Mapping[str, Any]]],
    ) -> None:
        await queue.join()
        with self.session_store._lock, self.session_store._connection() as connection:
            call = connection.execute(
                "SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)
            ).fetchone()
        if call is not None and str(call["phase"]) in TERMINAL_PHASES:
            self._persist_call_record(self._binding_from_row(call))

    async def _await_bounded(self, awaitable: Awaitable[Any], *, operation: str) -> Any:
        """Return after the close budget even if a WebSocket coroutine ignores cancellation."""
        task = asyncio.create_task(awaitable, name=f"hashi-live-{operation}")
        done, _pending = await asyncio.wait({task}, timeout=self._close_timeout_seconds)
        if task not in done:
            task.cancel()
            task.add_done_callback(self._consume_detached_operation)
            raise asyncio.TimeoutError(f"Live Phone {operation} exceeded its time limit")
        return await task

    @staticmethod
    def _consume_detached_operation(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        with suppress(Exception):
            task.exception()

    async def _close_sideband_socket(
        self,
        ws: Any,
        *,
        operation: str,
        binding: CallBinding | None = None,
    ) -> bool:
        if getattr(ws, "closed", True):
            return True
        try:
            await self._await_bounded(ws.close(), operation=operation)
        except Exception as exc:
            if binding is not None:
                self.audit.record(
                    binding, "provider.socket_operation_failed",
                    operation=operation,
                    **exception_evidence(exc),
                )
            else:
                logger.warning(
                    "Live Phone %s did not finish cleanly (%s)",
                    operation, type(exc).__name__,
                )
            return False
        return True

    async def _close_provider_session(
        self,
        key: str,
        provider_session_id: str,
        *,
        binding: CallBinding | None = None,
    ) -> tuple[str, dict[str, int] | None]:
        if binding is not None:
            self.audit.record(binding, "provider.cleanup_started", source="server")
        try:
            adapter = self._provider_for(binding, provider_session_id=provider_session_id)
            async with adapter.http_session() as http:
                ws = await adapter.attach(http, key=key, provider_session_id=provider_session_id)
                try:
                    await self._await_bounded(
                        ws.send_json(adapter.control("close", f"close-{uuid4().hex[:16]}")),
                        operation="provider_cleanup_send",
                    )

                    async def receive_close_event() -> Mapping[str, Any] | None:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                event = adapter.normalize_event(msg.data)
                                if event and event.get("type") == "provider.closed":
                                    return event
                        return None

                    event = await asyncio.wait_for(
                        receive_close_event(), timeout=self._close_timeout_seconds
                    )
                    if event is not None:
                        if binding is not None:
                            self.audit.record(
                                binding,
                                "provider.cleanup_confirmed",
                                provider_close_state="confirmed",
                                provider_reason=_provider_close_reason(event),
                                usage=_event_usage(event),
                            )
                        return "confirmed", _event_usage(event)
                finally:
                    if not ws.closed:
                        await self._close_sideband_socket(
                            ws, binding=binding, operation="provider_cleanup_socket_close"
                        )
        except Exception as exc:
            if binding is not None:
                self.audit.record(
                    binding,
                    "provider.cleanup_failed",
                    provider_close_state="unconfirmed",
                    **exception_evidence(exc),
                )
            return "unconfirmed", None
        if binding is not None:
            self.audit.record(
                binding,
                "provider.cleanup_unconfirmed",
                provider_close_state="unconfirmed",
            )
        return "unconfirmed", None

    async def _stage_provider_event_before_enqueue(
        self, binding: CallBinding, event: Mapping[str, Any]
    ) -> bool:
        event_type = str(event.get("type") or "unknown")
        event_id = str(event.get("event_id") or "")[:160]
        try:
            eligible = self._validate_provider_event_for_recovery(event)
        except (LiveVoiceError, TypeError, ValueError, UnicodeError) as exc:
            try:
                self.audit.record(
                    binding, "provider.event_staging_rejected",
                    provider_event_type=event_type,
                    provider_event_id=event_id,
                    error_code=getattr(exc, "code", "live_provider_event_invalid"),
                    reason="non_retryable_validation",
                )
            except Exception:
                logger.exception(
                    "Unable to audit rejected provider event for %s",
                    binding.call_id,
                )
            return False
        if not eligible:
            return False
        attempts = 0
        interrupted = False
        stage_task = asyncio.create_task(
            asyncio.to_thread(self._stage_provider_event_for_recovery, binding, event)
        )
        while True:
            try:
                staged = await asyncio.shield(stage_task)
            except asyncio.CancelledError:
                # A Function replacement may cancel this reader while SQLite
                # is writing. Let that transaction settle before acknowledging
                # cancellation; on failure, keep retrying until it is durable.
                if not interrupted:
                    interrupted = True
                    try:
                        self.audit.record(
                            binding, "provider.event_staging_interrupted",
                            provider_event_type=event_type,
                            provider_event_id=event_id,
                            reason="waiting_for_durable_stage",
                        )
                    except Exception:
                        logger.exception(
                            "Unable to audit provider event staging interruption for %s",
                            binding.call_id,
                        )
                continue
            except (LiveVoiceError, SessionConflict) as exc:
                try:
                    self.audit.record(
                        binding, "provider.event_staging_rejected",
                        provider_event_type=event_type,
                        provider_event_id=event_id,
                        error_code=getattr(exc, "code", "session_conflict"),
                        reason="non_retryable_validation",
                    )
                except Exception:
                    logger.exception(
                        "Unable to audit rejected provider event for %s",
                        binding.call_id,
                    )
                return False
            except Exception as exc:
                attempts += 1
                retry_delay = min(0.1 * attempts, 5.0)
                if attempts == 1 or attempts % 12 == 0:
                    try:
                        self.audit.record(
                            binding, "provider.event_staging_failed",
                            provider_event_type=event_type,
                            provider_event_id=event_id,
                            attempt=attempts,
                            retry_delay_s=round(retry_delay, 2),
                            **exception_evidence(exc),
                        )
                    except Exception:
                        logger.exception(
                            "Unable to audit provider event staging failure for %s",
                            binding.call_id,
                        )
                stage_task = asyncio.create_task(
                    asyncio.to_thread(self._stage_provider_event_for_recovery, binding, event)
                )
                try:
                    await asyncio.sleep(retry_delay)
                except asyncio.CancelledError:
                    interrupted = True
                    try:
                        self.audit.record(
                            binding, "provider.event_staging_interrupted",
                            provider_event_type=event_type,
                            provider_event_id=event_id,
                            reason="waiting_for_durable_stage",
                        )
                    except Exception:
                        logger.exception(
                            "Unable to audit provider event staging interruption for %s",
                            binding.call_id,
                        )
                continue
            if staged:
                try:
                    self.audit.record(
                        binding, "provider.event_staged",
                        provider_event_type=event_type,
                        provider_event_id=event_id,
                        attempts=attempts + 1,
                    )
                except Exception:
                    logger.exception(
                        "Unable to audit staged provider event for %s",
                        binding.call_id,
                    )
            if interrupted:
                raise asyncio.CancelledError
            return bool(staged)

    async def _run_sideband(self, binding: CallBinding, key: str, provider_session_id: str) -> None:
        ready = self._sideband_ready_events.setdefault(binding.call_id, asyncio.Event())
        confirmed_close = False
        self.audit.record(binding, "sideband.attach_started", source="provider")
        try:
            adapter = self._provider_for(binding, provider_session_id=provider_session_id)
            async with adapter.http_session() as http:
                ws = await adapter.attach(http, key=key, provider_session_id=provider_session_id)
                self._active_sockets[binding.call_id] = ws
                self.audit.record(binding, "sideband.connected", source="provider")
                ready.set()
                try:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                self.audit.record(
                                    binding,
                                    "sideband.socket_closed",
                                    ws_message_type=str(msg.type),
                                    ws_close_code=getattr(ws, "close_code", None),
                                )
                                break
                            continue
                        event = adapter.normalize_event(msg.data)
                        if event is None:
                            continue
                        event_type = event.get("type")
                        if event_type == "output.generated":
                            # Samples never reach PAO. While an opening is pending,
                            # retain only bounded in-memory activity/timing evidence.
                            # This fast path performs no per-packet SQLite or audit writes.
                            observation = self._opening_audio.get((binding.call_id, binding.call_epoch, binding.provider_session_id))
                            if observation is not None:
                                observation.observe(event)
                            continue
                        if not self._is_current_provider_binding(binding):
                            self.audit.record(binding, "provider.stale_epoch_event_ignored",
                                              provider_event_type=str(event_type or "unknown"))
                            continue
                        if event_type in {"conversation.user.delta", "conversation.assistant.delta"}:
                            # Observe ordering when received, before a slow durable
                            # stage could make pre-request speech look subsequent.
                            self._observe_opening_provider(binding, event)
                        if event_type == "action.proposed":
                            proposal = event.get("delegation")
                            if isinstance(proposal, Mapping) and isinstance(proposal.get("id"), str):
                                native_ids = self._provider_native_delegations.setdefault((binding.call_id, binding.call_epoch), set())
                                if len(native_ids) < 4096:
                                    native_ids.add(proposal["id"])
                        client_event_id = _provider_client_event_id(event)
                        event_detail: dict[str, Any] = {
                            "provider_event_type": str(event_type or "unknown"),
                            "provider_event_id": str(event.get("event_id") or "")[:160],
                        }
                        if event_type in {
                            "conversation.user.delta",
                            "conversation.assistant.delta",
                        }:
                            event_detail.update({
                                "speaker": "user" if event_type == "conversation.user.delta" else "assistant",
                                "start_ms": event.get("start_ms"),
                                "end_ms": event.get("end_ms"),
                                "text_bytes": len(
                                    str(event.get("delta") or "").encode("utf-8", errors="replace")
                                ),
                            })
                        self.audit.record(binding, "provider.event_received", **event_detail)
                        if event_type in {
                            "conversation.user.delta",
                            "conversation.assistant.delta",
                            "action.proposed",
                        }:
                            # Do not accept critical foreground content into a
                            # process-only queue before a durable recovery copy exists.
                            staged = await self._stage_provider_event_before_enqueue(binding, event)
                            if not staged:
                                continue
                        if event_type == "control.accepted" and client_event_id:
                            waiter = self._control_waiters.get((binding.call_id, client_event_id))
                            if waiter is not None and not waiter.done():
                                waiter.set_result(True)
                        if event_type == "update.accepted" and client_event_id:
                            waiter = self._update_waiters.get((binding.call_id, client_event_id))
                            if waiter is not None and not waiter.done():
                                waiter.set_result(True)
                        if event_type == "provider.error":
                            for waiters in (self._update_waiters, self._control_waiters):
                                waiter = waiters.get((binding.call_id, client_event_id))
                                if waiter is not None and not waiter.done():
                                    waiter.set_result(False)
                            logger.warning(
                                "Live Voice provider rejected %s for %s (%s)",
                                client_event_id or "an uncorrelated event",
                                binding.call_id,
                                _provider_error_code(event),
                            )
                            self.audit.record(
                                binding,
                                "provider.operation_error",
                                client_event_id=client_event_id,
                                error_code=_provider_error_code(event),
                            )
                            # Live errors are operation-scoped. The provider
                            # will emit session.closed or close the socket if
                            # the whole conversation is no longer usable.
                            continue
                        if event_type == "provider.ready":
                            self.audit.record(binding, "provider.session_started", source="provider")
                            with self.session_store._lock, self.session_store._connection() as connection:
                                row = connection.execute(
                                    "SELECT phase, call_epoch, provider_session_id FROM live_calls WHERE call_id = ?",
                                    (binding.call_id,),
                                ).fetchone()
                                is_current = bool(
                                    row is not None
                                    and int(row["call_epoch"]) == binding.call_epoch
                                    and str(row["provider_session_id"] or "") == binding.provider_session_id
                                )
                                if is_current and row["phase"] == "connecting":
                                    updated = connection.execute(
                                        """UPDATE live_calls SET phase = 'active'
                                           WHERE call_id = ? AND phase = 'connecting'
                                             AND call_epoch = ? AND provider_session_id = ?""",
                                        (binding.call_id, binding.call_epoch, binding.provider_session_id),
                                    )
                                    if updated.rowcount == 1:
                                        self._append_call_state(connection, binding, "active", "Live call active")
                                elif not is_current:
                                    self.audit.record(binding, "provider.stale_session_started_ignored")
                            if is_current:
                                self._recover_action_relays(call_id=binding.call_id)
                        elif event_type == "provider.closed":
                            confirmed_close = True
                            provider_reason = _provider_close_reason(event)
                            self.audit.record(binding, "provider.session_closed",
                                              provider_reason=provider_reason,
                                              provider_close_state="confirmed", usage=_event_usage(event))
                            with self.session_store._lock, self.session_store._connection() as connection:
                                call = connection.execute(
                                    "SELECT termination_initiator, termination_reason FROM live_calls WHERE call_id = ?",
                                    (binding.call_id,),
                                ).fetchone()
                            if call is not None and call["termination_initiator"] == "user" and call["termination_reason"] == "user_hangup":
                                self._mark_terminal(binding, "ended", provider_close_state="confirmed",
                                                    summary="User ended the live phone call", usage=_event_usage(event))
                            else:
                                self._mark_recovering(binding, reason="provider_session_closed",
                                                      summary="Provider transport closed; logical phone remains open",
                                                      provider_close_state="confirmed")
                            if self._is_current_provider_binding(binding):
                                self._session_closed_events.setdefault(binding.call_id, asyncio.Event()).set()
                            break
                        if event_type not in {"conversation.user.delta", "conversation.assistant.delta"}:
                            self._observe_opening_provider(binding, event)
                        # Persist asynchronously in per-call order. A slow Session write
                        # must not stop this reader from seeing session.closed or a user hangup.
                        self._enqueue_provider_event(binding, event)
                finally:
                    self._provider_native_delegations.pop((binding.call_id, binding.call_epoch), None)
                    if self._active_sockets.get(binding.call_id) is ws:
                        self._active_sockets.pop(binding.call_id, None)
                    if not ws.closed:
                        await self._close_sideband_socket(
                            ws, binding=binding, operation="sideband_socket_close"
                        )
        except asyncio.CancelledError:
            self.audit.record(binding, "sideband.cancelled", reason="task_cancelled")
            raise
        except Exception as exc:
            try:
                if self._is_current_provider_binding(binding):
                    self._sideband_failures.setdefault(binding.call_id, "live_sideband_unavailable")
            except Exception:
                pass
            logger.warning("Live Voice sideband interrupted for %s: %s", binding.call_id, type(exc).__name__)
            self.audit.record(binding, "sideband.exception", **exception_evidence(exc))
        finally:
            ready.set()
            if not confirmed_close and not self._closing:
                self.audit.record(binding, "sideband.disconnected", provider_close_state="unconfirmed")
                self._mark_recovering(binding, reason="sideband_disconnected",
                                      summary="Phone sideband disconnected; logical call remains open",
                                      provider_close_state="unconfirmed")
