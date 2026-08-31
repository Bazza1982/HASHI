"""Fixed-backend protocol and context projection for HER v2."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.her_v2.session_store import (
    HerSessionStore,
    HerSessionStoreError,
)

HER_FIXED_PROTOCOL = "hashi.her-fixed-backend.v1"
HER_FIXED_ENVELOPE_PREFIX = "HASHI_HER_FIXED_ENVELOPE_V1\n"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class HerFixedProtocolError(RuntimeError):
    """Typed fixed-backend protocol error safe to expose as provider metadata."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class AcceptedHerTurn:
    session_id: str
    epoch: int
    turn_id: str
    message_id: str
    state_version: int
    pcm_revision: int
    resource_revision: int
    canonical_sequence: int
    materialized_prompt: str
    transport_chars: int
    duplicate: bool = False
    duplicate_text: str = ""
    resource_attachments: tuple[dict[str, Any], ...] = ()


class HerBackendSessionCoordinator:
    """Own HER's logical thread independently from internal providers."""

    def __init__(self, state_root: Path, *, history_limit: int = 8):
        self.store = HerSessionStore(Path(state_root) / "fixed_sessions.sqlite3")
        self.history_limit = max(1, int(history_limit))

    @staticmethod
    def encode(payload: Mapping[str, Any]) -> str:
        return HER_FIXED_ENVELOPE_PREFIX + _canonical(dict(payload))

    @staticmethod
    def decode(value: str) -> dict[str, Any] | None:
        text = str(value or "")
        if not text.startswith(HER_FIXED_ENVELOPE_PREFIX):
            return None
        try:
            payload = json.loads(text[len(HER_FIXED_ENVELOPE_PREFIX) :])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HerFixedProtocolError(
                "invalid_envelope", "HER fixed-backend envelope is not valid JSON."
            ) from exc
        if not isinstance(payload, Mapping):
            raise HerFixedProtocolError(
                "invalid_envelope", "HER fixed-backend envelope must be an object."
            )
        if str(payload.get("protocol") or "") != HER_FIXED_PROTOCOL:
            raise HerFixedProtocolError(
                "unsupported_protocol",
                "HER fixed-backend protocol version is unsupported.",
            )
        return dict(payload)

    @staticmethod
    def _section_map(
        sections: Sequence[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(sections):
            if not isinstance(raw, Mapping):
                raise HerFixedProtocolError(
                    "invalid_pcm_snapshot", "Every PCM section must be an object."
                )
            key = str(raw.get("key") or "").strip()
            if not key or key == "current_user_request":
                continue
            section = dict(raw)
            section["key"] = key
            section["order"] = int(raw.get("order", index))
            section["text"] = str(raw.get("text") or "")
            section["title"] = str(raw.get("title") or key)
            section["authority"] = str(raw.get("authority") or "runtime_context")
            section["content_sha256"] = hashlib.sha256(
                section["text"].encode("utf-8")
            ).hexdigest()
            result[key] = section
        return result

    @staticmethod
    def _resource_key(resource: Mapping[str, Any]) -> str:
        for field in ("attachment_id", "asset_id", "local_ref", "sha256"):
            value = str(resource.get(field) or "").strip()
            if value:
                return f"{field}:{value}"
        return "digest:" + _digest(dict(resource)).removeprefix("sha256:")

    @classmethod
    def _resource_map(
        cls, resources: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for raw in resources:
            if not isinstance(raw, Mapping):
                raise HerFixedProtocolError(
                    "invalid_resource_snapshot", "Every resource must be an object."
                )
            resource = dict(raw)
            key = cls._resource_key(resource)
            existing = normalized.get(key)
            if existing is not None and existing != resource:
                raise HerFixedProtocolError(
                    "invalid_resource_snapshot",
                    "A resource identity occurs more than once with conflicting content.",
                )
            normalized[key] = resource
        return normalized

    @classmethod
    def _resource_snapshot(
        cls, resources: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        resource_map = cls._resource_map(resources)
        ordered = [resource_map[key] for key in sorted(resource_map)]
        return {"attachments": ordered, "digest": _digest(resource_map)}

    @classmethod
    def _resource_digest(cls, resources: Sequence[Mapping[str, Any]]) -> str:
        return _digest(cls._resource_map(resources))

    def prepare_transport(
        self,
        *,
        session_id: str,
        sections: Sequence[Mapping[str, Any]],
        resources: Sequence[Mapping[str, Any]],
        user_message: str,
        request_id: str,
        message_id: str,
        instance_id: str,
        agent_id: str,
        owner_id: str,
        hashi_conversation_id: str,
        context_generation: int,
        workzone_identity: str,
        removed_section_keys: Sequence[str] = (),
        revoked_resource_ids: Sequence[str] = (),
    ) -> tuple[str, dict[str, Any]]:
        """Build one full open or minimal append envelope from durable ACK state."""

        section_map = self._section_map(sections)
        resource_snapshot = self._resource_snapshot(resources)
        current = self.store.session(session_id)
        binding = {
            "instance_id": str(instance_id),
            "agent_id": str(agent_id).casefold(),
            "owner_id": str(owner_id),
            "hashi_conversation_id": str(hashi_conversation_id),
            "context_generation": max(1, int(context_generation)),
            "workzone_identity": str(workzone_identity),
        }
        turn_id = str(request_id)
        resolved_message_id = str(message_id or request_id)
        idempotency_key = f"{session_id}:{resolved_message_id}"
        if current is None:
            payload = {
                "protocol": HER_FIXED_PROTOCOL,
                "operation": "open_session",
                "her_backend_session_id": session_id,
                "session_epoch": 1,
                "session_binding": binding,
                "pcm_snapshot": {
                    "revision": 1,
                    "digest": _digest(section_map),
                    "sections": section_map,
                },
                "resource_snapshot": {
                    "revision": 1,
                    **resource_snapshot,
                },
                "initial_turn": {
                    "turn_id": turn_id,
                    "request_id": request_id,
                    "message_id": resolved_message_id,
                    "idempotency_key": idempotency_key,
                    "user_message": str(user_message),
                },
            }
            encoded = self.encode(payload)
            return encoded, {
                "operation": "open_session",
                "incremental": False,
                "transport_chars": len(encoded),
                "pcm_sections_sent": len(section_map),
                "pcm_sections_unchanged": 0,
                "pcm_sections_removed": 0,
                "resource_attachments_added": len(resource_snapshot["attachments"]),
                "resource_attachments_revoked": 0,
                "resource_attachments_unchanged": 0,
                "full_pcm_snapshot_count": 1,
                "incremental_turn_count": 0,
                "unexpected_full_snapshot_count_after_open": 0,
                "session_id": session_id,
            }

        existing_pcm = current.get("pcm") or {}
        operations: list[dict[str, Any]] = []
        unchanged = 0
        for key, section in section_map.items():
            existing = existing_pcm.get(key)
            # Every typed field participates in the delta decision.  Text-only
            # equality is insufficient because authority, ordering, protection,
            # and metadata changes alter how the materialised PCM is interpreted.
            if isinstance(existing, Mapping) and _digest(dict(existing)) == _digest(
                section
            ):
                unchanged += 1
                continue
            operations.append({"op": "upsert", "key": key, "section": section})
        # Incremental pre-turn providers may omit an unchanged section
        # intentionally. Absence is therefore "no delta", never revocation.
        # Revocation must arrive through the typed removed_section_keys field.
        removed = sorted(
            key
            for key in {str(item) for item in removed_section_keys if str(item)}
            if key in existing_pcm and key not in section_map
        )
        operations.extend({"op": "remove", "key": key} for key in removed)
        pcm_base = int(current["pcm_revision"])
        resource_base = int(current["resource_revision"])
        target_pcm = {
            str(key): dict(value)
            for key, value in existing_pcm.items()
            if isinstance(value, Mapping)
        }
        for operation in operations:
            key = str(operation["key"])
            if operation["op"] == "remove":
                target_pcm.pop(key, None)
            else:
                target_pcm[key] = dict(operation["section"])

        existing_resources = current.get("resources") or {}
        existing_attachments = list(existing_resources.get("attachments") or [])
        existing_resource_map = self._resource_map(existing_attachments)
        incoming_resource_map = self._resource_map(resources)
        requested_revocations = {
            str(value).strip() for value in revoked_resource_ids if str(value).strip()
        }
        revoked_keys = sorted(
            key
            for key, resource in existing_resource_map.items()
            if key in requested_revocations
            or any(
                str(resource.get(field) or "").strip() in requested_revocations
                for field in ("attachment_id", "asset_id", "local_ref", "sha256")
            )
        )
        additions = [
            dict(resource)
            for key, resource in incoming_resource_map.items()
            if key not in revoked_keys and existing_resource_map.get(key) != resource
        ]
        target_resource_map = dict(existing_resource_map)
        target_resource_map.update(incoming_resource_map)
        for key in revoked_keys:
            target_resource_map.pop(key, None)

        payload = {
            "protocol": HER_FIXED_PROTOCOL,
            "operation": "append_turn",
            "her_backend_session_id": session_id,
            "session_epoch": int(current["epoch"]),
            "expected_state_version": int(current["state_version"]),
            "expected_canonical_sequence": int(current["canonical_sequence"]),
            "session_binding": binding,
            "turn": {
                "turn_id": turn_id,
                "parent_turn_id": str(current.get("last_turn_id") or ""),
                "request_id": request_id,
                "message_id": resolved_message_id,
                "idempotency_key": idempotency_key,
                "user_message": str(user_message),
            },
            "pcm_delta": {
                "base_revision": pcm_base,
                "target_revision": pcm_base + 1,
                "operations": operations,
                "target_digest": _digest(target_pcm),
            },
            "resource_delta": {
                "base_revision": resource_base,
                "target_revision": resource_base + 1,
                "attachments_added": additions,
                "attachments_revoked": revoked_keys,
                "permissions_changed": [],
                "media_grants_changed": [],
                "target_digest": _digest(target_resource_map),
            },
        }
        encoded = self.encode(payload)
        return encoded, {
            "operation": "append_turn",
            "incremental": True,
            "transport_chars": len(encoded),
            "pcm_sections_sent": len(operations),
            "pcm_sections_unchanged": unchanged,
            "pcm_sections_removed": len(removed),
            "resource_attachments_added": len(additions),
            "resource_attachments_revoked": len(revoked_keys),
            "resource_attachments_unchanged": sum(
                1
                for key, resource in incoming_resource_map.items()
                if existing_resource_map.get(key) == resource
            ),
            "full_pcm_snapshot_count": 0,
            "incremental_turn_count": 1,
            "unexpected_full_snapshot_count_after_open": 0,
            "session_id": session_id,
        }

    @staticmethod
    def _binding(payload: Mapping[str, Any]) -> dict[str, Any]:
        binding = payload.get("session_binding")
        if not isinstance(binding, Mapping):
            raise HerFixedProtocolError(
                "invalid_envelope", "HER fixed-backend binding is missing."
            )
        return {
            "instance_id": str(binding.get("instance_id") or ""),
            "agent_id": str(binding.get("agent_id") or ""),
            "owner_id": str(binding.get("owner_id") or ""),
            "hashi_conversation_id": str(binding.get("hashi_conversation_id") or ""),
            "context_generation": int(binding.get("context_generation") or 1),
            "workzone_identity": str(binding.get("workzone_identity") or ""),
        }

    def accept(self, encoded: str) -> AcceptedHerTurn:
        payload = self.decode(encoded)
        if payload is None:
            raise HerFixedProtocolError(
                "invalid_envelope", "HER fixed-backend envelope is missing."
            )
        operation = str(payload.get("operation") or "")
        session_id = str(payload.get("her_backend_session_id") or "")
        if not session_id:
            raise HerFixedProtocolError(
                "invalid_envelope", "HER session ID is missing."
            )
        binding = self._binding(payload)
        try:
            if operation == "open_session":
                pcm_snapshot = payload.get("pcm_snapshot")
                resource_snapshot = payload.get("resource_snapshot")
                turn = payload.get("initial_turn")
                if not all(
                    isinstance(value, Mapping)
                    for value in (pcm_snapshot, resource_snapshot, turn)
                ):
                    raise HerFixedProtocolError(
                        "invalid_envelope", "HER open_session snapshot is incomplete."
                    )
                pcm_sections = dict(pcm_snapshot.get("sections") or {})
                if str(pcm_snapshot.get("digest") or "") != _digest(pcm_sections):
                    raise HerFixedProtocolError(
                        "pcm_digest_conflict", "HER PCM snapshot digest does not match."
                    )
                resource_attachments = list(resource_snapshot.get("attachments") or [])
                if str(resource_snapshot.get("digest") or "") != _digest(
                    self._resource_map(resource_attachments)
                ):
                    raise HerFixedProtocolError(
                        "resource_digest_conflict",
                        "HER resource snapshot digest does not match.",
                    )
                accepted = self.store.open_session(
                    session_id=session_id,
                    epoch=int(payload.get("session_epoch") or 1),
                    pcm_revision=int(pcm_snapshot.get("revision") or 1),
                    pcm=pcm_sections,
                    resource_revision=int(resource_snapshot.get("revision") or 1),
                    resources={
                        "attachments": resource_attachments,
                        "digest": str(resource_snapshot.get("digest") or ""),
                    },
                    turn_id=str(turn.get("turn_id") or ""),
                    request_id=str(turn.get("request_id") or ""),
                    message_id=str(turn.get("message_id") or ""),
                    idempotency_key=str(turn.get("idempotency_key") or ""),
                    user_message=str(turn.get("user_message") or ""),
                    **binding,
                )
            elif operation == "append_turn":
                pcm_delta = payload.get("pcm_delta")
                resource_delta = payload.get("resource_delta")
                turn = payload.get("turn")
                if not all(
                    isinstance(value, Mapping)
                    for value in (pcm_delta, resource_delta, turn)
                ):
                    raise HerFixedProtocolError(
                        "invalid_envelope", "HER append_turn delta is incomplete."
                    )
                operations = pcm_delta.get("operations") or []
                if not isinstance(operations, list):
                    raise HerFixedProtocolError(
                        "invalid_pcm_delta", "HER PCM operations must be an array."
                    )
                accepted = self.store.append_turn(
                    session_id=session_id,
                    epoch=int(payload.get("session_epoch") or 0),
                    expected_state_version=int(
                        payload.get("expected_state_version") or 0
                    ),
                    expected_canonical_sequence=int(
                        payload.get("expected_canonical_sequence") or 0
                    ),
                    pcm_base_revision=int(pcm_delta.get("base_revision") or 0),
                    pcm_target_revision=int(pcm_delta.get("target_revision") or 0),
                    pcm_operations=[dict(item) for item in operations],
                    pcm_target_digest=str(pcm_delta.get("target_digest") or ""),
                    resource_base_revision=int(
                        resource_delta.get("base_revision") or 0
                    ),
                    resource_target_revision=int(
                        resource_delta.get("target_revision") or 0
                    ),
                    resource_additions=[
                        dict(item)
                        for item in list(resource_delta.get("attachments_added") or [])
                    ],
                    resource_revocations=[
                        str(item)
                        for item in list(
                            resource_delta.get("attachments_revoked") or []
                        )
                    ],
                    resource_target_digest=str(
                        resource_delta.get("target_digest") or ""
                    ),
                    turn_id=str(turn.get("turn_id") or ""),
                    request_id=str(turn.get("request_id") or ""),
                    message_id=str(turn.get("message_id") or ""),
                    idempotency_key=str(turn.get("idempotency_key") or ""),
                    user_message=str(turn.get("user_message") or ""),
                    **binding,
                )
            else:
                raise HerFixedProtocolError(
                    "unsupported_operation",
                    f"Unsupported HER operation: {operation!r}.",
                )
        except HerSessionStoreError as exc:
            raise HerFixedProtocolError(exc.code, str(exc)) from exc

        session = accepted["session"]
        turn = accepted["turn"]
        duplicate = bool(accepted.get("duplicate"))
        materialized = ""
        if not duplicate or str(turn.get("status") or "") == "active":
            materialized = self._render_prompt(
                session.get("pcm") or {},
                session.get("resources") or {},
                str(turn.get("user_message") or ""),
                self.store.recent_completed_turns(session_id, limit=self.history_limit),
            )
        return AcceptedHerTurn(
            session_id=session_id,
            epoch=int(session["epoch"]),
            turn_id=str(turn["turn_id"]),
            message_id=str(turn["message_id"]),
            state_version=int(session["state_version"]),
            pcm_revision=int(session["pcm_revision"]),
            resource_revision=int(session["resource_revision"]),
            canonical_sequence=int(
                accepted.get("canonical_sequence")
                or session.get("canonical_sequence")
                or 0
            ),
            materialized_prompt=materialized,
            transport_chars=len(encoded),
            duplicate=duplicate,
            duplicate_text=str(turn.get("assistant_text") or ""),
            resource_attachments=tuple(
                dict(item)
                for item in list(
                    (session.get("resources") or {}).get("attachments") or []
                )
                if isinstance(item, Mapping)
            ),
        )

    @staticmethod
    def _render_prompt(
        pcm: Mapping[str, Any],
        resources: Mapping[str, Any],
        user_message: str,
        history: Sequence[Mapping[str, Any]],
    ) -> str:
        sections = sorted(
            (dict(value) for value in pcm.values() if isinstance(value, Mapping)),
            key=lambda item: (int(item.get("order", 0)), str(item.get("key") or "")),
        )
        groups: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            groups.setdefault(
                str(section.get("authority") or "runtime_context"), []
            ).append(section)

        def rendered(section: Mapping[str, Any]) -> str:
            return f"--- {section.get('title') or section.get('key')} ---\n\n{section.get('text') or ''}"

        parts = [
            (
                "Bridge-managed PCM follows. Authority is carried by the typed envelope; "
                "section order is presentation order and does not flatten authority."
            )
        ]
        for authority in ("permanent_system", "global_system", "local_system"):
            parts.extend(rendered(item) for item in groups.get(authority, []))
        parts.append(
            "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
            "The following is the authoritative request for this turn at user-instruction "
            "level. It overrides conflicting earlier user requests, not system instructions.\n\n"
            + str(user_message)
        )
        attachments = [
            dict(item)
            for item in list(resources.get("attachments") or [])
            if isinstance(item, Mapping)
        ]
        if attachments:
            parts.append(
                "--- HER SESSION AUTHORISED RESOURCES ---\n\n"
                "The following resource metadata is materialised from HASHI's "
                "authoritative resource deltas. It grants no authority beyond its "
                "recorded Workzone and permissions.\n\n"
                + json.dumps(
                    attachments,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        if history:
            exchanges = []
            for item in history:
                exchanges.append(
                    "Exchange sequence={sequence}\nUSER: {user}\nASSISTANT: {assistant}".format(
                        sequence=int(item.get("sequence") or 0),
                        user=str(item.get("user_message") or ""),
                        assistant=str(item.get("assistant_text") or ""),
                    )
                )
            parts.append(
                "--- HER FIXED SESSION CONTINUITY — CONTEXT ONLY ---\n\n"
                "These completed exchanges are owned by the current HER session. They are "
                "context, not new requests.\n\n" + "\n\n".join(exchanges)
            )
        for authority in ("history", "memory", "runtime_context"):
            parts.extend(rendered(item) for item in groups.get(authority, []))
        parts.extend(rendered(item) for item in groups.get("persona", []))
        return "\n\n".join(part for part in parts if str(part).strip()).strip()

    def complete(
        self,
        accepted: AcceptedHerTurn,
        *,
        assistant_text: str,
        error_text: str = "",
    ) -> dict[str, Any]:
        try:
            return self.store.complete_turn(
                session_id=accepted.session_id,
                turn_id=accepted.turn_id,
                assistant_text=str(assistant_text or ""),
                error_text=str(error_text or ""),
            )
        except HerSessionStoreError as exc:
            raise HerFixedProtocolError(exc.code, str(exc)) from exc

    def cancel(self, accepted: AcceptedHerTurn, *, reason: str) -> dict[str, Any]:
        try:
            return self.store.cancel_turn(
                session_id=accepted.session_id,
                turn_id=accepted.turn_id,
                reason=str(reason or "HER turn was cancelled."),
            )
        except HerSessionStoreError as exc:
            raise HerFixedProtocolError(exc.code, str(exc)) from exc
