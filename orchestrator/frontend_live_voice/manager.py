"""Authoritative Live Voice state, provider sideband, and PAO admission."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

import aiohttp

from orchestrator.phone_catalog import (
    OPENAI_LIVE_VOICES,
    OPENAI_LIVE_VOICE_PRESENTATIONS,
)
from .audit import LiveVoiceAuditLog, exception_evidence
from .delegation import Proposal, build_proposal
from .openai_live import (
    append_update,
    attach_provider,
    create_provider_session,
    fit_live_session_input,
    provider_http_session,
    safe_sideband_event,
    session_request,
)
from .ports import AdmissionPort, DurableVoicePort, LiveApplicationPort
from .protocol import CallBinding, Fragment, LiveVoiceError, identifier, positive_int, stable_digest
from .service import LiveVoiceEventService
from orchestrator.session_store import TERMINAL_RUN_STATES
from tools.token_tracker import estimate_tokens

logger = logging.getLogger(__name__)
ACTIVE_PHASES = {"connecting", "active", "ending"}
TERMINAL_PHASES = {"ended", "failed", "interrupted"}
CALL_EVENT_SCHEMA = "hashi.live_voice.event.v1"
LIVE_UPDATE_TOKEN_LIMIT = 320
LIVE_UPDATE_BYTE_LIMIT = 512
CLIENT_OBSERVATION_EVENTS = frozenset({
    "client.data_channel_state",
    "client.event_poll_failed",
    "client.event_poll_recovered",
    "client.heartbeat_failed",
    "client.heartbeat_recovered",
    "client.ice_connection_state",
    "client.ice_gathering_state",
    "client.media_track_state",
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
    "state", "visible",
})
TERMINATION_INITIATORS = frozenset({"client_fault", "system", "unknown", "user"})
TERMINATION_REASONS = frozenset({
    "component_dispose", "data_channel_closed", "data_channel_error",
    "lease_expired", "legacy_unspecified", "maximum_duration_reached", "network_offline",
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
        resolve_phone_session: Callable[..., Mapping[str, Any]] | None = None,
        proposal_grace_seconds: float = 0.25,
        control_timeout_seconds: float = 5.0,
        close_timeout_seconds: float = 8.0,
    ):
        self.session_store = session_store
        self.global_config = global_config
        self.secrets = dict(secrets or {})
        self.instance_id = str(getattr(global_config, "instance_id", "HASHI") or "HASHI").upper()
        self.instance_generation = str(getattr(global_config, "instance_generation", "1") or "1")
        self._feature_enabled = bool(getattr(global_config, "live_voice_v1", False))
        self._availability_override: bool | None = None
        self._admit_run = admit_run
        self._poll_run_activity = poll_run_activity
        self._resolve_phone_session = resolve_phone_session
        self._proposal_grace_seconds = max(0.0, float(proposal_grace_seconds))
        self._control_timeout_seconds = max(0.05, float(control_timeout_seconds))
        self._close_timeout_seconds = max(0.05, float(close_timeout_seconds))
        configured_logs = getattr(global_config, "base_logs_dir", None)
        default_logs = Path(self.session_store.db_path).parent.parent / "logs"
        self.audit = LiveVoiceAuditLog(Path(configured_logs or default_logs) / "voice_sessions")
        self.service = LiveVoiceEventService(self)
        self._sideband_tasks: set[asyncio.Task[Any]] = set()
        self._proposal_tasks: set[asyncio.Task[Any]] = set()
        self._relay_tasks: set[asyncio.Task[Any]] = set()
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
        return self._feature_enabled and bool(self._get_api_key())

    @available.setter
    def available(self, value: bool) -> None:
        self._availability_override = bool(value)

    def _get_api_key(self) -> str:
        import os
        return str(self.secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()

    def _phone_session(
        self,
        agent_id: str,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
        context_generation: int | None = None,
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
            value = (
                resolver(
                    agent_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    context_generation=context_generation,
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
        provider = str(value.get("provider") or "")
        model = str(value.get("model") or "")
        voice = str(value.get("voice") or "")
        instructions = value.get("instructions")
        public = value.get("public")
        if provider != "openai":
            raise LiveVoiceError("live_provider_unqualified", 503)
        if model != "gpt-live-1":
            raise LiveVoiceError("live_model_unqualified", 503)
        if voice not in OPENAI_LIVE_VOICES:
            raise LiveVoiceError("live_voice_unqualified", 503)
        if not isinstance(instructions, str) or not instructions:
            raise LiveVoiceError("live_instructions_invalid", 503)
        input_messages = value.get("input", [])
        if not isinstance(input_messages, list):
            raise LiveVoiceError("live_input_invalid", 503)
        try:
            validated = session_request(
                sdp="v=0",
                instructions=instructions,
                model=model,
                voice=voice,
                input_messages=input_messages,
            )["session"]["input"]
        except LiveVoiceError as exc:
            raise LiveVoiceError(exc.code, 503) from exc
        if not isinstance(public, Mapping):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        revision = str(public.get("revision") or "")
        if len(revision) != 64 or any(character not in "0123456789abcdef" for character in revision):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        if any(str(public.get(key) or "") != expected for key, expected in {
            "provider": provider, "model": model, "voice": voice,
        }.items()):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        if (
            "voice_presentation" in public
            and public.get("voice_presentation") != OPENAI_LIVE_VOICE_PRESENTATIONS.get(voice)
        ):
            raise LiveVoiceError("live_phone_configuration_invalid", 503)
        allowed_public = {
            key: public[key]
            for key in (
                "revision", "provider", "provider_label", "model", "model_label",
                "voice", "voice_label", "voice_presentation", "language", "language_label", "style",
                "style_label", "custom_style", "persona_projected",
            )
            if key in public
        }
        return {
            "provider": provider,
            "model": model,
            "voice": voice,
            "instructions": instructions,
            "input": validated,
            "context_audit": dict(value.get("context_audit") or {}),
            "public": allowed_public,
        }

    @staticmethod
    def _stored_phone_public(row: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            value = json.loads(row["phone_config_json"] or "{}")
        except (KeyError, TypeError, ValueError):
            return None
        public = value.get("public") if isinstance(value, Mapping) else None
        return dict(public) if isinstance(public, Mapping) else None

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
        now = _utc_now()
        with self.session_store._lock, self.session_store._connection() as connection:
            active_rows = connection.execute(
                "SELECT * FROM live_calls WHERE phase IN ('connecting', 'active', 'ending')"
            ).fetchall()
            for row in active_rows:
                binding = self._binding_from_row(row)
                self.audit.record(
                    binding,
                    "runtime.recovery_detected",
                    phase=str(row["phase"]),
                    reason="service_shutdown",
                )
                connection.execute(
                    "UPDATE live_calls SET phase = 'interrupted', ended_at = ?, provider_close_state = 'unconfirmed' WHERE call_id = ?",
                    (now, binding.call_id),
                )
                self._append_call_state(
                    connection,
                    binding,
                    "interrupted",
                    "Live call interrupted during recovery",
                    reason="service_shutdown",
                )
            proposal_rows = connection.execute(
                """
                SELECT d.delegation_id, d.offset_ms, c.*
                FROM live_delegations AS d JOIN live_calls AS c ON c.call_id = d.call_id
                WHERE d.proposal_state = 'pending'
                """
            ).fetchall()
            decision_rows = connection.execute(
                """
                SELECT r.idempotency_key, r.request_digest, r.receipt_json, c.*
                FROM live_control_receipts AS r JOIN live_calls AS c ON c.call_id = r.call_id
                WHERE r.operation IN ('decision_pending', 'admission_pending')
                """
            ).fetchall()
        if active_rows and self._get_api_key():
            await asyncio.gather(*(
                self._recover_orphan_provider(self._binding_from_row(row)) for row in active_rows
            ))
        for row in active_rows:
            self._persist_call_record(self._binding_from_row(row))
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

    async def _recover_orphan_provider(self, binding: CallBinding) -> None:
        close_state, usage = await self._close_provider_session(
            self._get_api_key(), binding.provider_session_id, binding=binding
        )
        if close_state == "confirmed":
            self._mark_terminal(
                binding, "interrupted", provider_close_state="confirmed",
                summary="Recovered provider session was closed", usage=usage,
            )

    async def shutdown(self) -> None:
        self._closing = True
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            await asyncio.gather(self._sweeper_task, return_exceptions=True)
            self._sweeper_task = None
        for collection in (self._proposal_tasks, self._relay_tasks, self._recovery_tasks):
            for task in tuple(collection):
                task.cancel()
            if collection:
                await asyncio.gather(*tuple(collection), return_exceptions=True)
                collection.clear()
        with self.session_store._lock, self.session_store._connection() as connection:
            active_rows = connection.execute(
                "SELECT * FROM live_calls WHERE phase IN ('connecting', 'active', 'ending')"
            ).fetchall()
        if active_rows:
            await asyncio.gather(*(
                self._finish_call(self._binding_from_row(row), reason="service_shutdown")
                for row in active_rows
            ), return_exceptions=True)
        for ws in tuple(self._active_sockets.values()):
            if not getattr(ws, "closed", True):
                with suppress(Exception):
                    await ws.close()
        for task in tuple(self._sideband_tasks):
            task.cancel()
        if self._sideband_tasks:
            await asyncio.gather(*tuple(self._sideband_tasks), return_exceptions=True)
            self._sideband_tasks.clear()
        self._active_sockets.clear()
        now = _utc_now()
        with self.session_store._lock, self.session_store._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM live_calls WHERE phase IN ('connecting', 'active', 'ending')"
            ).fetchall()
            for row in rows:
                binding = self._binding_from_row(row)
                connection.execute(
                    "UPDATE live_calls SET phase = 'interrupted', ended_at = ?, provider_close_state = 'unconfirmed' WHERE call_id = ?",
                    (now, binding.call_id),
                )
                self._append_call_state(
                    connection,
                    binding,
                    "interrupted",
                    "Live call interrupted during shutdown",
                    reason="service_shutdown",
                )
        for row in rows:
            self._persist_call_record(self._binding_from_row(row))

    async def _sweeper_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                now = _utc_now()
                with self.session_store._lock, self.session_store._connection() as connection:
                    rows = connection.execute(
                        """SELECT * FROM live_calls
                        WHERE phase IN ('connecting', 'active', 'ending')
                          AND (lease_expiry <= ? OR (max_ends_at IS NOT NULL AND max_ends_at <= ?))""",
                        (now, now),
                    ).fetchall()
                for row in rows:
                    binding = self._binding_from_row(row)
                    maximum = _parse_utc(row["max_ends_at"])
                    reason = (
                        "maximum_duration_reached"
                        if maximum is not None and maximum <= now
                        else "lease_expired"
                    )
                    self._track(asyncio.create_task(
                        self._finish_call(binding, reason=reason),
                        name=f"hashi-live-expire-{binding.call_id}",
                    ), self._recovery_tasks, binding=binding)
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

    def _call_binding(self, owner_id: str, payload: Mapping[str, Any]) -> tuple[CallBinding, Mapping[str, Any]]:
        call_id = identifier(payload.get("call_id"))
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (call_id,)).fetchone()
        if row is None:
            raise LiveVoiceError("live_not_found", 404)
        binding = self._binding_from_row(row)
        binding.require_scope(self._expected_scope(payload), authenticated_owner=owner_id)
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

    # Durable transcript and proposal state ---------------------------------
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
                return False
            prior = connection.execute(
                "SELECT COALESCE(MAX(cutoff_ms), 0) AS watermark FROM live_delegations WHERE call_id = ? AND call_epoch = ?",
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

    async def _admit_ready_delegation(self, binding: CallBinding, proposal: Proposal) -> None:
        """Admit provider delegation as an ordinary Agent turn, without a phone-only gate."""

        if proposal.ambiguous or not proposal.text.strip():
            await self._send_provider_update(
                binding,
                kind="commentary",
                content=(
                    "I could not recover an unambiguous spoken request. Ask the user to "
                    "repeat it before starting any work."
                ),
                delegation_id=proposal.delegation_id,
            )
            return
        admission = {
            "delegation_id": proposal.delegation_id,
            "proposal_version": proposal.version,
            "proposal_digest": proposal.digest,
            "mode": "automatic_agent_turn",
        }
        request_digest = stable_digest(admission)
        idempotency_key = f"live-auto-{request_digest[:32]}"
        for attempt in range(2):
            try:
                await self.admit_delegation(
                    binding,
                    proposal,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                )
                return
            except LiveVoiceError as exc:
                if exc.code == "live_outcome_unknown" and attempt == 0:
                    await asyncio.sleep(0.25)
                    continue
                logger.warning(
                    "Live Voice delegation was not admitted for %s (%s)",
                    proposal.delegation_id,
                    exc.code,
                )
                await self._send_provider_update(
                    binding,
                    kind="commentary",
                    content=(
                        "I could not start the requested work. Tell the user plainly that it "
                        "was not started and ask whether they want to try again."
                    ),
                    delegation_id=proposal.delegation_id,
                )
                return

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
        return await self._complete_pending_admission(binding, proposal, idempotency_key, request_digest)

    async def _complete_pending_admission(
        self, binding: CallBinding, proposal: Proposal, idempotency_key: str, request_digest: str,
    ) -> Mapping[str, Any]:
        try:
            accepted = await self._admit_run(  # type: ignore[misc]
                binding, proposal, f"live-delegation-{idempotency_key}"
            )
        except LiveVoiceError:
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
        """Append bounded context/results to GPT-Live through the trusted sideband."""

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
            payload = append_update(kind, chunk, delegation_id, event_id, estimate_tokens)
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
    def _activity_update(event: Mapping[str, Any]) -> tuple[str, str] | None:
        channel = str(event.get("presentation_channel") or "").strip().casefold()
        if channel in {"", "internal", "thinking", "reasoning", "answer"}:
            return None
        if channel not in {"commentary", "control", "verbose", "technical", "status", "progress"}:
            return None
        if channel in {"commentary", "control"} and not (
            event.get("presentation_enabled") is True or event.get("required") is True
        ):
            return None
        summary = str(event.get("summary") or "").strip()
        detail = event.get("detail")
        detail_text = str(detail).strip() if isinstance(detail, str) else ""
        content = summary or detail_text
        if not content:
            return None
        return ("commentary" if channel in {"commentary", "control"} else "thinking", content)

    async def _relay_run(
        self,
        binding: CallBinding,
        delegation_id: str,
        run_id: str,
        request_id: str,
    ) -> None:
        """Return one normal Agent Run's safe progress and final result to GPT-Live."""

        if binding.call_id not in self._active_sockets:
            self.audit.record(
                binding,
                "background.relay_skipped",
                delegation_id=delegation_id,
                run_id=run_id,
                request_id=request_id,
                reason="sideband_disconnected",
            )
            return
        self.audit.record(
            binding,
            "background.relay_started",
            delegation_id=delegation_id,
            run_id=run_id,
            request_id=request_id,
        )
        await self._send_provider_update(
            binding,
            kind="thinking",
            content=(
                "Your HASHI execution for this delegation has started. Treat it as your own "
                "ongoing work and wait for verified updates before claiming completion."
            ),
            delegation_id=delegation_id,
        )
        activity_after = 0
        last_state = ""
        while not self._closing:
            with self.session_store._lock, self.session_store._connection() as connection:
                call = connection.execute(
                    "SELECT phase FROM live_calls WHERE call_id = ?", (binding.call_id,)
                ).fetchone()
            if call is None or str(call["phase"]) in TERMINAL_PHASES:
                return
            if callable(self._poll_run_activity):
                try:
                    activity = await self._poll_run_activity(
                        binding, request_id, activity_after, 100
                    )
                except Exception as exc:
                    logger.debug(
                        "Live Voice activity polling unavailable for %s (%s)",
                        run_id,
                        type(exc).__name__,
                    )
                    activity = {}
                if isinstance(activity, Mapping) and activity.get("ok"):
                    for raw_event in activity.get("events") or ():
                        if not isinstance(raw_event, Mapping):
                            continue
                        sequence = raw_event.get("sequence")
                        if isinstance(sequence, int) and not isinstance(sequence, bool):
                            activity_after = max(activity_after, sequence)
                        update = self._activity_update(raw_event)
                        if update is not None:
                            update_kind, update_text = update
                            await self._send_provider_update(
                                binding,
                                kind=update_kind,
                                content=update_text,
                                delegation_id=delegation_id,
                            )
            try:
                run = self.session_store.get_run(run_id, owner_id=binding.owner_id)
            except Exception:
                return
            state = str(run.get("state") or "")
            if state == "awaiting_approval" and state != last_state:
                await self._send_provider_update(
                    binding,
                    kind="commentary",
                    content=(
                        "This work is waiting for an approval under your normal HASHI policy. "
                        "Explain the approval request naturally and accept the user's spoken response."
                    ),
                    delegation_id=delegation_id,
                )
            last_state = state
            if state in TERMINAL_RUN_STATES:
                if state == "completed" and run.get("final_message_id"):
                    try:
                        message = self.session_store.get_message(
                            run["final_message_id"],
                            session_id=binding.session_id,
                            owner_id=binding.owner_id,
                        )
                        final_text = str(message.get("text") or "").strip()
                    except Exception:
                        final_text = ""
                    if final_text:
                        chunks = _live_text_chunks(final_text)
                        if len(chunks) == 1:
                            relayed = await self._send_provider_update(
                                binding,
                                kind="commentary",
                                content=chunks[0],
                                delegation_id=delegation_id,
                            )
                        else:
                            relayed = True
                            for chunk in chunks:
                                relayed = await self._send_provider_update(
                                    binding,
                                    kind="thinking",
                                    content=chunk,
                                    delegation_id=delegation_id,
                                ) and relayed
                            relayed = await self._send_provider_update(
                                binding,
                                kind="commentary",
                                content=(
                                    "Your HASHI work is complete. Tell the user the result now in your "
                                    "own voice, using all result context appended for this delegation."
                                ),
                                delegation_id=delegation_id,
                            ) and relayed
                    else:
                        relayed = await self._send_provider_update(
                            binding,
                            kind="commentary",
                            content="The work completed and its result is available in the current chat.",
                            delegation_id=delegation_id,
                        )
                else:
                    error = str(run.get("error_text") or state or "the work stopped")
                    relayed = await self._send_provider_update(
                        binding,
                        kind="commentary",
                        content=f"The requested work did not complete: {error}",
                        delegation_id=delegation_id,
                    )
                if relayed:
                    with self.session_store._lock, self.session_store._connection() as connection:
                        self.session_store._append_event(
                            connection,
                            session_id=binding.session_id,
                            run_id=run_id,
                            kind="voice.live.delegation.result_relayed",
                            status=state,
                            phase="terminal",
                            summary="Live delegation result returned to the voice session",
                            detail={
                                "schema": CALL_EVENT_SCHEMA,
                                "scope": binding.public_scope(),
                                "delegation_id": delegation_id,
                                "request_id": request_id,
                            },
                        )
                    self.audit.record(
                        binding,
                        "background.result_relayed",
                        delegation_id=delegation_id,
                        run_id=run_id,
                        request_id=request_id,
                        outcome=state,
                    )
                else:
                    self.audit.record(
                        binding,
                        "background.result_relay_failed",
                        delegation_id=delegation_id,
                        run_id=run_id,
                        request_id=request_id,
                        outcome=state,
                    )
                return
            await asyncio.sleep(0.5)

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
            phone = self._phone_session(
                agent_id,
                owner_id=owner_id,
                session_id=session_id,
                context_generation=generation,
            )["public"]
        except LiveVoiceError as exc:
            configuration_error = exc.code
        return {
            "ok": True,
            "binding": {"instance_id": self.instance_id, "instance_generation": self.instance_generation,
                        "agent_id": agent_id, "session_id": session_id, "context_generation": generation},
            "capability": {"protocol_version": "1.0", "available": bool(self.available and phone),
                           "phone": phone, "configuration_error": configuration_error,
                           "heartbeat_interval_seconds": 10, "lease_expiry_seconds": 45,
                           "max_call_duration_seconds": 1800},
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
        expected_phone_revision = str(payload.get("phone_revision") or "")
        if expected_phone_revision != phone_session["public"]["revision"]:
            raise LiveVoiceError("live_phone_configuration_changed", 409)
        phone_record = {
            "public": phone_session["public"],
            "instructions_sha256": stable_digest({"instructions": phone_session["instructions"]}),
            "input_sha256": stable_digest({"input": phone_session["input"]}),
            "input_messages": len(phone_session["input"]),
            "input_tokens_est": int(phone_session["context_audit"].get("tokens_est") or 0),
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
                "SELECT * FROM live_calls WHERE owner_id = ? AND agent_id = ? AND phase IN ('connecting', 'active', 'ending')",
                (owner_id, expected["agent_id"]),
            ).fetchone()
            if active is not None:
                if active["lease_expiry"] > now:
                    raise LiveVoiceError("live_call_already_active", 409)
                old = self._binding_from_row(active)
                connection.execute(
                    "UPDATE live_calls SET phase = 'interrupted', ended_at = ?, provider_close_state = 'unconfirmed' WHERE call_id = ?",
                    (now, old.call_id),
                )
                self._append_call_state(
                    connection,
                    old,
                    "interrupted",
                    "Live call lease expired",
                    reason="lease_expired",
                )
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
            async with provider_http_session() as http:
                context_audit = phone_session["context_audit"]
                required_message_count = context_audit.get(
                    "required_message_count", len(phone_session["input"])
                )
                history_unit_message_counts = context_audit.get(
                    "history_unit_message_counts", []
                )
                provider_input, exact_audit = await fit_live_session_input(
                    http,
                    key=self._get_api_key(),
                    model=phone_session["model"],
                    input_messages=phone_session["input"],
                    required_message_count=required_message_count,
                    history_unit_message_counts=history_unit_message_counts,
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
                provider = await create_provider_session(
                    http, key=self._get_api_key(),
                    request=session_request(
                        sdp=sdp,
                        instructions=phone_session["instructions"],
                        model=phone_session["model"],
                        voice=phone_session["voice"],
                        input_messages=provider_input,
                        input_token_count_exact=exact_audit["input_tokens_exact"],
                    ),
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
                         (now_dt + timedelta(seconds=1800)).isoformat().replace("+00:00", "Z"),
                         phone_record_json),
                    )
                    self._append_call_state(connection, binding, "connecting", "Live call connecting")
        except Exception as exc:
            close_state, usage = await self._close_provider_session(self._get_api_key(), provider_id, binding=binding)
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
            await self._close_provider_session(self._get_api_key(), provider_id, binding=binding)
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
            self._run_sideband(binding, self._get_api_key(), provider_id),
            name=f"hashi-live-sideband-{binding.call_id}",
        ), self._sideband_tasks, binding=binding)
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
        except TimeoutError:
            self._sideband_failures[binding.call_id] = "live_sideband_timeout"
        failure = self._sideband_failures.pop(binding.call_id, None)
        if failure:
            close_state, usage = await self._close_provider_session(self._get_api_key(), provider_id, binding=binding)
            self._mark_terminal(binding, "failed", provider_close_state=close_state,
                                summary="Live call sideband failed", usage=usage)
            with self.session_store._lock, self.session_store._connection() as connection:
                connection.execute(
                    "UPDATE live_call_attempts SET state = 'failed', outcome_json = ?, updated_at = ? WHERE attempt_id = ?",
                    (json.dumps({"ok": False, "error_code": failure}), _utc_now(), attempt_id),
                )
            raise LiveVoiceError(failure, 502)
        outcome = {"ok": True, "accepted": True, "call_id": binding.call_id, "sideband_ready": True,
                   "provider_session_id": provider_id, "binding": binding.public_scope(),
                   "phone": phone_session["public"]}
        with self.session_store._lock, self.session_store._connection() as connection:
            check = connection.execute("SELECT cancel_requested FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if check is not None and check["cancel_requested"]:
            await self._finish_call(binding, reason="start_cancelled")
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

    async def _op_cancel_start(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        attempt_id = identifier(payload.get("attempt_id") or payload.get("idempotency_key"))
        expected = self._expected_scope(payload)
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_call_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None or row["owner_id"] != owner_id:
                raise LiveVoiceError("live_attempt_not_found", 404)
            for key in ("instance_id", "instance_generation", "agent_id", "session_id", "context_generation"):
                if str(row[key]) != str(expected[key]):
                    raise LiveVoiceError("live_scope_changed", 409)
            connection.execute(
                "UPDATE live_call_attempts SET cancel_requested = 1, updated_at = ? WHERE attempt_id = ?",
                (_utc_now(), attempt_id),
            )
            call_id, provider_id = row["call_id"], row["provider_id"]
        close_state = "not_created"
        if call_id:
            with self.session_store._lock, self.session_store._connection() as connection:
                call = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (call_id,)).fetchone()
            if call is not None:
                close_state = (await self._finish_call(self._binding_from_row(call), reason="start_cancelled"))["provider_close_state"]
        elif provider_id:
            close_state, _usage = await self._close_provider_session(self._get_api_key(), provider_id)
        return {"ok": True, "applied": True, "cancelled": True, "provider_close_state": close_state}

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
                "phone": self._stored_phone_public(row)}}

    async def _op_events(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        binding, _row = self._call_binding(owner_id, payload)
        try:
            after, limit = int(payload.get("after", 0)), int(payload.get("limit", 200))
        except (TypeError, ValueError) as exc:
            raise LiveVoiceError("live_page_invalid", 400) from exc
        if after < 0 or not 1 <= limit <= 500:
            raise LiveVoiceError("live_page_invalid", 400)
        scan_limit = min(2000, max(limit * 4, limit))
        with self.session_store._lock, self.session_store._connection() as connection:
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
            events.append({"schema": detail.get("schema", CALL_EVENT_SCHEMA), "scope": binding.public_scope(),
                           "sequence": int(row["sequence"]), "kind": str(row["kind"]).removeprefix("voice.live."),
                           "detail": {key: value for key, value in detail.items() if key not in {"schema", "scope"}}})
            if len(events) >= limit:
                break
        return {"ok": True, "events": events,
                "next_after": next_after, "reset_required": False}

    async def _op_observe(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Append bounded browser lifecycle evidence outside Session events."""

        binding, _row = self._call_binding(owner_id, payload)
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
        return {"ok": True, "accepted": accepted}

    async def _op_control(self, owner_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        binding, row = self._call_binding(owner_id, payload)
        action = str(payload.get("action") or "")
        if action not in {"heartbeat", "mute", "unmute", "end"}:
            raise LiveVoiceError("live_action_invalid", 400)
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
            initiator = str(payload.get("termination_initiator") or "unknown")
            reason = str(payload.get("termination_reason") or "legacy_unspecified")
            if initiator not in TERMINATION_INITIATORS or reason not in TERMINATION_REASONS:
                raise LiveVoiceError("live_termination_invalid", 400)
            if (initiator == "user") != (reason == "user_hangup"):
                raise LiveVoiceError("live_termination_invalid", 400)
            receipt = await self._finish_call(binding, reason=reason, initiator=initiator)
            result = {"ok": True, "applied": True, **receipt}
        elif row["phase"] in TERMINAL_PHASES or row["phase"] == "ending":
            raise LiveVoiceError("live_call_terminal", 409)
        elif action == "heartbeat":
            now = _utc_now_dt()
            maximum = _parse_utc(row["max_ends_at"]) or (now + timedelta(seconds=1800))
            expiry = min(now + timedelta(seconds=45), maximum).isoformat().replace("+00:00", "Z")
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
            await ws.send_json({"type": f"session.input_audio.{action}", "event_id": event_id})
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
        if initiator is None:
            initiator = "system"
            if reason in {"peer_connection_closed", "peer_connection_failed", "data_channel_closed", "data_channel_error", "network_offline"}:
                initiator = "client_fault"
        self.audit.record(
            binding,
            "termination.requested",
            initiator=initiator,
            reason=reason,
        )
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
            if row is None:
                raise LiveVoiceError("live_not_found", 404)
            if row["phase"] in TERMINAL_PHASES:
                usage = _public_usage(json.loads(row["usage_json"])) if row["usage_json"] else None
                return {"phase": row["phase"], "provider_close_state": row["provider_close_state"],
                        "usage": usage, "usage_finalization": "final" if usage else "unavailable"}
            if row["phase"] != "ending":
                connection.execute("UPDATE live_calls SET phase = 'ending' WHERE call_id = ?", (binding.call_id,))
                self._append_call_state(connection, binding, "ending", "Live call ending", reason=reason)
        ws = self._active_sockets.get(binding.call_id)
        closed = self._session_closed_events.setdefault(binding.call_id, asyncio.Event())
        if ws is not None and not getattr(ws, "closed", True):
            try:
                await ws.send_json({"type": "session.close", "event_id": f"close-{uuid4().hex[:16]}"})
                self.audit.record(
                    binding,
                    "provider.close_requested",
                    initiator=initiator,
                    reason=reason,
                    close_sent=True,
                )
            except Exception as exc:
                self.audit.record(
                    binding,
                    "provider.close_request_failed",
                    initiator=initiator,
                    reason=reason,
                    close_sent=False,
                    **exception_evidence(exc),
                )
            try:
                await asyncio.wait_for(closed.wait(), timeout=self._close_timeout_seconds)
            except TimeoutError:
                self.audit.record(
                    binding,
                    "provider.close_timeout",
                    initiator=initiator,
                    reason=reason,
                    provider_close_state="unconfirmed",
                )
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
        if row["phase"] not in TERMINAL_PHASES:
            self._mark_terminal(binding, "interrupted", provider_close_state="unconfirmed", summary="Live call close was not confirmed")
            with self.session_store._lock, self.session_store._connection() as connection:
                row = connection.execute("SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
        usage = _public_usage(json.loads(row["usage_json"])) if row["usage_json"] else None
        return {"phase": row["phase"], "provider_close_state": row["provider_close_state"],
                "usage": usage, "usage_finalization": "final" if usage else "unavailable"}

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
            segments = self.session_store.live_transcript_segments(
                binding.session_id,
                owner_id=binding.owner_id,
                context_generation=binding.context_generation,
                call_id=binding.call_id,
                call_epoch=binding.call_epoch,
            )

            def timestamp(milliseconds: int) -> str:
                seconds = max(0, int(milliseconds) // 1000)
                return f"{seconds // 60}:{seconds % 60:02d}"

            lines = ["☎ Live call transcript"]
            if segments:
                for segment in segments:
                    marker = "🎙️" if segment["role"] == "user" else "🔊"
                    lines.extend(("", f"[{timestamp(segment['start_ms'])}] {marker} {segment['text']}"))
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

    def _mark_terminal(
        self, binding: CallBinding, phase: str, *, provider_close_state: str,
        summary: str, usage: Mapping[str, Any] | None = None,
    ) -> None:
        public_usage = _public_usage(usage)
        became_terminal = False
        with self.session_store._lock, self.session_store._connection() as connection:
            row = connection.execute(
                "SELECT phase, provider_close_state, usage_json FROM live_calls WHERE call_id = ?",
                (binding.call_id,),
            ).fetchone()
            if row is None:
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
            self._persist_call_record(binding)

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
            async with provider_http_session() as http:
                ws = await attach_provider(http, key=key, provider_session_id=provider_session_id)
                try:
                    await ws.send_json({"type": "session.close", "event_id": f"close-{uuid4().hex[:16]}"})
                    async with asyncio.timeout(self._close_timeout_seconds):
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                event = safe_sideband_event(msg.data)
                                if event and event.get("type") == "session.closed":
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
                        await ws.close()
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

    async def _run_sideband(self, binding: CallBinding, key: str, provider_session_id: str) -> None:
        ready = self._sideband_ready_events.setdefault(binding.call_id, asyncio.Event())
        confirmed_close = False
        self.audit.record(binding, "sideband.attach_started", source="provider")
        try:
            async with provider_http_session() as http:
                ws = await attach_provider(http, key=key, provider_session_id=provider_session_id)
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
                        event = safe_sideband_event(msg.data)
                        if event is None:
                            continue
                        event_type = event.get("type")
                        client_event_id = _provider_client_event_id(event)
                        event_detail: dict[str, Any] = {
                            "provider_event_type": str(event_type or "unknown"),
                            "provider_event_id": str(event.get("event_id") or "")[:160],
                        }
                        if event_type in {
                            "session.input_transcript.delta",
                            "session.output_transcript.delta",
                        }:
                            event_detail.update({
                                "speaker": "user" if event_type == "session.input_transcript.delta" else "assistant",
                                "start_ms": event.get("start_ms"),
                                "end_ms": event.get("end_ms"),
                                "text_bytes": len(str(event.get("delta") or "").encode("utf-8")),
                            })
                        self.audit.record(binding, "provider.event_received", **event_detail)
                        if event_type in {"session.input_audio.muted", "session.input_audio.unmuted"} and client_event_id:
                            waiter = self._control_waiters.get((binding.call_id, client_event_id))
                            if waiter is not None and not waiter.done():
                                waiter.set_result(True)
                        if event_type in {
                            "session.commentary.appended",
                            "session.thinking.appended",
                            "session.instructions.appended",
                        } and client_event_id:
                            waiter = self._update_waiters.get((binding.call_id, client_event_id))
                            if waiter is not None and not waiter.done():
                                waiter.set_result(True)
                        if event_type == "error":
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
                        if event_type == "session.started":
                            self.audit.record(binding, "provider.session_started", source="provider")
                            with self.session_store._lock, self.session_store._connection() as connection:
                                row = connection.execute("SELECT phase FROM live_calls WHERE call_id = ?", (binding.call_id,)).fetchone()
                                if row is not None and row["phase"] == "connecting":
                                    connection.execute("UPDATE live_calls SET phase = 'active' WHERE call_id = ?", (binding.call_id,))
                                    self._append_call_state(connection, binding, "active", "Live call active")
                        elif event_type == "session.closed":
                            confirmed_close = True
                            self.audit.record(
                                binding,
                                "provider.session_closed",
                                provider_reason=_provider_close_reason(event),
                                provider_close_state="confirmed",
                                usage=_event_usage(event),
                            )
                            self._mark_terminal(binding, "ended", provider_close_state="confirmed",
                                summary="Live call ended",
                                usage=_event_usage(event))
                            self._session_closed_events.setdefault(binding.call_id, asyncio.Event()).set()
                            break
                        await self.service.on_provider_event(binding, event)
                finally:
                    self._active_sockets.pop(binding.call_id, None)
                    if not ws.closed:
                        await ws.close()
        except asyncio.CancelledError:
            self.audit.record(binding, "sideband.cancelled", reason="task_cancelled")
            raise
        except Exception as exc:
            self._sideband_failures.setdefault(binding.call_id, "live_sideband_unavailable")
            logger.warning("Live Voice sideband interrupted for %s: %s", binding.call_id, type(exc).__name__)
            self.audit.record(binding, "sideband.exception", **exception_evidence(exc))
        finally:
            ready.set()
            if not confirmed_close and not self._closing:
                self.audit.record(
                    binding,
                    "sideband.disconnected",
                    provider_close_state="unconfirmed",
                )
                self._mark_terminal(binding, "interrupted", provider_close_state="unconfirmed",
                                    summary="Live call sideband disconnected")
                close_state, usage = await self._close_provider_session(
                    key, provider_session_id, binding=binding
                )
                if close_state == "confirmed":
                    self._mark_terminal(
                        binding, "interrupted", provider_close_state="confirmed",
                        summary="Disconnected provider session was closed", usage=usage,
                    )
