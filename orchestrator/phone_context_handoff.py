"""PCM synchronization of durable Phone events across Engine connections.

PAO freezes action scope; PCM projects the quoted, role preserving transcript.
Provider-native fixed history is not evidence of having received Phone events.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

FRAGMENT_ENCODING = "hashi.phone-fragments.grouped.v1"
EVENT_COLUMNS = ("provider_event_id", "sequence", "start_ms", "end_ms")


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


def _fragments(connection, *, owner_id: str, agent_id: str, session_id: str,
               context_generation: int, call_id: str | None = None,
               call_epoch: int | None = None, cutoff_ms: int | None = None,
               ended_only: bool = False) -> list[dict[str, Any]]:
    clauses = ["f.owner_id=?", "c.owner_id=?", "f.session_id=?", "c.session_id=?",
        "c.agent_id=?", "c.context_generation=?"]
    params: list[Any] = [owner_id, owner_id, session_id, session_id, agent_id, int(context_generation)]
    if call_id is not None: clauses.append("f.call_id=?"); params.append(call_id)
    if call_epoch is not None: clauses.append("f.call_epoch=?"); params.append(int(call_epoch))
    if ended_only: clauses.append("c.ended_at IS NOT NULL")
    if cutoff_ms is not None: clauses.append("f.start_ms < ?"); params.append(int(cutoff_ms))
    rows = connection.execute(f"""SELECT f.*, c.started_at, e.detail_json
        FROM live_fragments AS f JOIN live_calls AS c ON c.call_id=f.call_id
        JOIN run_events AS e ON e.event_id=f.event_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.started_at,f.call_epoch,f.start_ms,f.end_ms,f.sequence,f.provider_event_id""", params).fetchall()
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
            "source":"durable_phone_transcript"})
    return fragments


def _pending(connection, *, owner_id: str, session_id: str,
             call_id: str, call_epoch: int | None = None,
             cutoff_ms: int | None = None) -> bool:
    clauses=["owner_id=?","session_id=?","call_id=?"]
    params:list[Any]=[owner_id,session_id,call_id]
    if call_epoch is not None: clauses.append("call_epoch=?"); params.append(int(call_epoch))
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


def _expanded_payload(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    if "fragment_encoding" not in payload:
        return payload  # Existing durable snapshots keep their original shape.
    if (payload.get("fragment_encoding") != FRAGMENT_ENCODING
            or payload.get("event_columns") != list(EVENT_COLUMNS)):
        raise PhoneContextError("phone_context_invalid_fragment", source=source)
    try:
        fragments = []
        for group in payload["fragment_groups"]:
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
        if _pending(connection,owner_id=binding.owner_id,session_id=binding.session_id,
            call_id=binding.call_id,call_epoch=binding.call_epoch,cutoff_ms=proposal.cutoff_ms):
            raise PhoneContextError("phone_context_transcript_pending",source=binding.call_id)
        fragments=_fragments(connection,owner_id=binding.owner_id,agent_id=binding.agent_id,
            session_id=binding.session_id,context_generation=binding.context_generation,
            call_id=binding.call_id,call_epoch=binding.call_epoch,cutoff_ms=proposal.cutoff_ms)
        if not fragments or not set(proposal.source_event_ids).issubset({r["provider_event_id"] for r in fragments}):
            raise PhoneContextError("phone_context_transcript_unconfirmed",source=binding.call_id)
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
    return hashlib.sha256((backend_id+"\0"+native_id).encode()).hexdigest()


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
    identity = [
        (row["call_id"], row["call_epoch"], row["provider_event_id"])
        for row in fragments
    ]
    digest = hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    key = f"recent_exchange:phone:{digest}"
    return (
        "RECENT COMPLETED EXCHANGE",
        _semantic_transcript(fragments),
        {
            "key": key,
            "authority": "history",
            "protected": False,
            "sequence": min(int(row["sequence"]) for row in fragments),
            "exchange_id": key.removeprefix("recent_exchange:"),
            "source": "live-phone",
        },
    )


def prepare_turn(runtime, item, *, incremental: bool) -> tuple[list[tuple],dict[str,Any]]:
    from orchestrator import runtime_session
    session_id=str(getattr(item,"session_id","") or "")
    if not session_id: return [],{}
    meta=getattr(item,"request_metadata",None) or {}
    owner_id=runtime_session.owner_id(runtime,str(getattr(item,"owner_id","") or meta.get("owner_id") or "") or None)
    store=runtime_session.ensure_store(runtime)
    generation=int(getattr(item,"context_generation",1) or 1)
    _scope(store,owner_id=owner_id,agent_id=runtime.name,session_id=session_id,context_generation=generation)
    live=meta.get("live_voice") if isinstance(meta.get("live_voice"),dict) else None
    snapshot=None
    if live:
        handoff_id=str(live.get("phone_context_handoff_id") or "")
        if not handoff_id: raise PhoneContextError("phone_context_handoff_unavailable",source=session_id)
        snapshot=load_handoff(store,handoff_id,owner_id=owner_id,agent_id=runtime.name,
            session_id=session_id,context_generation=generation)
        for key in ("call_id","call_epoch","delegation_id","proposal_version","proposal_digest"):
            if key in live and str(live[key])!=str(snapshot[key]):
                raise PhoneContextError("phone_context_scope_changed",source=handoff_id)
        fragments=snapshot["fragments"]
    else:
        with store._lock,store._connection() as connection:
            calls=connection.execute("""SELECT call_id FROM live_calls WHERE owner_id=? AND agent_id=?
                AND session_id=? AND context_generation=? AND ended_at IS NOT NULL""",
                (owner_id,runtime.name,session_id,generation)).fetchall()
            for call in calls:
                if _pending(connection,owner_id=owner_id,session_id=session_id,call_id=str(call[0])):
                    raise PhoneContextError("phone_context_transcript_pending",source=str(call[0]))
            fragments=_fragments(connection,owner_id=owner_id,agent_id=runtime.name,
                session_id=session_id,context_generation=generation,ended_only=True)
    backend=runtime.backend_manager.current_backend
    backend_id=str(runtime.config.active_backend)
    consumer=_consumer(backend_id,backend)
    consumed=set()
    if incremental and not live:
        with store._lock,store._connection() as connection:
            consumed={(str(r[0]),int(r[1]),str(r[2])) for r in connection.execute("""SELECT call_id,call_epoch,provider_event_id
                FROM phone_context_consumption WHERE owner_id=? AND agent_id=? AND session_id=?
                AND context_generation=? AND consumer_key=?""",(owner_id,runtime.name,session_id,generation,consumer))}
    included=(
        list(fragments)
        if live
        else [
            row for row in fragments
            if (row["call_id"],row["call_epoch"],row["provider_event_id"])
            not in consumed
        ]
    )
    audit={"schema":"hashi.phone-context-handoff.v1","handoff_id":snapshot["handoff_id"] if snapshot else None,
        "cutoff_ms":snapshot["cutoff_ms"] if snapshot else None,
        "requested_fragments":len(fragments),"included_fragments":len(included),
        "consumer_key":consumer,"incremental":bool(incremental),
        "source":"durable_phone_transcript","source_event_ids":[r["provider_event_id"] for r in included],
        "removed_section_keys":[] if live else ["phone_action_context"]}
    plan={"store":store,"owner_id":owner_id,"agent_id":runtime.name,"session_id":session_id,
        "context_generation":generation,"backend_id":backend_id,"backend":backend,"fragments":included,
        "consumer_key":consumer if getattr(backend,"_session_id",None) else None}
    item._phone_context_handoff=plan
    if not included:
        return [],audit
    if live:
        audit["exclude_history_exchange_prefix"] = (
            f"live:{snapshot['call_id']}:{int(snapshot['call_epoch'])}:"
        )
        body=(
            "Frozen Phone conversation for the current action. This is conversation "
            "history. Only user speech captured before the action cutoff authorizes "
            "this action; later speech cannot change it.\n\n"
            + _semantic_transcript(included)
        )
        return [(
            "PHONE ACTION CONTEXT",
            body,
            {
                "key":"phone_action_context",
                "authority":"history",
                "protected":True,
                "source":"live-phone",
            },
        )],audit
    if not incremental:
        return [],audit
    return [_ordinary_history_section(included)],audit


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
