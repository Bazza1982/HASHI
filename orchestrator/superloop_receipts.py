"""PAO-owned admission of controller review after a correlated peer receipt.

Transport completion is evidence to inspect, never a task/review verdict.
The Remote adapter supplies accepted receipt identities, not its private files
or peer-authored text. Admission uses the existing local API idempotency store.
"""
from __future__ import annotations

import hashlib
import logging
import math
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
        activity: Callable[[str, str], dict | None] | None = None,
    ) -> None:
        if activity is not None:
            self.reconcile(enqueue, activity)
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

    def _evidence_exists(self, loop_id: str, ref: Any) -> bool:
        if not isinstance(ref, str) or not ref.strip():
            return False
        root = self.store.loop_dir(loop_id).resolve()
        path = (root / ref).resolve()
        return path.is_relative_to(root) and path.is_file()

    def delivery_gaps(self, loop_id: str) -> list[str]:
        """Require recorded outcome evidence only for opted-in completed tasks."""
        state = self.store.load_loop_state(loop_id)
        tasks = self.store.load_loop_json_list(self.store.resolve_loop_path(
            loop_id, state.get("taskboard_path"), "taskboard.json"))
        gaps = []
        for task in tasks:
            if task.get("status") != "completed" or task.get("delivery_required") is not True:
                continue
            requirements = ["runtime_adoption", "user_acceptance"]
            if task.get("terminal_delivery_required") is True:
                requirements.append("terminal_delivery")
            for requirement in requirements:
                if (task.get(requirement + "_verified") is not True
                        or not self._evidence_exists(loop_id, task.get(requirement + "_evidence_ref"))):
                    gaps.append(f"{task.get('task_id')}:{requirement}")
        return gaps

    def review_gaps(self, loop_id: str, row: dict) -> list[str]:
        """Validate recorded dispositions, not the truth of a manager's claims."""
        gaps = self.delivery_gaps(loop_id)
        if row.get("review_verified") is not True or not self._evidence_exists(loop_id, row.get("review_evidence_ref")):
            gaps.append("review_evidence")
        state = self.store.load_loop_state(loop_id)
        tasks = self.store.load_loop_json_list(self.store.resolve_loop_path(loop_id, state.get("taskboard_path"), "taskboard.json"))
        dispositions = row.get("dispositions") or []
        if not isinstance(dispositions, list):
            return gaps + ["dispositions"]
        by_task = {d.get("task_id"): d for d in dispositions if isinstance(d, dict)}
        latest = {}
        for dispatch in self.ledger.load_rows(loop_id):
            latest[dispatch.get("dispatch_instance_id")] = dispatch
        continuous = state.get("continuous_supervision_required") is True

        def disposition_valid(task: dict, d: Any, *, allow_action: bool = True) -> bool:
            if not isinstance(d, dict):
                return False
            kind = d.get("kind")
            if kind == "active_dispatch":
                dispatch = latest.get(d.get("dispatch_instance_id"), {})
                return (dispatch.get("task_id") == task.get("task_id")
                        and dispatch.get("status") == "accepted" and dispatch.get("terminal") is False
                        and self._evidence_exists(loop_id, d.get("evidence_ref")))
            if kind == "action" and allow_action:
                return (self._evidence_exists(loop_id, d.get("evidence_ref"))
                        and (not continuous or disposition_valid(task, d.get("next"), allow_action=False)))
            if kind in {"blocked", "deferred"}:
                if not all(isinstance(d.get(k), str) and d[k].strip()
                           for k in ("reason", "owner", "trigger")):
                    return False
                deadline = d.get("review_after")
                if deadline is None:
                    return not continuous
                # Absolute UTC epoch seconds; invalid/nonfinite values must not
                # turn an intentional wait into a permanent exemption.
                return (isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
                        and math.isfinite(deadline) and deadline > time.time())
            return False

        for task in tasks:
            if task.get("status") in {"completed", "cancelled", "canceled", "aborted", "failed"}:
                continue
            if not disposition_valid(task, by_task.get(task.get("task_id"), {})):
                gaps.append(str(task.get("task_id")))
        return gaps

    def reconcile(self, enqueue: Callable[[dict], str | None], activity: Callable[[str, str], dict | None]) -> None:
        # Independent of transport retention and task collection. HTTP reads run
        # outside the lock; reload before writing so a concurrent review survives.
        for path in sorted(self.store.loops_dir.glob("*/receipt_reviews.json")):
            loop_id = path.parent.name
            try:
                state = self.store.load_loop_state(loop_id)
                controller = state.get("controller") or {}
                if not isinstance(controller, dict) or _identity(controller.get("instance")) != self.local_instance:
                    continue
                snapshots = self.store.load_loop_json_list(path)
                latest_review = next((item for item in reversed(snapshots)
                    if isinstance(item, dict) and item.get("status") == "queued"), {})
                for snapshot in snapshots:
                    if not isinstance(snapshot, dict) or snapshot.get("status") != "queued":
                        continue
                    # Only the latest review supervises the current whole board;
                    # historical receipts must not fan out recovery for one gap.
                    if snapshot.get("followthrough_state") == "reviewed" and (
                            snapshot.get("idempotency_key") != latest_review.get("idempotency_key")
                            or not self.review_gaps(loop_id, snapshot)):
                        continue
                    now = time.time()
                    if float(snapshot.get("check_after") or 0) > now:
                        continue
                    if not isinstance(snapshot.get("request"), dict) or not isinstance(snapshot.get("recovery", {}), dict):
                        continue
                    recovery = snapshot.get("recovery") or {}
                    target = recovery if recovery.get("controller_request_id") else snapshot
                    request_id = target.get("controller_request_id")
                    if not request_id:
                        continue
                    try:
                        observed = activity(str(controller.get("agent") or ""), str(request_id))
                    except Exception:
                        observed = None
                    with loop_dispatch_lock(self.store, loop_id):
                        rows = self.store.load_loop_json_list(path)
                        row = next((r for r in rows if r.get("idempotency_key") == snapshot.get("idempotency_key")), None)
                        if row is None:
                            continue
                        if row.get("followthrough_state") == "reviewed":
                            latest_current = next((item for item in reversed(rows)
                                if isinstance(item, dict) and item.get("status") == "queued"), {})
                            if (row.get("idempotency_key") != latest_current.get("idempotency_key")
                                    or not self.review_gaps(loop_id, row)):
                                continue
                        if not isinstance(row.get("request"), dict) or not isinstance(row.get("recovery", {}), dict):
                            continue
                        current_recovery = row.get("recovery") or {}
                        current_target = current_recovery if current_recovery.get("controller_request_id") else row
                        current_state = self.store.load_loop_state(loop_id)
                        if (current_target.get("controller_request_id") != request_id
                                or current_state.get("controller") != controller
                                or row.get("controller_agent") != controller.get("agent")
                                or (row.get("request") or {}).get("agent") != controller.get("agent")):
                            continue
                        row["check_after"] = now + RETRY_SECONDS
                        expected_session = (row.get("request") or {}).get("session_id")
                        if not (isinstance(observed, dict) and observed.get("ok") is True
                                and observed.get("request_id") == request_id
                                and observed.get("agent_id") == controller.get("agent")
                                and observed.get("session_id") == expected_session):
                            row["observation_error"] = "activity_unavailable_or_identity_mismatch"
                            self.store.save_loop_json_list(path, rows)
                            continue
                        row.pop("observation_error", None)
                        # The request activity API exposes execution, not the
                        # canonical Connector delivery_event. Never promote Run
                        # completion or a manager-written file to sent evidence.
                        row["report_delivery_state"] = "unverified"
                        row["report_delivery_reason"] = "activity_api_has_no_connector_delivery_evidence"
                        row["execution_state"] = observed.get("state")
                        row["observed_request_id"] = request_id
                        gaps = self.review_gaps(loop_id, row)
                        row["review_gaps"] = gaps
                        if observed.get("terminal") is not True:
                            row["followthrough_state"] = "awaiting_execution"
                        elif observed.get("state") != "completed":
                            row["followthrough_state"] = "needs_attention"
                        elif not gaps:
                            row["followthrough_state"] = "reviewed"
                        else:
                            row["followthrough_state"] = "needs_attention"
                            state = self.store.load_loop_state(loop_id)
                            permitted = (state.get("receipt_continuation_enabled") is True
                                and evaluate_dispatch_interlock(self.store, loop_id, check_work_blockers=False).allowed)
                            recovery = row.get("recovery")
                            if permitted and not (recovery or {}).get("controller_request_id"):
                                if recovery is None:
                                    request = dict(row["request"])
                                    request["idempotency_key"] = row["idempotency_key"] + ":followthrough:1"
                                    request["text"] = (
                                        f"Superloop follow-through recovery: {loop_id}. The prior controller run "
                                        "completed but its review or whole-board dispositions are incomplete. "
                                        "Read receipt_reviews.json and the current board. This is the single bounded "
                                        "recovery for that receipt; do not resend original work or ACK peers. "
                                        + self._closeout_policy()
                                    )
                                    recovery = row["recovery"] = {"request": request, "attempts": 0}
                                recovery["attempts"] += 1
                                self.store.save_loop_json_list(path, rows)
                                try:
                                    admitted = enqueue(recovery["request"])
                                    if admitted:
                                        recovery["controller_request_id"] = admitted
                                        row["followthrough_state"] = "recovery_queued"
                                except Exception:
                                    recovery["last_error"] = "admission_unconfirmed"
                        if row.get("followthrough_state") == "needs_attention":
                            row["attention_owner"] = controller.get("agent")
                            row["attention_trigger"] = "existing_controller_or_maintenance_review"
                        else:
                            row.pop("attention_owner", None)
                            row.pop("attention_trigger", None)
                        self.store.save_loop_json_list(path, rows)
            except (OSError, ValueError, TypeError, KeyError):
                logger.exception("Superloop review reconciliation deferred for %s", loop_id)

    @staticmethod
    def _closeout_policy() -> str:
        return (
            "Lead the visible user report with requested outcomes: what now works, what still affects the user, "
            "the actual next action and responsible owner, and the blocker with its release condition. "
            "Do not substitute test counts, commits, queue admission or worker claims for delivery results. "
            "Review the entire taskboard and current worker activity, queues and scheduled work, not only "
            "the triggering task. Take every currently authorized, conflict-free next action now; "
            "a runtime adoption wait must not block independent development or review. Before ending, "
            "record review_verified and a local review_evidence_ref in the ORIGINAL receipt_reviews row, "
            "plus dispositions for every nonterminal task: {task_id, kind: active_dispatch, dispatch_instance_id, evidence_ref} "
            "with a local recent execution observation for existing work; {task_id, kind: action, evidence_ref} for actual work performed; or "
            "{task_id, kind: blocked|deferred, reason, owner, trigger} for a concrete dependency, capacity, "
            "priority or approval wait. Use existing SuperloopStore persistence. Evidence paths are files "
            "relative to this loop. When continuous_supervision_required=true, an action on an unfinished task "
            "also needs a next disposition (active_dispatch or blocked|deferred); every blocked/deferred "
            "disposition needs review_after as absolute UTC epoch seconds using an existing receipt, deadline "
            "or maintenance review, not a new recurring schedule. Expired waits need current reassessment. "
            "A task label, runner state change or promise is not execution evidence. "
            "Verify dispatch execution separately, reuse existing receipt/deadline triggers and do not add "
            "recurring polling. Report user outcomes, unresolved impact and next responsibility visibly. "
            "For completed tasks explicitly marked delivery_required=true, record runtime_adoption_verified "
            "and user_acceptance_verified with matching *_evidence_ref files; when terminal_delivery_required=true, "
            "also record terminal_delivery_verified and terminal_delivery_evidence_ref. Missing delivery evidence "
            "requires reopening the delivery task and taking action or recording a concrete blocker. "
            "Review records never prove runtime adoption or terminal delivery. report_delivery_state=unverified "
            "requires independent canonical Connector delivery_event inspection for the exact controller request. "
            "If the single recovery is exhausted, the existing controller/maintenance review must handle "
            "needs_attention; that status is not itself a delivered user notification."
        )

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
                    "the peer or resend the original assignment. Report progress visibly to the user. "
                    + self._closeout_policy()
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
