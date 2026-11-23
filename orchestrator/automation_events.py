"""Read-only, owner-scoped terminal Agent events for HEA v1.

Only durable Run terminal events are exposed. This projection deliberately does
not subscribe to provider deltas, logs, or text, and changes no Session schema
semantics. Event order comes from the append-only ``automation_event_index``
whose AUTOINCREMENT identity stays monotonic even after history deletion;
``run_events.rowid`` is never used as a feed cursor, so SQLite rowid reuse
cannot silently rewind or skip new events.
"""
from __future__ import annotations

import asyncio

from aiohttp import web

EVENT_KINDS = {
    "run.completed": "agent.run.completed",
    "run.failed": "agent.run.failed",
}
MAX_LIMIT = 100
MAX_WAIT_SECONDS = 12


def terminal_events_page(store, *, owner_id: str, after: int, limit: int = 50) -> dict:
    """Read committed events across an owner's Sessions with one DB query window."""
    if not isinstance(after, int) or after < -1:
        raise ValueError("after must be -1 or a non-negative integer")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit is outside the supported range")
    with store._lock, store._connection() as connection:
        scope = (str(owner_id),)
        head = connection.execute(
            """SELECT COALESCE(MAX(i.feed_id), 0) AS cursor
               FROM automation_event_index AS i
               WHERE i.owner_id = ?""",
            scope,
        ).fetchone()
        latest = int(head["cursor"])
        if after == -1:
            return {
                "events": [], "next_cursor": latest, "latest_cursor": latest,
                "has_more": False, "reset_required": False,
            }
        if after > latest:
            # History destruction can move the newest cursor backwards.
            # Report a reset explicitly: consumers re-baseline and must not
            # replay or silently drop events.
            return {
                "events": [], "next_cursor": latest, "latest_cursor": latest,
                "has_more": False, "reset_required": True,
            }
        rows = connection.execute(
            """SELECT i.feed_id AS cursor, i.event_id, i.run_id, i.session_id,
                      i.kind, i.created_at, i.agent_id,
                      COALESCE(r.idempotency_key, '') AS run_idempotency_key
               FROM automation_event_index AS i
               JOIN sessions AS s ON s.session_id = i.session_id
               LEFT JOIN runs AS r ON r.run_id = i.run_id
               WHERE i.owner_id = ?
                 AND s.status != 'deleted'
                 AND i.feed_id > ?
               ORDER BY i.feed_id ASC LIMIT ?""",
            (*scope, after, limit + 1),
        ).fetchall()

    selected = rows[:limit]
    events = [
        {
            "cursor": int(row["cursor"]),
            "event_id": str(row["event_id"]),
            "type": EVENT_KINDS[str(row["kind"])],
            "agent_id": str(row["agent_id"]),
            "run_id": str(row["run_id"] or ""),
            "session_id": str(row["session_id"]),
            "occurred_at": str(row["created_at"]),
            # Automation-dispatched Runs (HEA "hea:" and HAL "hal:" idempotency
            # prefixes) must not recursively trigger entry rules. The raw key
            # is exposed so consumers can correlate dependent workflow stages.
            # Keep prefixes in sync with hal/event_source.py.
            "run_idempotency_key": str(row["run_idempotency_key"] or ""),
            "automation_origin": str(row["run_idempotency_key"]).startswith(("hea:", "hal:")),
        }
        for row in selected
    ]
    return {
        "events": events,
        "next_cursor": events[-1]["cursor"] if events else after,
        "latest_cursor": latest,
        "has_more": len(rows) > limit,
        "reset_required": False,
    }


async def handle_automation_events(server, request):
    """Authenticated, bounded long poll over committed Session event evidence."""
    owner_id = server._v1_owner_id(request)
    if owner_id is None or not server._check_admin_auth(request):
        return web.json_response(
            {"ok": False, "error_code": "auth_required"}, status=401,
        )
    try:
        after = int(request.query.get("after", "-1"))
        limit = int(request.query.get("limit", "50"))
        wait_seconds = float(request.query.get("wait_seconds", "0"))
        if not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise ValueError("wait_seconds must be between 0 and 12")
        # One bounded waiter per connected Workbench. There is no global
        # scheduler, per-Agent watcher, or repeated provider call.
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            result = await asyncio.to_thread(
                terminal_events_page, server.session_store,
                owner_id=owner_id, after=after, limit=limit,
            )
            if result["events"] or result["reset_required"] or after == -1:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            # Cross-process Session writers cannot signal an asyncio.Event.
            # One cheap indexed DB check every <=5s is the recovery fallback.
            await asyncio.sleep(min(5.0, remaining))
        return web.json_response({
            "ok": True, "schema": "hashi.automation-events/v1",
            "instance_id": server.session_store.instance_id,
            **result,
        }, headers={"Cache-Control": "no-store"})
    except (TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error_code": "automation_cursor_invalid"}, status=400,
        )
    except Exception:
        server.logger.exception("HEA terminal event read failed") if getattr(server, "logger", None) else None
        return web.json_response(
            {"ok": False, "error_code": "automation_feed_unavailable"}, status=503,
        )
# HASHI-Fingerprint: hfp_2a81a60d13cd41ceb1c6d85b1edb7ba1
