"""PCM synchronization of durable Phone events across Engine connections.

PAO freezes action scope; PCM projects the quoted, role preserving transcript.
Provider-native fixed history is not evidence of having received Phone events.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

FRAGMENT_ENCODING = "hashi.phone-fragments.grouped.v1"
EVENT_COLUMNS = ("provider_event_id", "sequence", "start_ms", "end_ms")
LEGACY_SECTION_PREFIX = "phone_external_context:"
SEMANTIC_SECTION_PREFIX = "recent_exchange:phone:"
SEMANTIC_CONSUMER_VERSION = "phone-semantic-history-v4"
ACTION_OWNER_DIGEST_FIELD = "phone_action_owner_sha256"
PHONE_SOURCE_LAST_SEQUENCE = "phone_source_last_sequence"
LEGACY_SNAPSHOT_CANDIDATE_LIMIT = 100_000


class PhoneContextError(RuntimeError):
    def __init__(self, code: str, *, source: str = ""):
        self.code, self.source = code, source
        super().__init__(f"{code}: durable Phone context remains in {source or 'SessionStore'}; context was not truncated")


def initialize_schema(connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS phone_context_handoffs (
            handoff_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
            context_generation INTEGER NOT NULL, call_id TEXT NOT NULL,
            call_epoch INTEGER NOT NULL, delegation_id TEXT NOT NULL,
            proposal_digest TEXT NOT NULL, payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id),
            FOREIGN KEY(call_id) REFERENCES live_calls(call_id)
        );
        CREATE TABLE IF NOT EXISTS phone_context_consumption (
            owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
            context_generation INTEGER NOT NULL, consumer_key TEXT NOT NULL,
            call_id TEXT NOT NULL, call_epoch INTEGER NOT NULL,
            provider_event_id TEXT NOT NULL, consumed_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, agent_id, session_id, context_generation,
                consumer_key, call_id, call_epoch, provider_event_id),
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );
    """)


def _scope(store, *, owner_id: str, agent_id: str, session_id: str,
           context_generation: int) -> dict[str, Any]:
    session = store.get_session(session_id, owner_id=owner_id, agent_id=agent_id)
    if int(session["context_generation"]) != int(context_generation):
        raise PhoneContextError("phone_context_scope_changed", source=session_id)
    return session


def _conversation_at(started_at: Any, start_ms: int) -> str:
    try:
        parsed = datetime.fromisoformat(
            str(started_at or "").strip().replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        + timedelta(milliseconds=max(0, int(start_ms)))
    ).isoformat()


def _fragments(connection, *, owner_id: str, agent_id: str, session_id: str,
               context_generation: int, call_id: str | None = None,
               call_epoch: int | None = None, cutoff_ms: int | None = None,
               ended_only: bool = False, consumer_key: str | None = None,
               before_call_epoch: int | None = None,
               unconsumed_only: bool = False,
               exclude_call_scope: tuple[str, int] | None = None,
               ) -> list[dict[str, Any]]:
    clauses = ["f.owner_id=?", "c.owner_id=?", "f.session_id=?", "c.session_id=?",
        "c.agent_id=?", "c.context_generation=?"]
    params: list[Any] = [owner_id, owner_id, session_id, session_id, agent_id, int(context_generation)]
    if call_id is not None: clauses.append("f.call_id=?"); params.append(call_id)
    if call_epoch is not None: clauses.append("f.call_epoch=?"); params.append(int(call_epoch))
    if before_call_epoch is not None: clauses.append("f.call_epoch<?"); params.append(int(before_call_epoch))
    if ended_only: clauses.append("c.ended_at IS NOT NULL")
    if cutoff_ms is not None: clauses.append("f.start_ms < ?"); params.append(int(cutoff_ms))
    if exclude_call_scope is not None:
        # A frozen action covers every epoch of its logical call.
        clauses.append("f.call_id<>?")
        params.append(str(exclude_call_scope[0]))
    if unconsumed_only:
        if not consumer_key:
            raise PhoneContextError("phone_context_consumer_unavailable", source=session_id)
        clauses.append("""NOT EXISTS (
            SELECT 1 FROM phone_context_consumption AS pc
            WHERE pc.owner_id=f.owner_id AND pc.agent_id=c.agent_id
            AND pc.session_id=f.session_id AND pc.context_generation=c.context_generation
            AND pc.consumer_key=? AND pc.call_id=f.call_id
            AND pc.call_epoch=f.call_epoch
            AND pc.provider_event_id=f.provider_event_id
        )""")
        params.append(str(consumer_key))
    rows = connection.execute(f"""SELECT f.*, c.started_at, e.detail_json
        FROM live_fragments AS f JOIN live_calls AS c ON c.call_id=f.call_id
        JOIN run_events AS e ON e.event_id=f.event_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.started_at,f.call_epoch,f.start_ms,f.end_ms,f.sequence,f.provider_event_id""", params).fetchall()
    from orchestrator.session_store import _conversation_timestamp, _live_epoch_starts
    epoch_starts = _live_epoch_starts(
        connection, session_id,
        {str(row["call_id"]) for row in rows if int(row["call_epoch"]) > 1},
    )
    fragments=[]
    for row in rows:
        if cutoff_ms is not None and int(row["end_ms"])>cutoff_ms:
            raise PhoneContextError("phone_context_cutoff_unconfirmed", source=str(row["call_id"]))
        try: detail=json.loads(row["detail_json"])
        except (ValueError, TypeError): raise PhoneContextError("phone_context_invalid_fragment", source=str(row["event_id"])) from None
        if row["speaker"] not in {"user","assistant"} or not isinstance(detail.get("text"),str):
            raise PhoneContextError("phone_context_invalid_fragment", source=str(row["event_id"]))
        fragments.append({"call_id":str(row["call_id"]),"call_epoch":int(row["call_epoch"]),
            "provider_event_id":str(row["provider_event_id"]),"sequence":int(row["sequence"]),
            "role":str(row["speaker"]),"text":detail["text"],
            "start_ms":int(row["start_ms"]),"end_ms":int(row["end_ms"]),
            "conversation_at":_conversation_timestamp(
                row["started_at"] if int(row["call_epoch"]) == 1
                else epoch_starts.get((str(row["call_id"]), int(row["call_epoch"]))),
                int(row["start_ms"]), fallback=row["created_at"],
            ),
            "source":"durable_phone_transcript"})
    return fragments


def _pending(connection, *, owner_id: str, session_id: str,
             call_id: str, call_epoch: int | None = None,
             before_call_epoch: int | None = None,
             cutoff_ms: int | None = None) -> bool:
    clauses=["owner_id=?","session_id=?","call_id=?"]
    params:list[Any]=[owner_id,session_id,call_id]
    if call_epoch is not None: clauses.append("call_epoch=?"); params.append(int(call_epoch))
    if before_call_epoch is not None: clauses.append("call_epoch<?"); params.append(int(before_call_epoch))
    if cutoff_ms is not None: clauses.append("start_ms < ?"); params.append(int(cutoff_ms))
    for table in ("live_provider_fragment_inbox","live_provider_event_inbox"):
        extra=" AND event_type='transcript'" if table.endswith("event_inbox") else ""
        if connection.execute(f"SELECT 1 FROM {table} WHERE {' AND '.join(clauses)}{extra} LIMIT 1",params).fetchone(): return True
    return False


def _packed_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove repeated labels, never transcript content or event boundaries."""
    groups: list[dict[str, Any]] = []
    for fragment in payload["fragments"]:
        scope = {key: fragment[key] for key in ("call_id", "call_epoch", "source")}
        if not groups or any(groups[-1][key] != value for key, value in scope.items()):
            groups.append({**scope, "fragments": []})
        groups[-1]["fragments"].append({"role": fragment["role"], "text": fragment["text"],
                                     "event": [fragment[key] for key in EVENT_COLUMNS]})
    return {**{key: value for key, value in payload.items() if key != "fragments"},
            "fragment_encoding": FRAGMENT_ENCODING, "event_columns": list(EVENT_COLUMNS),
            "fragment_groups": groups}


def _expanded_payload(
    payload: dict[str, Any], *, source: str, identities_only: bool = False,
) -> dict[str, Any]:
    if "fragment_encoding" not in payload:
        return payload  # Existing durable snapshots keep their original shape.
    columns = payload.get("event_columns")
    if payload.get("fragment_encoding") != FRAGMENT_ENCODING:
        raise PhoneContextError("phone_context_invalid_fragment", source=source)
    if identities_only:
        if not isinstance(columns, list) or columns.count("provider_event_id") != 1:
            raise PhoneContextError("phone_context_invalid_fragment", source=source)
        identity_index = columns.index("provider_event_id")
    elif columns != list(EVENT_COLUMNS):
        raise PhoneContextError("phone_context_invalid_fragment", source=source)
    try:
        fragments = []
        for group in payload["fragment_groups"]:
            if identities_only:
                scope = {key: group[key] for key in ("call_id", "call_epoch")}
                for fragment in group["fragments"]:
                    event = fragment["event"]
                    if not isinstance(event, list) or len(event) <= identity_index:
                        raise ValueError("missing event identity")
                    fragments.append({**scope, "provider_event_id": event[identity_index]})
                continue
            scope = {key: group[key] for key in ("call_id", "call_epoch", "source")}
            for fragment in group["fragments"]:
                event = fragment["event"]
                if len(event) != len(EVENT_COLUMNS):
                    raise ValueError("invalid event columns")
                fragments.append({**scope, "role": fragment["role"], "text": fragment["text"],
                                  **dict(zip(EVENT_COLUMNS, event))})
        return {**{key: value for key, value in payload.items()
                   if key not in {"fragment_encoding", "event_columns", "fragment_groups"}},
                "fragments": fragments}
    except (KeyError, TypeError, ValueError):
        raise PhoneContextError("phone_context_invalid_fragment", source=source) from None


def _encode_payload(payload: dict[str, Any]) -> str:
    return json.dumps(_packed_payload(payload),ensure_ascii=False,separators=(",",":"))


def freeze_action_handoff(store, binding, proposal) -> str:
    _scope(store,owner_id=binding.owner_id,agent_id=binding.agent_id,
        session_id=binding.session_id,context_generation=binding.context_generation)
    material={"owner_id":binding.owner_id,**binding.public_scope(),
        "delegation_id":proposal.delegation_id,"proposal_version":proposal.version,
        "proposal_digest":proposal.digest,"cutoff_ms":proposal.cutoff_ms}
    key="phone-handoff-"+hashlib.sha256(json.dumps(material,sort_keys=True).encode()).hexdigest()
    with store._lock,store._connection() as connection:
        existing=connection.execute("SELECT 1 FROM phone_context_handoffs WHERE handoff_id=?",(key,)).fetchone()
        if existing: return key
        call=connection.execute("SELECT * FROM live_calls WHERE call_id=?",(binding.call_id,)).fetchone()
        if call is None or any(str(call[name])!=str(getattr(binding,name)) for name in
            ("owner_id","agent_id","session_id","context_generation","instance_id","instance_generation","call_epoch","provider_session_id")):
            raise PhoneContextError("phone_context_scope_changed",source=binding.call_id)
        if (
            _pending(connection,owner_id=binding.owner_id,session_id=binding.session_id,
                call_id=binding.call_id,call_epoch=binding.call_epoch,cutoff_ms=proposal.cutoff_ms)
            or _pending(connection,owner_id=binding.owner_id,session_id=binding.session_id,
                call_id=binding.call_id,before_call_epoch=binding.call_epoch)
        ):
            raise PhoneContextError("phone_context_transcript_pending",source=binding.call_id)
        current_fragments=_fragments(connection,owner_id=binding.owner_id,agent_id=binding.agent_id,
            session_id=binding.session_id,context_generation=binding.context_generation,
            call_id=binding.call_id,call_epoch=binding.call_epoch,cutoff_ms=proposal.cutoff_ms)
        if not current_fragments or not set(proposal.source_event_ids).issubset(
            {r["provider_event_id"] for r in current_fragments}
        ):
            raise PhoneContextError("phone_context_transcript_unconfirmed",source=binding.call_id)
        prior_fragments=_fragments(connection,owner_id=binding.owner_id,agent_id=binding.agent_id,
            session_id=binding.session_id,context_generation=binding.context_generation,
            call_id=binding.call_id,before_call_epoch=binding.call_epoch)
        fragments=[*prior_fragments,*current_fragments]
        payload={**material,"handoff_id":key,"fragments":fragments}
        encoded=_encode_payload(payload)
        connection.execute("""INSERT INTO phone_context_handoffs VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (key,binding.owner_id,binding.agent_id,binding.session_id,binding.context_generation,
             binding.call_id,binding.call_epoch,proposal.delegation_id,proposal.digest,encoded,
             datetime.now(timezone.utc).isoformat()))
    return key


def load_handoff(store, handoff_id: str, *, owner_id: str, agent_id: str,
                 session_id: str, context_generation: int) -> dict[str, Any]:
    _scope(store,owner_id=owner_id,agent_id=agent_id,session_id=session_id,context_generation=context_generation)
    with store._lock,store._connection() as connection:
        row=connection.execute("""SELECT payload_json FROM phone_context_handoffs
            WHERE handoff_id=? AND owner_id=? AND agent_id=? AND session_id=? AND context_generation=?""",
            (handoff_id,owner_id,agent_id,session_id,int(context_generation))).fetchone()
    if row is None: raise PhoneContextError("phone_context_handoff_unavailable",source=session_id)
    payload=_expanded_payload(json.loads(row[0]),source=handoff_id)
    return payload


def _consumer(backend_id: str, backend: Any) -> str:
    native_id=str(getattr(backend,"_session_id","") or "")
    parts = [backend_id, native_id]
    if callable(getattr(backend, "current_pcm_section_keys", None)):
        # Typed PCM can revoke the deprecated raw Phone sections and replay
        # their durable source once in the semantic representation.
        parts.append(SEMANTIC_CONSUMER_VERSION)
    material = "\0".join(parts)
    return hashlib.sha256(material.encode()).hexdigest()


def _fixed_pcm_section_keys(backend: Any) -> tuple[str, ...]:
    """Return exact keys from typed fixed PCM for safe, explicit tombstones."""
    resolver = getattr(backend, "current_pcm_section_keys", None)
    if not callable(resolver):
        return ()
    try:
        keys = resolver()
    except Exception as exc:
        raise PhoneContextError(
            "phone_context_legacy_state_unavailable",
            source=str(getattr(backend, "_session_id", "") or "fixed-session"),
        ) from exc
    if not isinstance(keys, (list, tuple, set, frozenset)):
        raise PhoneContextError(
            "phone_context_legacy_state_unavailable",
            source=str(getattr(backend, "_session_id", "") or "fixed-session"),
        )
    return tuple(sorted({str(key) for key in keys if isinstance(key, str) and key}))


def _fixed_pcm_sections(
    backend: Any, section_keys: list[str]
) -> dict[str, dict[str, Any]]:
    resolver = getattr(backend, "current_pcm_sections", None)
    if not callable(resolver):
        raise PhoneContextError(
            "phone_context_legacy_state_unavailable",
            source=str(getattr(backend, "_session_id", "") or "fixed-session"),
        )
    try:
        raw_sections = resolver(tuple(section_keys))
    except Exception as exc:
        raise PhoneContextError(
            "phone_context_legacy_state_unavailable",
            source=str(getattr(backend, "_session_id", "") or "fixed-session"),
        ) from exc
    if not isinstance(raw_sections, Mapping):
        raise PhoneContextError(
            "phone_context_legacy_state_unavailable",
            source=str(getattr(backend, "_session_id", "") or "fixed-session"),
        )
    sections: dict[str, dict[str, Any]] = {}
    for key, value in raw_sections.items():
        if not isinstance(key, str) or not key or not isinstance(value, Mapping):
            raise PhoneContextError(
                "phone_context_legacy_state_unavailable",
                source=str(getattr(backend, "_session_id", "") or "fixed-session"),
            )
        sections[key] = dict(value)
    return sections


def _fragment_identity(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row["call_id"]),
        int(row["call_epoch"]),
        str(row["provider_event_id"]),
    )


def _fragment_digest(fragments: list[Mapping[str, Any]]) -> str:
    identity = [_fragment_identity(row) for row in fragments]
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def _legacy_payload(section: Mapping[str, Any], *, section_key: str) -> dict[str, Any]:
    text = section.get("text")
    if not isinstance(text, str):
        raise PhoneContextError(
            "phone_context_legacy_source_incomplete", source=section_key
        )
    marker = text.find("\n{")
    encoded = text[marker + 1 :] if marker >= 0 else text.strip()
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        raise PhoneContextError(
            "phone_context_legacy_source_incomplete", source=section_key
        ) from None
    if not isinstance(payload, Mapping):
        raise PhoneContextError(
            "phone_context_legacy_source_incomplete", source=section_key
        )
    try:
        payload = _expanded_payload(
            dict(payload), source=section_key, identities_only=True,
        )
    except PhoneContextError:
        raise PhoneContextError(
            "phone_context_legacy_source_incomplete", source=section_key
        ) from None
    if not isinstance(payload.get("fragments"), list):
        raise PhoneContextError(
            "phone_context_legacy_source_incomplete", source=section_key
        )
    return payload


def _validate_legacy_sources(
    sections: Mapping[str, Mapping[str, Any]],
    legacy_section_keys: list[str],
    replacement_fragments: list[dict[str, Any]],
    *,
    session_id: str,
    agent_id: str,
    context_generation: int,
) -> None:
    if not legacy_section_keys:
        return
    available = {_fragment_identity(row) for row in replacement_fragments}
    for section_key in legacy_section_keys:
        section = sections.get(section_key)
        if section is None:
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            )
        payload = _legacy_payload(section, section_key=section_key)
        scope = payload.get("scope")
        try:
            scope_generation = (
                int(scope.get("context_generation") or 0)
                if isinstance(scope, Mapping) else 0
            )
        except (TypeError, ValueError):
            scope_generation = 0
        if (
            not isinstance(scope, Mapping)
            or str(scope.get("session_id") or "") != session_id
            or str(scope.get("agent_id") or "") != agent_id
            or scope_generation != int(context_generation)
        ):
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            )
        fragments = [row for row in payload["fragments"] if isinstance(row, Mapping)]
        if len(fragments) != len(payload["fragments"]) or not fragments:
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            )
        try:
            identities = [_fragment_identity(row) for row in fragments]
            expected_key = LEGACY_SECTION_PREFIX + _fragment_digest(fragments)
        except (KeyError, TypeError, ValueError):
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            ) from None
        if expected_key != section_key:
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            )
        if len(identities) != len(set(identities)):
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            )
        # The scoped durable events own the transcript. The legacy PCM section
        # is only a projection and its text, timestamps or hash can differ.
        if any(identity not in available for identity in identities):
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            )


def _identity_interval_matches(
    units: list[list[dict[str, Any]]],
    target_digest: str,
    encoded_identities: dict[int, bytes],
    *,
    first_arrival: int | None = None,
    last_arrival: int | None = None,
    candidate_budget: list[int] | None = None,
) -> bool | None:
    # A candidate snapshot must include both of its arrival extrema.  Locate
    # their units first instead of hashing every interval that cannot contain
    # them.  Repeated sequence values are handled as separate possible anchors.
    from bisect import bisect_left

    target_bytes = bytes.fromhex(target_digest)
    unit_bytes: list[bytes] = []
    first_units: list[int] = []
    last_units: list[int] = []
    for index, unit in enumerate(units):
        unit_bytes.append(b",".join(encoded_identities[id(row)] for row in unit))
        sequences = {int(row["sequence"]) for row in unit}
        if first_arrival is not None and first_arrival in sequences:
            first_units.append(index)
        if last_arrival is not None and last_arrival in sequences:
            last_units.append(index)

    if first_arrival is not None and not first_units:
        return False
    if last_arrival is not None and not last_units:
        return False

    last_lower = len(units) - 1
    if first_arrival is not None:
        last_lower = min(last_lower, first_units[-1])
    if last_arrival is not None:
        last_lower = min(last_lower, last_units[-1])

    for lower in range(last_lower + 1):
        first_index = bisect_left(first_units, lower)
        last_index = bisect_left(last_units, lower)
        if first_arrival is not None and first_index == len(first_units):
            continue
        if last_arrival is not None and last_index == len(last_units):
            continue
        earliest_end = lower
        if first_arrival is not None:
            earliest_end = max(earliest_end, first_units[first_index])
        if last_arrival is not None:
            earliest_end = max(earliest_end, last_units[last_index])

        digest = hashlib.sha256(b"[" + b",".join(unit_bytes[lower:earliest_end + 1]))
        for upper in range(earliest_end, len(units)):
            if candidate_budget is not None:
                if candidate_budget[0] <= 0:
                    return None
                candidate_budget[0] -= 1
            if upper != earliest_end:
                digest.update(b",")
                digest.update(unit_bytes[upper])
            candidate = digest.copy()
            candidate.update(b"]")
            if candidate.digest()[:12] == target_bytes:
                return True
    return False


def _semantic_source_matches(
    section_key: str,
    section: Mapping[str, Any],
    replacement_fragments: list[dict[str, Any]],
    *,
    candidate_budget: list[int] | None = None,
) -> bool | None:
    target_digest = section_key.removeprefix(SEMANTIC_SECTION_PREFIX)
    if len(target_digest) != 24 or any(
        character not in "0123456789abcdef" for character in target_digest
    ):
        return False
    if not replacement_fragments:
        return False

    # Most sections already correspond to one dialogue unit (or to the entire
    # source). Their identity is enough; the stored display text is not an
    # authority over the durable Phone events.
    if _fragment_digest(replacement_fragments) == target_digest:
        return True
    units = _history_units(replacement_fragments)
    if any(
        _fragment_digest(unit) == target_digest
        for unit in units
    ):
        return True

    encoded_identities = {
        id(row): json.dumps(_fragment_identity(row), separators=(",", ":")).encode()
        for row in replacement_fragments
    }
    metadata = section.get("metadata")
    if isinstance(metadata, Mapping) and PHONE_SOURCE_LAST_SEQUENCE in metadata:
        # New sections record both arrival bounds. Verify one snapshot exactly,
        # without searching possible high-water marks or trusting rendered text.
        try:
            if isinstance(metadata["sequence"], bool) or isinstance(metadata[PHONE_SOURCE_LAST_SEQUENCE], bool):
                return False
            first = int(metadata["sequence"])
            last = int(metadata[PHONE_SOURCE_LAST_SEQUENCE])
            if first > last:
                return False
            eligible = [row for row in replacement_fragments
                        if first <= int(row["sequence"]) <= last]
            return _identity_interval_matches(
                _history_units(eligible), target_digest, encoded_identities,
                first_arrival=first, last_arrival=last,
            )
        except (KeyError, TypeError, ValueError):
            return False

    # A stale or missing legacy projection sequence is only a lost search hint;
    # a matching interval of scoped identities is still authoritative.
    if _identity_interval_matches(units, target_digest, encoded_identities):
        return True

    if not isinstance(metadata, Mapping):
        return False
    try:
        first_arrival = int(metadata["sequence"])
        arrivals = sorted({
            int(row["sequence"])
            for row in replacement_fragments
            if int(row["sequence"]) >= first_arrival
        })
    except (KeyError, TypeError, ValueError):
        return False
    if not arrivals or first_arrival not in arrivals:
        return False

    # Whole snapshots were emitted by earlier consumers. Check them before
    # reconstructing intervals, newest first; this verifies the same exact
    # identity digest without scanning millions of impossible subintervals.
    target_bytes = bytes.fromhex(target_digest)
    for last_arrival in reversed(arrivals):
        identities = (
            encoded_identities[id(row)]
            for row in replacement_fragments
            if first_arrival <= int(row["sequence"]) <= last_arrival
        )
        candidate = hashlib.sha256(b"[" + b",".join(identities) + b"]").digest()
        if candidate[:12] == target_bytes:
            return True

    # Old v2 sections were snapshots of every unconsumed Phone event from their
    # first arrival through one durable high-water mark.  Arrival sequence only
    # bounds membership; it is not transcript order, and calls that were still
    # active at that high-water may now appear between completed history units.
    # Rebuild aligned dialogue-unit intervals using only event identities.
    # Text is a derived projection and can differ after formatting or correction.
    if candidate_budget is None:
        candidate_budget = [LEGACY_SNAPSHOT_CANDIDATE_LIMIT]
    for last_arrival in arrivals:
        eligible = [
            row
            for row in replacement_fragments
            if first_arrival <= int(row["sequence"]) <= last_arrival
        ]
        units = _history_units(eligible)
        if len(units) <= 1:
            # This entire snapshot was already checked above.
            continue
        matched = _identity_interval_matches(
            units, target_digest, encoded_identities,
            first_arrival=first_arrival, last_arrival=last_arrival,
            candidate_budget=candidate_budget,
        )
        if matched is not False:
            # None means the search stopped, not that its source is missing.
            return matched
    return False


def _validate_semantic_sources(
    sections: Mapping[str, Mapping[str, Any]],
    semantic_section_keys: list[str],
    replacement_fragments: list[dict[str, Any]],
) -> list[str]:
    retained: list[str] = []
    candidate_budget = [LEGACY_SNAPSHOT_CANDIDATE_LIMIT]
    for section_key in semantic_section_keys:
        section = sections.get(section_key)
        matched = (
            _semantic_source_matches(
                section_key, section, replacement_fragments,
                candidate_budget=candidate_budget,
            )
            if section is not None else False
        )
        if matched is None:
            # Keep the existing PCM section; incomplete search is not a
            # verified reason to delete history or reject an ordinary turn.
            retained.append(section_key)
        elif not matched:
            raise PhoneContextError(
                "phone_context_legacy_source_incomplete", source=section_key
            )
    return retained


def _semantic_transcript(fragments: list[dict[str, Any]]) -> str:
    segments: list[dict[str, Any]] = []
    for fragment in fragments:
        role = str(fragment["role"])
        call_key = (str(fragment["call_id"]), int(fragment["call_epoch"]))
        text = str(fragment["text"])
        if segments and segments[-1]["role"] == role and segments[-1]["call_key"] == call_key:
            segments[-1]["text"] += text
        else:
            segments.append({"role": role, "call_key": call_key, "text": text})
    return "\n".join(
        f"{'USER' if segment['role'] == 'user' else 'ASSISTANT'}: {segment['text']}"
        for segment in segments
    )


def _ordinary_history_section(fragments: list[dict[str, Any]]) -> tuple:
    key = f"recent_exchange:phone:{_fragment_digest(fragments)}"
    conversation_times = sorted({
        str(row.get("conversation_at") or "").strip()
        for row in fragments
        if str(row.get("conversation_at") or "").strip()
    })
    metadata = {
        "key": key,
        "authority": "history",
        "protected": False,
        "sequence": min(int(row["sequence"]) for row in fragments),
        PHONE_SOURCE_LAST_SEQUENCE: max(int(row["sequence"]) for row in fragments),
        "exchange_id": key.removeprefix("recent_exchange:"),
        "source": "live-phone",
    }
    if conversation_times:
        metadata["conversation_at"] = conversation_times[0]
    return (
        "RECENT COMPLETED EXCHANGE",
        _semantic_transcript(fragments),
        metadata,
    )


def _history_units(
    fragments: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_scope: tuple[str, int] | None = None
    current_has_user = False
    for fragment in fragments:
        scope = (str(fragment["call_id"]), int(fragment["call_epoch"]))
        role = str(fragment["role"])
        starts_next_round = (
            bool(current)
            and (
                scope != current_scope
                or (
                    role == "user" and current_has_user
                    and str(current[-1]["role"]) != "user"
                )
            )
        )
        if starts_next_round:
            units.append(current)
            current = []
            current_has_user = False
        if not current:
            current_scope = scope
        current.append(fragment)
        current_has_user = current_has_user or role == "user"
    if current:
        units.append(current)
    return units


def _ordinary_history_sections(
    fragments: list[dict[str, Any]],
) -> list[tuple]:
    return [
        _ordinary_history_section(unit)
        for unit in _history_units(fragments)
        if unit
    ]


def _pending_ended_call(
    connection: Any,
    *,
    owner_id: str,
    agent_id: str,
    session_id: str,
    context_generation: int,
    exclude_call_scope: tuple[str, int] | None = None,
) -> str | None:
    call_clauses = [
        "c.owner_id=?",
        "c.agent_id=?",
        "c.session_id=?",
        "c.context_generation=?",
        "c.ended_at IS NOT NULL",
    ]
    call_params: list[Any] = [
        owner_id, agent_id, session_id, int(context_generation)
    ]
    if exclude_call_scope is not None:
        call_clauses.append("c.call_id<>?")
        call_params.append(str(exclude_call_scope[0]))
    row = connection.execute(f"""SELECT c.call_id FROM live_calls AS c
        WHERE {' AND '.join(call_clauses)} AND (
            EXISTS (
                SELECT 1 FROM live_provider_fragment_inbox AS p
                WHERE p.owner_id=c.owner_id AND p.session_id=c.session_id
                AND p.call_id=c.call_id AND p.call_epoch=c.call_epoch
            )
            OR EXISTS (
                SELECT 1 FROM live_provider_event_inbox AS p
                WHERE p.owner_id=c.owner_id AND p.session_id=c.session_id
                AND p.call_id=c.call_id AND p.call_epoch=c.call_epoch
                AND p.event_type='transcript'
            )
        ) LIMIT 1""", call_params).fetchone()
    return str(row[0]) if row is not None else None


def _ended_fragments(
    store: Any,
    *,
    owner_id: str,
    agent_id: str,
    session_id: str,
    context_generation: int,
    consumer_key: str | None = None,
    unconsumed_only: bool = False,
    exclude_call_scope: tuple[str, int] | None = None,
) -> list[dict[str, Any]]:
    with store._lock, store._connection() as connection:
        pending_call = _pending_ended_call(
            connection,
            owner_id=owner_id,
            agent_id=agent_id,
            session_id=session_id,
            context_generation=context_generation,
            exclude_call_scope=exclude_call_scope,
        )
        if pending_call is not None:
            raise PhoneContextError(
                "phone_context_transcript_pending", source=pending_call
            )
        return _fragments(
            connection,
            owner_id=owner_id,
            agent_id=agent_id,
            session_id=session_id,
            context_generation=context_generation,
            ended_only=True,
            consumer_key=consumer_key,
            unconsumed_only=unconsumed_only,
            exclude_call_scope=exclude_call_scope,
        )


def _action_owner_digest(
    *,
    owner_id: str,
    agent_id: str,
    session_id: str,
    context_generation: int,
    call_id: str,
    call_epoch: int,
) -> str:
    material = [
        "hashi.phone-action-owner.v1",
        str(owner_id),
        str(agent_id),
        str(session_id),
        int(context_generation),
        str(call_id),
        int(call_epoch),
    ]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode()
    ).hexdigest()


def _action_context_is_active(
    store: Any,
    backend: Any,
    *,
    owner_id: str,
    agent_id: str,
    session_id: str,
    context_generation: int,
) -> bool:
    with store._lock, store._connection() as connection:
        active_calls = connection.execute(
            """SELECT call_id,call_epoch FROM live_calls
                WHERE owner_id=? AND agent_id=? AND session_id=?
                AND context_generation=? AND ended_at IS NULL""",
            (owner_id, agent_id, session_id, int(context_generation)),
        ).fetchall()
        # Existing fixed sections identify the epoch in which their snapshot was
        # frozen.  A replacement Provider changes the epoch, not the logical call.
        prior_epochs = connection.execute(
            """SELECT DISTINCT h.call_id,h.call_epoch
                FROM phone_context_handoffs AS h
                JOIN live_calls AS c ON c.call_id=h.call_id
                WHERE c.owner_id=? AND c.agent_id=? AND c.session_id=?
                AND c.context_generation=? AND c.ended_at IS NULL
                AND h.owner_id=c.owner_id AND h.agent_id=c.agent_id
                AND h.session_id=c.session_id
                AND h.context_generation=c.context_generation""",
            (owner_id, agent_id, session_id, int(context_generation)),
        ).fetchall()
    if not active_calls:
        return False

    try:
        action = _fixed_pcm_sections(
            backend, ["phone_action_context"]
        ).get("phone_action_context")
    except PhoneContextError:
        return True
    metadata = action.get("metadata") if isinstance(action, Mapping) else None
    declared = (
        str(metadata.get(ACTION_OWNER_DIGEST_FIELD) or "")
        if isinstance(metadata, Mapping)
        else ""
    )
    if len(declared) != 64 or any(
        character not in "0123456789abcdef" for character in declared
    ):
        # Pre-v4 action sections have no owner digest.  While any scoped call is
        # still active, preserving the temporary section is the safe migration.
        return True
    return any(
        _action_owner_digest(
            owner_id=owner_id,
            agent_id=agent_id,
            session_id=session_id,
            context_generation=context_generation,
            call_id=str(row["call_id"]),
            call_epoch=int(row["call_epoch"]),
        ) == declared
        for row in [*active_calls, *prior_epochs]
    )


def prepare_turn(runtime, item, *, incremental: bool) -> tuple[list[tuple],dict[str,Any]]:
    from orchestrator import runtime_session
    session_id=str(getattr(item,"session_id","") or "")
    if not session_id: return [],{}
    meta=getattr(item,"request_metadata",None) or {}
    owner_id=runtime_session.owner_id(
        runtime,
        str(getattr(item,"owner_id","") or meta.get("owner_id") or "") or None,
    )
    store=runtime_session.ensure_store(runtime)
    generation=int(getattr(item,"context_generation",1) or 1)
    _scope(
        store,owner_id=owner_id,agent_id=runtime.name,session_id=session_id,
        context_generation=generation,
    )
    live=meta.get("live_voice") if isinstance(meta.get("live_voice"),dict) else None
    snapshot=None
    action_fragments: list[dict[str,Any]] = []
    if live:
        handoff_id=str(live.get("phone_context_handoff_id") or "")
        if not handoff_id:
            raise PhoneContextError("phone_context_handoff_unavailable",source=session_id)
        snapshot=load_handoff(
            store,handoff_id,owner_id=owner_id,agent_id=runtime.name,
            session_id=session_id,context_generation=generation,
        )
        for key in (
            "call_id","call_epoch","delegation_id","proposal_version","proposal_digest"
        ):
            if key in live and str(live[key])!=str(snapshot[key]):
                raise PhoneContextError("phone_context_scope_changed",source=handoff_id)
        action_fragments=list(snapshot["fragments"])
        from orchestrator.session_store import _live_epoch_starts
        with store._lock, store._connection() as connection:
            call = connection.execute(
                """SELECT started_at FROM live_calls
                    WHERE owner_id=? AND agent_id=? AND session_id=?
                    AND context_generation=? AND call_id=?""",
                (owner_id, runtime.name, session_id, generation, str(snapshot["call_id"])),
            ).fetchone()
            epoch_starts = _live_epoch_starts(
                connection, session_id,
                {str(snapshot["call_id"])} if any(
                    int(fragment["call_epoch"]) > 1 for fragment in action_fragments
                ) else set(),
            )
        if call is not None:
            for fragment in action_fragments:
                epoch = int(fragment["call_epoch"])
                start = (call["started_at"] if epoch == 1
                         else epoch_starts.get((str(snapshot["call_id"]), epoch)))
                fragment["conversation_at"] = _conversation_at(
                    start, int(fragment["start_ms"])
                )

    backend=runtime.backend_manager.current_backend
    backend_id=str(runtime.config.active_backend)
    consumer=_consumer(backend_id,backend)
    typed_pcm = incremental and callable(
        getattr(backend,"current_pcm_section_keys",None)
    )
    fixed_section_keys = _fixed_pcm_section_keys(backend) if typed_pcm else ()
    legacy_section_keys = [
        key for key in fixed_section_keys if key.startswith(LEGACY_SECTION_PREFIX)
    ]
    semantic_section_keys = [
        key for key in fixed_section_keys if key.startswith(SEMANTIC_SECTION_PREFIX)
    ]
    has_action_section = "phone_action_context" in fixed_section_keys
    action_context_active = bool(
        has_action_section
        and not live
        and _action_context_is_active(
            store,
            backend,
            owner_id=owner_id,
            agent_id=runtime.name,
            session_id=session_id,
            context_generation=generation,
        )
    )
    has_consumption=False
    if incremental:
        with store._lock,store._connection() as connection:
            has_consumption=connection.execute("""SELECT 1
                FROM phone_context_consumption
                WHERE owner_id=? AND agent_id=? AND session_id=?
                AND context_generation=? AND consumer_key=? LIMIT 1""",(
                    owner_id,runtime.name,session_id,generation,consumer,
                )).fetchone() is not None
    migrate_history = bool(legacy_section_keys) or (
        not has_consumption and bool(semantic_section_keys)
    )

    history_fragments: list[dict[str,Any]] = []
    action_scope = (
        (str(snapshot["call_id"]),int(snapshot["call_epoch"]))
        if snapshot else None
    )
    if incremental:
        history_fragments=_ended_fragments(
            store,
            owner_id=owner_id,
            agent_id=runtime.name,
            session_id=session_id,
            context_generation=generation,
            consumer_key=consumer,
            unconsumed_only=not migrate_history,
            exclude_call_scope=action_scope,
        )
    else:
        with store._lock,store._connection() as connection:
            pending_call=_pending_ended_call(
                connection,
                owner_id=owner_id,
                agent_id=runtime.name,
                session_id=session_id,
                context_generation=generation,
                exclude_call_scope=action_scope,
            )
        if pending_call is not None:
            raise PhoneContextError(
                "phone_context_transcript_pending", source=pending_call
            )
    included=action_fragments if live else history_fragments
    replacement_by_identity: dict[tuple[str,int,str],dict[str,Any]] = {}
    for fragment in [*history_fragments,*action_fragments]:
        replacement_by_identity[_fragment_identity(fragment)]=fragment
    replacement_fragments=list(replacement_by_identity.values())
    semantic_migration_keys = semantic_section_keys if migrate_history else []
    migration_keys = [*legacy_section_keys, *semantic_migration_keys]
    migration_sections = (
        _fixed_pcm_sections(backend, migration_keys) if migration_keys else {}
    )
    _validate_legacy_sources(
        migration_sections,
        legacy_section_keys,
        replacement_fragments,
        session_id=session_id,
        agent_id=runtime.name,
        context_generation=generation,
    )
    retained_semantic_keys = _validate_semantic_sources(
        migration_sections, semantic_migration_keys, replacement_fragments
    )

    migration_removals = (
        [*legacy_section_keys, *(key for key in semantic_section_keys
                                if key not in retained_semantic_keys)]
        if migrate_history else []
    )
    audit={
        "schema":"hashi.phone-context-handoff.v1",
        "handoff_id":snapshot["handoff_id"] if snapshot else None,
        "cutoff_ms":snapshot["cutoff_ms"] if snapshot else None,
        "requested_fragments":len(action_fragments if live else history_fragments),
        "included_fragments":len(included),
        "consumer_key":consumer,
        "incremental":bool(incremental),
        "source":"durable_phone_transcript",
        "source_event_ids":[row["provider_event_id"] for row in included],
        "history_fragments":len(history_fragments) if live else 0,
        "history_source_event_ids":[
            row["provider_event_id"] for row in history_fragments
        ] if live else [],
        "history_migration_fragments":(
            len(history_fragments) if live and migrate_history else 0
        ),
        "history_migration_source_event_ids":(
            [row["provider_event_id"] for row in history_fragments]
            if live and migrate_history else []
        ),
        "retained_section_keys":retained_semantic_keys,
        "removed_section_keys":(
            migration_removals
            if live else [
                *(
                    ["phone_action_context"]
                    if has_action_section and not action_context_active else []
                ),
                *migration_removals,
            ]
        ),
    }
    # The protected action snapshot is temporary transport context.  It must
    # never advance ordinary-history consumption, including on the first HERV3
    # full open.  Ended history delivered beside an action remains consumable.
    consumed_on_success=history_fragments if live else included
    plan={
        "store":store,
        "owner_id":owner_id,
        "agent_id":runtime.name,
        "session_id":session_id,
        "context_generation":generation,
        "backend_id":backend_id,
        "backend":backend,
        "fragments":consumed_on_success,
        "consumer_key":consumer if getattr(backend,"_session_id",None) else None,
    }
    item._phone_context_handoff=plan
    if not included:
        return [],audit
    if live:
        audit["exclude_history_exchange_prefix"]=(
            f"live:{snapshot['call_id']}:"
        )
        body=(
            "Frozen Phone conversation for the current action. This is conversation "
            "history, including earlier connections of the same call. Only user speech "
            "from the current connection captured before the action cutoff authorizes "
            "this action; later speech cannot change it.\n\n"
            + _semantic_transcript(included)
        )
        sections=_ordinary_history_sections(history_fragments)
        action_metadata = {
            "key":"phone_action_context",
            "authority":"history",
            "protected":True,
            "source":"live-phone",
            ACTION_OWNER_DIGEST_FIELD:_action_owner_digest(
                owner_id=owner_id,
                agent_id=runtime.name,
                session_id=session_id,
                context_generation=generation,
                call_id=str(snapshot["call_id"]),
                call_epoch=int(snapshot["call_epoch"]),
            ),
        }
        action_times = sorted({
            str(row.get("conversation_at") or "").strip()
            for row in action_fragments
            if str(row.get("conversation_at") or "").strip()
        })
        if action_times:
            action_metadata["conversation_at"] = action_times[0]
        sections.append((
            "PHONE ACTION CONTEXT",
            body,
            action_metadata,
        ))
        return sections,audit
    if not incremental:
        return [],audit
    return _ordinary_history_sections(included),audit

def commit_success(item, response) -> None:
    plan=getattr(item,"_phone_context_handoff",None)
    if not isinstance(plan,dict) or not bool(getattr(response,"is_success",False)): return
    backend=plan["backend"]
    if not getattr(backend,"_session_id",None): return
    key=plan["consumer_key"] or _consumer(plan["backend_id"],backend)
    store=plan["store"]
    _scope(store,owner_id=plan["owner_id"],agent_id=plan["agent_id"],
        session_id=plan["session_id"],context_generation=plan["context_generation"])
    with store._lock,store._connection() as connection:
        for row in plan["fragments"]:
            connection.execute("""INSERT OR IGNORE INTO phone_context_consumption VALUES(?,?,?,?,?,?,?,?,?)""",
                (plan["owner_id"],plan["agent_id"],plan["session_id"],plan["context_generation"],
                 key,row["call_id"],row["call_epoch"],row["provider_event_id"],datetime.now(timezone.utc).isoformat()))
