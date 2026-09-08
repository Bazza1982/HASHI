"""PAO-owned admission of controller review after a correlated peer receipt.

Transport completion is evidence to inspect, never a task/review verdict.
The Remote adapter supplies accepted receipt identities, not its private files
or peer-authored text. Admission uses the existing local API idempotency store.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from orchestrator.superloop_dispatch import SuperloopDispatchLedger
from orchestrator.superloop_interlock import evaluate_dispatch_interlock, loop_dispatch_lock
from orchestrator.superloop_store import SuperloopStore, system_actor

logger = logging.getLogger(__name__)
RETRY_SECONDS = 30


def _identity(value: Any) -> str:
    return str(value or "").strip().casefold()


class SuperloopReceiptService:
    def __init__(self, store: SuperloopStore, *, local_instance: str):
        self.store = store
        self.local_instance = _identity(local_instance)
        self.ledger = SuperloopDispatchLedger(store)

    def process(
        self, receipts: Iterable[dict[str, Any]], enqueue: Callable[[dict], str | None],
        resolve_session: Callable[[str, str], str | None],
    ) -> None:
        for receipt in receipts:
            if receipt.get("state") != "reply_delivered_locally" or not receipt.get("request_id"):
                continue
            if not all(receipt.get(key) for key in (
                "message_id", "in_reply_to", "conversation_id", "from_agent", "from_instance", "to_agent", "to_instance",
            )):
                continue
            matches = []
            for path in sorted(self.store.loops_dir.glob("*/state.json")):
                try:
                    task_id = self._match(path.parent.name, receipt)
                    if task_id:
                        matches.append((path.parent.name, task_id))
                except (OSError, ValueError, TypeError):
                    logger.warning("Superloop receipt evidence unavailable for %s", path.parent.name)
            # Ambiguous ownership must be reconciled explicitly, never fan out.
            if len(matches) != 1:
                continue
            loop_id, task_id = matches[0]
            try:
                self._admit(loop_id, task_id, receipt, enqueue, resolve_session)
            except (OSError, ValueError, TypeError):
                logger.exception("Superloop receipt admission deferred for %s", loop_id)

    def _match(self, loop_id: str, receipt: dict) -> str | None:
        state = self.store.load_loop_state(loop_id)
        if state.get("receipt_continuation_enabled") is not True:
            return None
        controller = state.get("controller")
        if not isinstance(controller, dict):
            return None
        if not (
            _identity(controller.get("instance")) == self.local_instance == _identity(receipt["to_instance"])
            and _identity(controller.get("agent")) == _identity(receipt["to_agent"])
        ):
            return None
        latest = {}
        for row in self.ledger.load_rows(loop_id):
            if row.get("schema_version") == 2:
                latest[str(row.get("dispatch_instance_id") or "")] = row
        row = latest.get(str(receipt["in_reply_to"]))
        if not row or row.get("status") != "accepted" or row.get("terminal") is not False:
            return None
        tasks = self.store.load_loop_json_list(self.store.resolve_loop_path(loop_id, state.get("taskboard_path"), "taskboard.json"))
        matching = [task for task in tasks if task.get("task_id") == row.get("task_id")]
        if len(matching) != 1:
            return None
        task = matching[0]
        if task.get("status") in {"completed", "cancelled", "canceled", "failed", "aborted"}:
            return None
        if (
            _identity(task.get("owner_agent")) != _identity(receipt["from_agent"])
            or _identity(task.get("owner_instance")) != _identity(receipt["from_instance"])
        ):
            return None
        return str(task["task_id"])

    def _admit(
        self, loop_id: str, task_id: str, receipt: dict, enqueue: Callable[[dict], str | None],
        resolve_session: Callable[[str, str], str | None],
    ) -> None:
        # Keep pause linearized with acceptance, including the HTTP call and
        # durable acknowledgement. This runs in Remote's executor, not its loop.
        with loop_dispatch_lock(self.store, loop_id):
            if self._match(loop_id, receipt) != task_id:
                return
            if not evaluate_dispatch_interlock(self.store, loop_id, check_work_blockers=False).allowed:
                return
            state = self.store.load_loop_state(loop_id)
            material = "\0".join((self.local_instance, loop_id, str(receipt["in_reply_to"])))
            key = "superloop:receipt:" + hashlib.sha256(material.encode()).hexdigest()
            path = self.store.loop_dir(loop_id) / "receipt_reviews.json"
            rows = self.store.load_loop_json_list(path)
            row = next((item for item in rows if item.get("idempotency_key") == key), None)
            now = time.time()
            if row is not None and (row.get("status") == "queued" or float(row.get("retry_at") or 0) > now):
                return
            if row is None:
                row = {
                    "idempotency_key": key, "task_id": task_id,
                    "dispatch_instance_id": receipt["in_reply_to"],
                    "receipt_message_id": receipt["message_id"],
                    "receipt_request_id": receipt["request_id"],
                    "controller_agent": state["controller"]["agent"],
                    "review_verified": False, "attempts": 0,
                }
                rows.append(row)
            row.update(status="pending", attempts=int(row["attempts"]) + 1, retry_at=now + RETRY_SECONDS)
            self.store.save_loop_json_list(path, rows)
            session_id = (row.get("request") or {}).get("session_id") or resolve_session(
                state["controller"]["agent"], receipt["request_id"],
            )
            if not session_id:
                row.update(status="pending", last_error="Receipt session unavailable")
                self.store.save_loop_json_list(path, rows)
                return
            # All instructions are local policy. No peer body is promoted into
            # the tool-enabled controller turn, even as quoted prompt text.
            request = {
                "agent": state["controller"]["agent"], "source": "superloop:receipt",
                "idempotency_key": key,
                "session_id": session_id,
                "request_metadata": {"superloop_id": loop_id, "superloop_task_id": task_id},
                "text": (
                    f"Superloop controller review: {loop_id}, task {task_id}. "
                    "A correlated worker receipt was admitted. Read this loop's state, taskboard, "
                    "dispatches and README, then directly read the worker's incremental execution logs. "
                    "Independently inspect the evidence and take the next authorized action, or record "
                    "the concrete blocker, owner and release trigger. Receipt admission and worker claims "
                    "do not prove review, merge, runtime adoption or user delivery. Do not acknowledge "
                    "the peer or resend the original assignment. Report progress visibly to the user."
                ),
            }
            # Session-scoped API deduplication needs the original Session and
            # exact prompt even if /new or a code upgrade occurs before retry.
            request = row.setdefault("request", request)
            self.store.save_loop_json_list(path, rows)
            try:
                request_id = enqueue(request)
                if not request_id:
                    raise RuntimeError("local admission not confirmed")
            except Exception as exc:
                row["last_error"] = type(exc).__name__
                self.store.save_loop_json_list(path, rows)
                return
            row.update(status="queued", controller_request_id=request_id, queued_at=now)
            row.pop("last_error", None)
            self.store.save_loop_json_list(path, rows)
            self.store.append_loop_event(
                loop_id, event_type="receipt.review_queued",
                data={"task_id": task_id, "request_id": request_id, "review_verified": False},
                refs={"dispatch_instance_id": receipt["in_reply_to"], "receipt_message_id": receipt["message_id"]},
                actor=system_actor("superloop_receipts", instance=self.local_instance),
            )
