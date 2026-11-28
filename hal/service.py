"""HAL v0.1 service: optional runtime loop and Workbench API handlers.

Owned by the existing Workbench API server process (Layer-2 Function). HAL adds
no new process, no supervisor, no scheduler and no forced dependency:

- zero enabled rules  -> no loop task at all (or the loop exits);
- enabled rules       -> one bounded feed check per <=5s (same precedent as the
                          existing HEA long-poll fallback), event-driven wake;
- every execution goes through the SAME authenticated prompt/command adapters
  a connected user uses. HAL never implements a second executor.

All persistence is synchronous SQLite behind ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from aiohttp import web

from hal import compile_hal
from hal.dispatch import DispatchAdapter, DispatchResult, resolve_agent_target, split_agent_spec
from hal.peer import RemoteTerminalUnavailable, handle_hal_peer_request
from hal.event_source import (
    EVENT_WHITEBOARD_STAGE,
    HalTrigger,
    feed_event_hal_event,
    whiteboard_event_trigger,
)
from hal.runtime import (
    EXEC_ACTIVE,
    EXEC_COMPLETED,
    EXEC_FAILED,
    EXEC_UNVERIFIED,
    RECEIPT_DISPATCHED,
    RECEIPT_FAILED,
    exec_context,
    directive_for_exec_stage,
    program_from_json,
    plan_for_trigger,
    entry_rules_match,
    halted_execs_for,
    rework_budget_reason,
    rule_waits_for_whiteboard,
    stage_after_dispatch,
    unavailable_wait_detail,
    validate_script,
    whiteboard_halt_reason,
    whiteboard_stop_is_exceptional,
)
from hal.store import HalStore, HalStoreError

RECEIPT_PENDING = "pending_retry"
RECEIPT_UNKNOWN = "unknown"

POLL_INTERVAL_SECONDS = 5.0
MAX_SCRIPT_BYTES = 64 * 1024
MAX_PAGE_EVENTS = 100

# Bounded transient-prompt retry: a failed dispatch is deferred with an
# exponential backoff and never retried more than a small number of times, so a
# network error cannot become a continuous request storm while the loop waits.
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 60.0
MAX_RETRY_ATTEMPTS = 5


def _retry_backoff(attempts: int) -> float:
    """Exponential backoff in seconds for the Nth failed attempt (N >= 1)."""
    return min(RETRY_BASE_SECONDS * (2.0 ** max(0, attempts - 1)), RETRY_MAX_SECONDS)

SCHEMA_ID = "hashi.hal/v1"


class HalService:
    def __init__(
        self,
        *,
        store: HalStore,
        session_store,
        dispatch: DispatchAdapter,
        logger: logging.Logger | None = None,
        clock: Any = None,
        peer: Any = None,
        whiteboard: Any = None,
    ):
        self.store = store
        self.session_store = session_store
        self.dispatch = dispatch
        self.peer = peer
        # Optional whiteboard stage-transition source. None means this instance
        # has no board: whiteboard rules then refuse to enable rather than
        # silently never firing.
        self.whiteboard = whiteboard
        self.logger = logger or logging.getLogger("hal.runtime")
        self._clock = clock if clock is not None else time.time
        self._loop_task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ API

    def status(self) -> dict[str, Any]:
        enabled = self.store.enabled_rule_count()
        return {
            "ok": True,
            "schema": SCHEMA_ID,
            "available": True,
            "instance_id": getattr(self.session_store, "instance_id", None),
            "enabled_rule_count": enabled,
            "runtime_loop_running": self._loop_task is not None and not self._loop_task.done(),
            "event_availability": {
                event: {
                    "available": available and self._event_source_enabled(event),
                    "source": (
                        reason
                        if self._event_source_enabled(event)
                        else "no whiteboard endpoint configured on this instance"
                    ),
                }
                for event, (available, reason) in _ALL_EVENT_AVAILABILITY.items()
            },
            "whiteboard": {
                "configured": self.whiteboard is not None,
                "url": getattr(self.whiteboard, "base_url", None),
            },
        }

    def validate(self, script: str) -> dict[str, Any]:
        return validate_script(script)

    def upsert_rule(
        self,
        *,
        owner_id: str,
        name: str,
        script: str,
        rule_id: str | None = None,
        expected_revision: int | None = None,
        migration_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        result = validate_script(script)
        if not result["ok"]:
            raise HalStoreError(result["error"] or "invalid HAL script")
        # Idempotent migration create: a repeated create for the same source HEA
        # rule + target instance returns the SAME draft unchanged — it never
        # bumps revision, overwrites user edits, or disables an enabled rule.
        if rule_id is None and migration_key:
            existing = self.store.find_rule_by_migration_key(owner_id, migration_key)
            if existing is not None:
                return existing, False
        rule, created = self.store.upsert_rule(
            owner_id=owner_id,
            name=name,
            script=script,
            rule_id=rule_id,
            expected_revision=expected_revision,
            migration_key=migration_key,
        )
        if not created:
            # Saving a modification returns the rule to draft (disabled); settle
            # any in-flight executions so they don't resume under the new script.
            self.store.mark_rule_execs_disabled(
                owner_id, rule["rule_id"], "rule saved as a new draft; execution settled"
            )
        return rule, created

    async def enable_rule(
        self,
        *,
        owner_id: str,
        rule_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Enable a rule whose board baseline must be read live.

        The only network read on the enable path is awaited here, so the API
        request handler never blocks the event loop while a (possibly slow)
        board answers. The value is then handed to the atomic enable
        transaction, which keeps the cursor and the enabled state in ONE write.
        """
        rule = self.store.get_rule(owner_id, rule_id)
        if rule is None:
            raise HalStoreError("rule not found")
        # Converge the conflict check before the network read: a stale write
        # must never spend a board round-trip, and must never move a baseline.
        if expected_revision is not None and int(rule["revision"]) != expected_revision:
            raise HalStoreError("rule revision conflict; refresh before saving")
        wb_baseline: int | None = None
        if _script_has_whiteboard_entry(rule["script"]):
            if self.whiteboard is None:
                raise HalStoreError(
                    "this rule waits for whiteboard stage transitions, but no "
                    "whiteboard endpoint is configured"
                )
            try:
                wb_baseline = int(await self.whiteboard.head_seq())
            except Exception as exc:  # noqa: BLE001
                raise HalStoreError(
                    f"cannot establish whiteboard enable baseline: {exc}"
                ) from exc
        return self.set_enabled(
            owner_id=owner_id,
            rule_id=rule_id,
            enabled=True,
            expected_revision=expected_revision,
            wb_baseline=wb_baseline,
        )

    def set_enabled(
        self,
        *,
        owner_id: str,
        rule_id: str,
        enabled: bool,
        expected_revision: int | None = None,
        wb_baseline: int | None = None,
    ) -> dict[str, Any]:
        """Flip a rule's enabled state in one transaction (no network here).

        Enabling a whiteboard rule needs the board's CURRENT sequence as its
        baseline. That read is deliberately NOT done here: this method is
        synchronous and may be called from a running event loop, where a
        blocking HTTP read would stall every other task (the whole Workbench
        API). ``enable_rule`` awaits the read and passes it in; a sync caller
        without a baseline is refused instead of silently blocking.
        """
        rule = self.store.get_rule(owner_id, rule_id)
        if rule is None:
            raise HalStoreError("rule not found")
        # Converge the revision conflict check here, BEFORE any baseline or
        # enabled-state mutation: a stale write must never move an existing
        # rule's event baseline.
        if expected_revision is not None and int(rule["revision"]) != expected_revision:
            raise HalStoreError("rule revision conflict; refresh before saving")
        if not enabled:
            updated = self.store.set_enabled(
                owner_id, rule_id, enabled, expected_revision=expected_revision
            )
            # Explicit, consistent settle for unfinished executions: disabling a
            # rule never leaves its in-flight workflows silently active.
            self.store.mark_rule_execs_disabled(
                owner_id, rule_id, "rule disabled while execution in progress"
            )
            self._note_rules_changed()
            return updated

        # --- enable path ---
        result = validate_script(rule["script"])
        if not result["ok"]:
            raise HalStoreError(f"stored script no longer compiles: {result['error']}")
        unavailable = [
            d["message"]
            for d in result["diagnostics"]
            if d["kind"] == "entry_event_unavailable"
        ]
        if unavailable:
            raise HalStoreError(
                "entry event has no real source yet; " + "; ".join(unavailable)
            )

        # Reading the latest feed cursor is REQUIRED, never best-effort: a stale
        # owner cursor must not be used as an unsafe fallback. If the feed is
        # unavailable, fail the enable and keep the rule disabled (its baseline
        # and the existing rules' cursors untouched).
        try:
            page = _feed_page(self.session_store, owner_id, -1, 1)
        except Exception as exc:  # noqa: BLE001
            raise HalStoreError(
                f"cannot establish enable baseline (feed unavailable): {exc}"
            ) from exc
        baseline = int(page["next_cursor"])
        if baseline < 0:
            baseline = 0

        # A rule that waits for whiteboard transitions needs the SAME guarantee
        # on the board sequence: the position at enable time, read live by
        # ``enable_rule`` and handed over here. If it could not be read, the
        # enable fails instead of replaying history.
        # Turning a rule OFF must never depend on the board being reachable:
        # during a board outage the operator still has to be able to stop
        # automation. Only an enable needs the live baseline.
        if enabled and _script_has_whiteboard_entry(rule["script"]):
            if self.whiteboard is None:
                raise HalStoreError(
                    "this rule waits for whiteboard stage transitions, but no "
                    "whiteboard endpoint is configured"
                )
            if wb_baseline is None:
                raise HalStoreError(
                    "enabling a whiteboard rule needs the board baseline; use "
                    "enable_rule() so the read never blocks the event loop"
                )
            if not isinstance(wb_baseline, int) or isinstance(wb_baseline, bool):
                raise HalStoreError("whiteboard enable baseline must be an integer")

        # Atomic enable: revision check, per-rule enable baseline, enabled-state
        # update and the initial owner-cursor anchor happen in ONE transaction.
        updated = self.store.enable_rule(
            owner_id=owner_id,
            rule_id=rule_id,
            expected_revision=expected_revision,
            baseline_cursor=baseline,
            wb_baseline_cursor=wb_baseline,
        )
        if updated is None:
            raise HalStoreError("rule not found")
        self._note_rules_changed()
        return updated

    def list_rules(self, owner_id: str) -> list[dict[str, Any]]:
        rows = []
        for rule in self.store.list_rules(owner_id):
            public = {
                "rule_id": rule["rule_id"],
                "name": rule["name"],
                "script": rule["script"],
                "revision": rule["revision"],
                "enabled": bool(rule["enabled"]),
                "created_at": rule["created_at"],
                "updated_at": rule["updated_at"],
            }
            public["validation"] = {
                key: value for key, value in validate_script(rule["script"]).items()
            }
            rows.append(public)
        return rows

    def list_runs(
        self, owner_id: str, limit: int = 50, rule_id: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "exec_id": row["exec_id"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "rule_revision": row["rule_revision"],
                "workflow_index": row["workflow_index"],
                "stage": row["stage"],
                "state": row["state"],
                "trigger_event_id": row["trigger_event_id"],
                "trigger_agent": row["trigger_agent"],
                "trigger_kind": row["trigger_kind"],
                "detail": row["detail"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in self.store.list_execs(owner_id, limit=limit, rule_id=rule_id)
        ]

    def run_detail(self, owner_id: str, exec_id: str) -> dict[str, Any] | None:
        row = self.store.get_exec(owner_id, exec_id)
        if row is None:
            return None
        detail = dict(row)
        detail["receipts"] = self.store.list_receipts(exec_id)
        for receipt in detail["receipts"]:
            if receipt["kind"] == "prompt":
                _, target_instance = split_agent_spec(receipt.get("target"))
                receipt["target_instance"] = target_instance or self._current_instance_id()
        return detail

    # ------------------------------------------------------------------ loop

    def start_loop(self) -> None:
        if self._stopping:
            return
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.get_running_loop().create_task(self._run_loop())

    def stop_loop(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._loop_task
        if task is not None and not task.done():
            task.cancel()

    def _note_rules_changed(self) -> None:
        if self._stopping:
            return
        try:
            self._wake.set()
        except RuntimeError:
            pass
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # synchronous caller (tests): no loop to start
        if self.store.enabled_rule_count() > 0:
            self.start_loop()

    async def _run_loop(self) -> None:
        self.logger.info("HAL runtime loop started")
        try:
            while not self._stopping:
                owners = await asyncio.to_thread(self.store.enabled_owner_ids)
                if not owners:
                    self.logger.info("HAL runtime loop exiting: no enabled rules")
                    return
                activity = False
                for owner in owners:
                    if await self._process_owner(owner):
                        activity = True
                try:
                    if activity:
                        await asyncio.wait_for(self._wake.wait(), timeout=0.1)
                    else:
                        await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._wake.clear()
        except asyncio.CancelledError:
            raise
        finally:
            self._loop_task = None

    async def _process_owner(self, owner_id: str) -> bool:
        """One bounded pass over every configured source for this owner.

        The two sources are independent: a whiteboard outage cannot stall the
        Run feed and vice versa, and each keeps its own cursor.
        """
        # The API can listen before the shared Function generation commits its
        # Worker routes. Keep the cursor intact until those routes are ready.
        if not self.dispatch.ready_for_events():
            return False
        feed_activity = await self._process_feed(owner_id)
        try:
            board_activity = await self._process_whiteboard(owner_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the board must never kill the loop
            self.logger.warning("HAL whiteboard pass failed for %s: %s", owner_id, exc)
            board_activity = False
        return feed_activity or board_activity

    async def _process_feed(self, owner_id: str) -> bool:
        """One bounded feed check + deterministic planning + idempotent apply."""
        activity = False
        try:
            cursor = await asyncio.to_thread(self.store.get_cursor, owner_id)
            page = await asyncio.to_thread(
                _feed_page, self.session_store, owner_id, cursor, MAX_PAGE_EVENTS
            )
            events = page.get("events") or []
            new_cursor = page.get("next_cursor", cursor)
            has_more = bool(page.get("has_more"))
            reset = bool(page.get("reset_required"))
        except Exception as exc:  # noqa: BLE001 — feed outage must never kill the loop
            self.logger.warning("HAL feed read failed for %s: %s", owner_id, exc)
            return False

        if cursor < 0 or reset:
            # Bootstrap: adopt the current feed position. Enabling HAL never
            # replays pre-enable history.
            await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)
            return False

        rules = await self._load_enabled_rules(owner_id)
        if not rules:
            await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)
            return False

        execs = await asyncio.to_thread(self.store.list_active_execs, owner_id)
        for row in execs:
            try:
                row["program"] = program_from_json(json.loads(row["program_blob"]))
            except (ValueError, KeyError, TypeError):
                row["program"] = None
        execs = [row for row in execs if row.get("program") is not None]
        execs_by_id = {str(e["exec_id"]): e for e in execs}
        receipts = await self._load_receipts(execs)
        consumed: dict[str, set[str]] = {}

        if not events and not has_more:
            # No new evidence: recover pending/unfinished executions so a
            # transient failure (or a crash) is retried without waiting for the
            # next event. The cursor still advances past this empty page.
            # Returning the recovery result (True only when a dispatch was
            # actually attempted) keeps a normal "waiting for a correlated
            # event" state out of the 0.1s fast loop.
            await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)
            return await self._recover_executions(owner_id, rules, execs_by_id, receipts)

        # Recover BEFORE matching any event. A crash between the receipt write
        # and the stage advance leaves a dispatched-but-not-advanced stage whose
        # correlated completion event may already be persisted in this feed page.
        # Repair the stage first so the event matches the correct (advanced)
        # stage — never the stale one — and a dispatched stage is never
        # re-executed. The cursor is advanced only after this repair.
        if await self._recover_executions(owner_id, rules, execs_by_id, receipts):
            activity = True
        # Reload so recovery's stage advances are visible to this page's
        # planning; the same page then sees one consistent execution snapshot.
        execs = await asyncio.to_thread(self.store.list_active_execs, owner_id)
        for row in execs:
            try:
                row["program"] = program_from_json(json.loads(row["program_blob"]))
            except (ValueError, KeyError, TypeError):
                row["program"] = None
        execs = [row for row in execs if row.get("program") is not None]
        execs_by_id = {str(e["exec_id"]): e for e in execs}
        receipts = await self._load_receipts(execs)

        for event in events:
            trigger = feed_event_hal_event(event)
            if trigger is not None and trigger.agent_identity:
                # Canonical HAL agent identity: @name@instance (spec 2.2).
                instance_id = getattr(self.session_store, "instance_id", "") or ""
                suffix = f"@{instance_id}" if instance_id else ""
                trigger = _with_agent_identity(trigger, f"@{trigger.agent_identity}{suffix}")
            if trigger is not None:
                if await self._apply_trigger(
                    owner_id, trigger, rules, execs_by_id, receipts, consumed
                ):
                    activity = True

        await asyncio.to_thread(self.store.set_cursor, owner_id, new_cursor)

        # Reload after applying this cycle's intents so freshly created
        # executions with pending dispatches are retried in the same cycle.
        fresh_execs = await asyncio.to_thread(self.store.list_active_execs, owner_id)
        fresh_by_id = {str(e["exec_id"]): e for e in fresh_execs}
        fresh_receipts = await self._load_receipts(fresh_execs)
        recovered = await self._recover_executions(owner_id, rules, fresh_by_id, fresh_receipts)
        # A reported feed backlog (has_more) keeps the loop consuming pages
        # immediately instead of idling POLL_INTERVAL between non-matching pages.
        return activity or recovered or has_more

    def _event_source_enabled(self, event: str) -> bool:
        """A board event is only available when this instance has a board."""
        if event == EVENT_WHITEBOARD_STAGE and self.whiteboard is None:
            return False
        return True

    async def _process_whiteboard(self, owner_id: str) -> bool:
        """One bounded board read per pass, with its own cursor."""
        if self.whiteboard is None:
            return False
        rules = await self._load_enabled_rules(owner_id)
        if not any(_rule_waits_for_whiteboard(rule["program"]) for rule in rules):
            return False
        try:
            cursor = await asyncio.to_thread(self.store.get_wb_cursor, owner_id)
            page = await self.whiteboard.fetch_events(cursor, MAX_PAGE_EVENTS)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("HAL whiteboard read failed for %s: %s", owner_id, exc)
            return False
        new_cursor = int(page["next_cursor"])
        if cursor < 0:
            # Bootstrap: adopt the board's current position. Enabling HAL never
            # replays transitions that happened before enablement.
            await asyncio.to_thread(self.store.set_wb_cursor, owner_id, new_cursor)
            return False
        if page.get("gap"):
            # The board pruned past this cursor. The skipped transitions cannot
            # be replayed, and one of them may have been a stop: recover from
            # the cards' own current state (the board's contract for a gap)
            # instead of trusting a feed that no longer holds the evidence.
            self.logger.warning(
                "HAL whiteboard cursor gap for %s: %s", owner_id, page["gap"]
            )
            await self._resync_after_gap(owner_id)
        activity = False
        for event in page["events"]:
            trigger = whiteboard_event_trigger(event)
            if trigger is None:
                continue
            if await self._apply_whiteboard_trigger(owner_id, trigger, rules):
                activity = True
        await asyncio.to_thread(self.store.set_wb_cursor, owner_id, new_cursor)
        return activity or bool(page["has_more"])

    async def _apply_whiteboard_trigger(
        self, owner_id: str, trigger: HalTrigger, rules: list[dict[str, Any]]
    ) -> bool:
        """Stop the chain when the board says so, otherwise plan normally."""
        context = dict(trigger.context or {})
        # The guard verdict is read from the board's own stage table. Its reason
        # is kept verbatim: "the table could not be read", "this stage is
        # human-gated" and "this rework is over budget" are different facts and
        # must not be merged into one.
        guard_reason, guard_exceptional = await self._board_guard(context)
        if guard_reason and "human decision" in guard_reason:
            context["human_gate"] = True
        trigger = dataclasses.replace(trigger, context=context)

        active = await self._load_active_execs(owner_id)
        reason = guard_reason or whiteboard_halt_reason(trigger)
        if reason:
            # The resolved reason is authoritative: the budget and the stage
            # table are judged here, so a stop this helper cannot re-derive must
            # still settle the live executions instead of only being announced.
            halts = halted_execs_for(trigger, list(active.values()), reason=reason)
            would_start = entry_rules_match(
                trigger, rules, current_instance=self._current_instance_id() or None
            )
            if (
                not halts
                and not would_start
                and not guard_exceptional
                and not whiteboard_stop_is_exceptional(trigger)
            ):
                # Nothing was running, nothing would have started and the board
                # did not take a protective action: a human-gated move nobody
                # automated is ordinary traffic, not an automation stop.
                return False
            stopped = 0
            for halt in halts:
                if await asyncio.to_thread(
                    self.store.fail_exec_if_active,
                    owner_id=owner_id,
                    exec_id=halt.exec_id,
                    detail=f"stopped by the board: {reason}",
                ):
                    stopped += 1
            await self._notify_stop(owner_id, context, reason)
            self.logger.info(
                "HAL stopped %s execution(s) for card %s: %s",
                stopped, context.get("card_id"), reason,
            )
            return stopped > 0

        receipts = await self._load_receipts(list(active.values()))
        consumed: dict[str, set[str]] = {}
        return await self._apply_trigger(
            owner_id, trigger, rules, active, receipts, consumed
        )

    async def _load_active_execs(self, owner_id: str) -> dict[str, dict[str, Any]]:
        execs = await asyncio.to_thread(self.store.list_active_execs, owner_id)
        for row in execs:
            try:
                row["program"] = program_from_json(json.loads(row["program_blob"]))
            except (ValueError, KeyError, TypeError):
                row["program"] = None
        return {
            str(row["exec_id"]): row
            for row in execs
            if row.get("program") is not None
        }

    async def _board_guard(self, context: Mapping[str, Any]) -> tuple[str | None, bool]:
        """Reasons the board's own stage table forbids continuing here.

        Returns ``(reason, exceptional)``. The table decides two things HAL must
        not re-derive: whether the destination is human-gated, and whether this
        move spends a rework round the budget cannot afford. A forward move at
        the budget boundary is legal, so only rework destinations are checked.
        """
        project = str(context.get("project") or "")
        stage = str(context.get("to_stage") or "")
        if not stage:
            return None, False
        try:
            stages = await self.whiteboard.stages(project)
        except Exception as exc:  # noqa: BLE001
            # Firing work into a stage we cannot classify would bypass a gate we
            # simply failed to see, so an unreadable table stops the chain.
            return f"whiteboard stage table unavailable ({exc}); refusing to auto-advance", True
        entry = stages.get(stage)
        if entry is None:
            return None, False
        if entry.get("human_gate"):
            return f"stage {stage} requires a human decision", False
        budget = rework_budget_reason(
            stage, entry, context.get("round"), context.get("max_rounds")
        )
        if budget:
            return budget, True
        return None, False

    async def _card_stop_state(
        self, card: Mapping[str, Any], project: str
    ) -> tuple[str | None, bool]:
        """(stop reason, confirmed) from the card's CURRENT board projection.

        The card document already carries the board's own ``stage_category``,
        ``stage``, ``round`` and effective ``max_rounds``, projected with the
        project's definition at read time, so a fused card is visible without a
        second round-trip. Two facts are not carried by the card — the human
        gate flag and whether its current stage spends a rework round — so the
        stage table is read for them; when that read fails the answer is "not
        confirmed" and the caller HOLDS the chain instead of guessing.

        The budget is judged by the same ``rework_budget_reason`` rule the
        transition-event guard uses: a card the board moved into its last
        rework round (``changes_requested`` at ``round == max_rounds``) must
        not receive one more remediation, whether the stop is noticed by the
        event, by the pre-dispatch check or by the post-gap resync. A forward
        stage at the same round stays dispatchable — it spends no round.
        """
        stage = str(card.get("stage") or "")
        if not stage:
            return None, True
        if str(card.get("stage_category") or "") == "halted":
            return f"card is on halted stage {stage}", True
        try:
            stages = await self.whiteboard.stages(project)
        except Exception as exc:  # noqa: BLE001
            return f"whiteboard stage table unavailable ({exc})", False
        entry = stages.get(stage)
        if entry is not None and entry.get("human_gate"):
            return f"stage {stage} requires a human decision", True
        budget = rework_budget_reason(
            stage, entry, card.get("round"), card.get("max_rounds")
        )
        if budget:
            return budget, True
        return None, True

    async def _card_allows_dispatch(
        self, owner_id: str, exec_row: Mapping[str, Any]
    ) -> bool:
        """May this execution dispatch now? The card, not the feed, decides.

        The Run feed is processed before the board on every pass, so a reviewer
        finishing in the same pass in which the board fused the card would
        otherwise dispatch the remediation first and only then process the stop.
        Holding when the board cannot answer keeps the chain intact (its
        receipt/stage are untouched) and retries on a later pass.
        """
        if self.whiteboard is None:
            return True
        context = exec_context(exec_row) or {}
        card_id = str(context.get("card_id") or "")
        if not card_id:
            return True  # an independent Run rule owns no card and is untouched
        try:
            card = await self.whiteboard.card(card_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "HAL holding card %s chain: card state unreadable (%s)", card_id, exc
            )
            return False
        project = str(card.get("project") or context.get("project") or "")
        reason, confirmed = await self._card_stop_state(card, project)
        if reason is None:
            return True
        if not confirmed:
            self.logger.warning("HAL holding card %s chain: %s", card_id, reason)
            return False
        await self._stop_card_chain(owner_id, card_id, reason, card=card)
        return False

    async def _stop_card_chain(
        self,
        owner_id: str,
        card_id: str,
        reason: str,
        *,
        card: Mapping[str, Any] | None = None,
    ) -> int:
        """Settle every active execution on this card and report the stop once.

        The card's CURRENT revision names the stop: the transition event for the
        same move carries that revision, so a stop first noticed by the
        pre-dispatch check (Run feed read before the board) and then seen again
        on the event is recorded and posted once, not twice.
        """
        active = await self._load_active_execs(owner_id)
        stopped = 0
        context: Mapping[str, Any] = {}
        for exec_row in active.values():
            row_context = exec_context(exec_row) or {}
            if str(row_context.get("card_id") or "") != card_id:
                continue
            if not context:
                context = row_context
            if await asyncio.to_thread(
                self.store.fail_exec_if_active,
                owner_id=owner_id,
                exec_id=str(exec_row["exec_id"]),
                detail=f"stopped by the board: {reason}",
            ):
                stopped += 1
        stop_context = {**dict(context), "card_id": card_id}
        if card is not None:
            # The notice quotes the card as it is NOW, not the stage that
            # dispatched this execution: the stop is about the card's current
            # round and stage.
            if card.get("revision") is not None:
                stop_context["revision"] = card["revision"]
            if card.get("stage"):
                stop_context["to_stage"] = card["stage"]
            if isinstance(card.get("round"), int):
                stop_context["round"] = card["round"]
            if isinstance(card.get("max_rounds"), int):
                stop_context["max_rounds"] = card["max_rounds"]
        await self._notify_stop(owner_id, stop_context, reason)
        self.logger.info(
            "HAL stopped %s execution(s) for card %s: %s", stopped, card_id, reason
        )
        return stopped

    async def _resync_after_gap(self, owner_id: str) -> None:
        """A pruned window may have hidden a stop: re-derive it from the cards.

        The board's own contract for a stale cursor is "read the current card
        state". If the board cannot be read the cursor still advances (the
        skipped window is unrecoverable), and every card chain is then held at
        dispatch time until the board answers again.
        """
        try:
            cards = await self.whiteboard.cards()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "HAL whiteboard resync unreadable for %s: %s", owner_id, exc
            )
            return
        active = await self._load_active_execs(owner_id)
        seen: set[str] = set()
        for exec_row in active.values():
            context = exec_context(exec_row) or {}
            card_id = str(context.get("card_id") or "")
            card = cards.get(card_id) if card_id else None
            if card is None or card_id in seen:
                continue
            seen.add(card_id)
            project = str(card.get("project") or context.get("project") or "")
            reason, confirmed = await self._card_stop_state(card, project)
            if reason is None or not confirmed:
                continue
            await self._stop_card_chain(owner_id, card_id, reason, card=card)

    async def _notify_stop(
        self, owner_id: str, context: Mapping[str, Any], reason: str
    ) -> None:
        """Record the stop once, then try to put it in front of the humans."""
        card_id = str(context.get("card_id") or "") or None
        revision = context.get("revision")
        detail = (
            f"自动接力已停止：{reason}。卡片 {card_id or '?'}"
            f"（{context.get('project') or '?'}，{context.get('from_stage') or '?'}"
            f" → {context.get('to_stage') or '?'}，"
            f"第 {context.get('round') or '?'}/{context.get('max_rounds') or '?'} 轮）"
        )
        notice_id = f"hal:{card_id}:rev:{revision}:halted" if card_id else None
        created = await asyncio.to_thread(
            self.store.add_notice,
            owner_id=owner_id,
            card_id=card_id,
            kind="halted",
            detail=detail,
            notice_id=notice_id,
        )
        if not created:
            return  # this exact stop was already reported
        if self.whiteboard is None or not card_id:
            return
        try:
            posted, info = await self.whiteboard.post_comment(
                card_id, f"**HAL 停止自动接力**：{detail}"
            )
        except Exception as exc:  # noqa: BLE001 — a failed comment never hides the stop
            posted, info = False, f"{type(exc).__name__}: {exc}"
        await asyncio.to_thread(
            self.store.mark_notice_delivered,
            notice_id,
            info if posted else f"not delivered: {info}",
        )

    async def _load_enabled_rules(self, owner_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self.store.list_rules, owner_id)
        loaded = []
        for row in rows:
            if not row["enabled"]:
                continue
            try:
                program = compile_hal(row["script"])
            except Exception as exc:  # noqa: BLE001 — stored scripts must not crash the loop
                self.logger.error("HAL rule %s failed to compile: %s", row["rule_id"], exc)
                continue
            row["program"] = program
            loaded.append(row)
        return loaded

    async def _load_receipts(self, execs: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
        receipts: dict[tuple[str, int], dict[str, Any]] = {}
        for exec_row in execs:
            for receipt in await asyncio.to_thread(self.store.list_receipts, exec_row["exec_id"]):
                receipts[(str(receipt["exec_id"]), int(receipt["stage"]))] = receipt
        return receipts

    async def _apply_trigger(
        self,
        owner_id: str,
        trigger: HalTrigger,
        rules: list[dict[str, Any]],
        execs_by_id: dict[str, dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
        consumed: dict[str, set[str]],
    ) -> bool:
        activity = False
        creates, advances, hints = await asyncio.to_thread(
            plan_for_trigger,
            trigger,
            rules,
            list(execs_by_id.values()),
            list(receipts.values()),
            _flatten_consumed(consumed),
            current_instance=self._current_instance_id() or None,
        )
        # A rule disabled concurrently (between the rule load and this dispatch)
        # must not fire again. Re-check the store's enabled state for exactly the
        # rules the planner touched; this closes the plan->dispatch race window.
        advance_rule_ids = {
            str(execs_by_id[a.exec_id]["rule_id"])
            for a in advances
            if a.exec_id in execs_by_id
        }
        intent_rule_ids = {c.rule_id for c in creates} | advance_rule_ids
        if intent_rule_ids:
            fresh_rows = await asyncio.to_thread(self.store.list_rules, owner_id)
            enabled_ids = {str(r["rule_id"]) for r in fresh_rows if r["enabled"]}
            creates = [c for c in creates if c.rule_id in enabled_ids]
            advances = [
                a for a in advances
                if a.exec_id in execs_by_id
                and str(execs_by_id[a.exec_id]["rule_id"]) in enabled_ids
            ]
        for hint in hints:
            await asyncio.to_thread(
                self.store.update_exec, hint.exec_id, detail=hint.detail
            )
        for intent in creates:
            # Idempotent per (rule, event): a replay after crash reuses the row.
            created = await asyncio.to_thread(
                self.store.create_exec,
                owner_id=owner_id,
                rule_id=intent.rule_id,
                rule_revision=intent.rule_revision,
                rule_name=intent.rule_name,
                workflow_index=intent.workflow_index,
                exec_id=intent.exec_id,
                trigger_event_id=intent.trigger_event_id,
                trigger_agent=intent.trigger_agent,
                trigger_kind=intent.trigger_kind,
                program_blob=_program_for_rule(rules, intent.rule_id),
                detail=None,
                context_blob=(
                    json.dumps(dict(intent.context), ensure_ascii=False)
                    if intent.context is not None else None
                ),
            )
            exec_row = await asyncio.to_thread(self.store.get_exec, owner_id, intent.exec_id)
            if exec_row is None:
                continue
            if created is False:
                # Replayed create (idempotency hit): never blindly re-dispatch.
                # Reconcile from the PERSISTED execution state and receipt so a
                # completed/failed/disabled/unverified run is never re-run and
                # its state/receipt is never overwritten.
                await self._reconcile(owner_id, exec_row, rules, receipts)
                continue
            if await self._dispatch_stage(owner_id, exec_row, intent.directive, rules, receipts):
                activity = True
        for intent in advances:
            if intent.exec_id not in execs_by_id:
                continue
            exec_row = execs_by_id[intent.exec_id]
            if int(exec_row["stage"]) != intent.stage:
                continue
            # Dispatch FIRST (writes the pre-state receipt) and only then mark the
            # event consumed. A crash between the two leaves the receipt behind, so
            # re-delivery of the event re-dispatches idempotently (prompt) or fails
            # honestly (command) instead of silently dropping the stage.
            if await self._dispatch_stage(owner_id, exec_row, intent.directive, rules, receipts):
                activity = True
            await asyncio.to_thread(
                self.store.mark_exec_event_consumed, intent.exec_id, intent.trigger_event_id
            )
        return activity

    def _current_instance_id(self) -> str:
        return str(getattr(self.session_store, "instance_id", "") or "")

    async def _prompt_result(
        self, owner_id: str, exec_row: Mapping[str, Any], directive, exec_id: str, stage: int
    ) -> DispatchResult:
        target = directive.target
        if target == "@agent_trigger":
            target = exec_row.get("trigger_agent") or None
            if not target:
                return DispatchResult(
                    accepted=False,
                    transient=False,
                    detail="prompt target @agent_trigger has no Agent for this system event",
                )
        target_name, instance = split_agent_spec(target)
        current = self._current_instance_id().casefold()
        if target_name and instance and instance != current and self.peer is not None:
            if owner_id.startswith("enterprise:"):
                return DispatchResult(False, False, "cross-instance HAL requires the instance owner")
            return await self.peer.dispatch_prompt(
                instance, target_name, directive.payload, directive.idempotency_key,
            )
        name, error = resolve_agent_target(target, current)
        if error:
            return DispatchResult(accepted=False, transient=False, detail=error)
        return await self.dispatch.dispatch_prompt(
            target_agent=name,
            text=directive.payload,
            idempotency_key=directive.idempotency_key,
            exec_id=exec_id,
            stage=stage,
        )

    async def _command_result(
        self, exec_row: Mapping[str, Any], directive
    ) -> DispatchResult:
        trigger_agent = exec_row.get("trigger_agent")
        trigger_name = None
        if trigger_agent:
            name, error = resolve_agent_target(trigger_agent, self._current_instance_id())
            if error:
                return DispatchResult(
                    accepted=False,
                    transient=False,
                    detail=f"cannot resolve command runtime: {error}",
                )
            trigger_name = name
        return await self.dispatch.dispatch_command(
            command_text=directive.payload,
            trigger_agent=trigger_name,
        )

    async def _exec_still_active(self, owner_id: str, exec_id: str) -> bool:
        """Re-read persisted execution state (single source of truth)."""
        fresh = await asyncio.to_thread(self.store.get_exec, owner_id, exec_id)
        return fresh is not None and str(fresh.get("state")) == EXEC_ACTIVE

    async def _rule_enabled(self, owner_id: str, rule_id: str) -> bool:
        rule = await asyncio.to_thread(self.store.get_rule, owner_id, rule_id)
        return rule is not None and bool(rule.get("enabled"))

    async def _dispatch_stage(
        self,
        owner_id: str,
        exec_row: Mapping[str, Any],
        directive,
        rules: list[dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
    ) -> bool:
        """Dispatch one stage once; crash-safe and never double-executing."""
        # One choke point for the board's veto: creates, dependent advances and
        # every recovery retry all pass through here, so a card the board has
        # already stopped can never receive one more prompt — regardless of
        # whether the Run feed or the board pass happens to run first.
        if not await self._card_allows_dispatch(owner_id, exec_row):
            return False
        exec_id = directive.exec_id
        stage = directive.stage
        rule_id = str(exec_row["rule_id"])
        existing = receipts.get((exec_id, stage))
        if existing is not None and existing["state"] in (RECEIPT_DISPATCHED, RECEIPT_FAILED):
            return False
        if existing is not None and existing["state"] == RECEIPT_UNKNOWN and directive.kind == "native_command":
            # Ambiguous after an interruption: never re-run a command.
            await asyncio.to_thread(
                self.store.upsert_receipt,
                exec_id=exec_id,
                stage=stage,
                idempotency_key=existing["idempotency_key"],
                kind=directive.kind,
                target=existing["target"],
                payload=existing["payload"],
                state=RECEIPT_FAILED,
                detail="command outcome unknown after interruption; manual verification advised",
            )
            await asyncio.to_thread(
                self.store.fail_exec_if_active, owner_id=owner_id, exec_id=exec_id,
                detail="native command outcome unknown after interruption",
            )
            return False

        if directive.unbound_reason:
            await asyncio.to_thread(
                self.store.upsert_receipt,
                exec_id=exec_id,
                stage=stage,
                idempotency_key=directive.idempotency_key,
                kind=directive.kind,
                target=directive.target,
                payload=directive.payload,
                state=RECEIPT_FAILED,
                detail=directive.unbound_reason,
            )
            await asyncio.to_thread(
                self.store.fail_exec_if_active, owner_id=owner_id, exec_id=exec_id,
                detail=directive.unbound_reason,
            )
            return False

        # Atomic admission: rule-enabled + exec-active + pre-state receipt in
        # ONE transaction. A concurrent disable can no longer land between the
        # state check and the receipt write, so "disable returned" implies "no
        # new side effect will be admitted".
        pre_state = RECEIPT_PENDING if directive.kind == "prompt" else RECEIPT_UNKNOWN
        admission = await asyncio.to_thread(
            self.store.admit_stage_dispatch,
            owner_id=owner_id,
            rule_id=rule_id,
            exec_id=exec_id,
            stage=stage,
            idempotency_key=directive.idempotency_key,
            kind=directive.kind,
            target=directive.target,
            payload=directive.payload,
            pre_state=pre_state,
        )
        if admission != "admitted":
            return False

        if directive.kind == "prompt":
            result = await self._prompt_result(owner_id, exec_row, directive, exec_id, stage)
        else:
            result = await self._command_result(exec_row, directive)

        if result.accepted:
            # Preserve the true dispatch receipt even if a concurrent disable
            # settled the execution while the adapter was in flight.
            await asyncio.to_thread(
                self.store.upsert_receipt,
                exec_id=exec_id,
                stage=stage,
                idempotency_key=directive.idempotency_key,
                kind=directive.kind,
                target=directive.target,
                payload=directive.payload,
                state=RECEIPT_DISPATCHED,
                detail=result.detail,
            )
            program = program_from_json(json.loads(exec_row["program_blob"]))
            workflow = program.workflows[int(exec_row["workflow_index"])]
            next_stage, state = stage_after_dispatch(stage, len(workflow))
            detail = None
            if directive.kind == "native_command" and state == EXEC_COMPLETED:
                # A native command being accepted is NOT proof that its effect
                # (a reboot or deploy) finished. v0.1 has no reliable completion
                # event source for these, so report the honest terminal state
                # instead of claiming "completed".
                state = EXEC_UNVERIFIED
                detail = "command accepted; completion not verified"
            elif state == EXEC_ACTIVE and next_stage < len(workflow):
                detail = unavailable_wait_detail(workflow[next_stage].event)
            # Advance only if STILL active; never overwrite a concurrent settle.
            await asyncio.to_thread(
                self.store.advance_exec_if_active,
                owner_id=owner_id,
                exec_id=exec_id,
                stage=next_stage,
                state=state,
                detail=detail,
            )
            return True

        # Failure: transient (prompt only) keeps the same idempotency key for
        # retry; permanent failures stop the workflow honestly.
        final_state = RECEIPT_PENDING if result.transient else RECEIPT_FAILED
        existing_attempts = 0
        if existing is not None:
            existing_attempts = int(existing.get("attempts") or 0)
        attempt_number = existing_attempts + 1
        attempts = None
        next_retry_at = None
        if result.transient:
            attempts = attempt_number
            next_retry_at = self._clock() + _retry_backoff(attempts)
        # Every failed dispatch is logged with enough context to diagnose it
        # later even if the persisted receipt is overwritten.
        self.logger.warning(
            "HAL dispatch failed exec=%s stage=%s kind=%s target=%s attempt=%s "
            "retryable=%s exception_type=%s detail=%s",
            exec_id,
            stage,
            directive.kind,
            directive.target,
            attempt_number,
            result.transient,
            (result.evidence or {}).get("exception_type"),
            result.detail,
        )
        await asyncio.to_thread(
            self.store.upsert_receipt,
            exec_id=exec_id,
            stage=stage,
            idempotency_key=directive.idempotency_key,
            kind=directive.kind,
            target=directive.target,
            payload=directive.payload,
            state=final_state,
            detail=result.detail,
            attempts=attempts,
            next_retry_at=next_retry_at,
        )
        if not result.transient:
            await asyncio.to_thread(
                self.store.fail_exec_if_active, owner_id=owner_id, exec_id=exec_id,
                detail=result.detail,
            )
        return False

    async def _reconcile(
        self,
        owner_id: str,
        exec_row: Mapping[str, Any],
        rules: list[dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        """Repair stage/state divergence after a crash between writes."""
        if str(exec_row.get("state")) != EXEC_ACTIVE:
            # Terminal/settled executions (completed, failed, disabled,
            # unverified) must never be re-run or have their state overwritten
            # by a replayed trigger.
            return
        # Re-read persisted state: a concurrent disable/settle must never be
        # overwritten by a stale 'active' snapshot.
        if not await self._exec_still_active(owner_id, exec_row["exec_id"]):
            return
        stage = int(exec_row["stage"])
        program = program_from_json(json.loads(exec_row["program_blob"]))
        workflow = program.workflows[int(exec_row["workflow_index"])]
        if stage >= len(workflow):
            return
        receipt = await asyncio.to_thread(self.store.get_receipt, exec_row["exec_id"], stage)
        if receipt is None or receipt["state"] == RECEIPT_PENDING:
            return  # retry pass handles the dispatch
        if receipt["state"] == RECEIPT_FAILED:
            await asyncio.to_thread(
                self.store.fail_exec_if_active,
                owner_id=owner_id,
                exec_id=exec_row["exec_id"],
                detail=receipt.get("detail") or "dispatch failed",
            )
            return
        if receipt["state"] == RECEIPT_DISPATCHED:
            next_stage, state = stage_after_dispatch(stage, len(workflow))
            detail = None
            if receipt.get("kind") == "native_command" and state == EXEC_COMPLETED:
                state = EXEC_UNVERIFIED
                detail = "command accepted; completion not verified"
            elif state == EXEC_ACTIVE and next_stage < len(workflow):
                detail = unavailable_wait_detail(workflow[next_stage].event)
            await asyncio.to_thread(
                self.store.advance_exec_if_active,
                owner_id=owner_id,
                exec_id=exec_row["exec_id"],
                stage=next_stage,
                state=state,
                detail=detail,
            )

    async def _recover_executions(
        self,
        owner_id: str,
        rules: list[dict[str, Any]],
        execs_by_id: dict[str, dict[str, Any]],
        receipts: dict[tuple[str, int], dict[str, Any]],
    ) -> bool:
        """Recover active executions after a crash or between event pages.

        Covers every crash position in the dispatch write order:
        - no receipt yet           -> reconstruct the directive and dispatch once;
        - receipt pending (prompt) -> retry with the SAME idempotency key (bounded);
        - receipt unknown (cmd)    -> fail honestly (never re-run a command);
        - receipt dispatched       -> advance the exec stage;
        - receipt failed           -> mark the exec failed.

        Returns True only when a dispatch was actually attempted. A normal
        "waiting for a correlated event" (or a pending retry still inside its
        backoff window) returns False so the run loop uses the slow poll
        instead of the 0.1s fast loop.
        """
        from hal.runtime import DispatchDirective

        did_work = False
        now = self._clock()
        for exec_row in execs_by_id.values():
            if exec_row.get("state") != EXEC_ACTIVE:
                continue
            # Re-read persisted state so a concurrent disable (which settles the
            # exec to 'disabled') is honored and never overwritten by recovery.
            if not await self._exec_still_active(owner_id, exec_row["exec_id"]):
                continue
            stage = int(exec_row["stage"])
            receipt = receipts.get((exec_row["exec_id"], stage))
            if receipt is None:
                if stage != 0:
                    # Dependent stages legitimately wait for a correlated event;
                    # a missing receipt here is normal, not a crash to repair.
                    continue
                directive = directive_for_exec_stage(exec_row)
                if directive is None:
                    await asyncio.to_thread(
                        self.store.fail_exec_if_active,
                        owner_id=owner_id,
                        exec_id=exec_row["exec_id"],
                        detail="cannot reconstruct dispatch for current stage",
                    )
                    continue
                if getattr(directive, "unbound_reason", None):
                    await asyncio.to_thread(
                        self.store.fail_exec_if_active,
                        owner_id=owner_id,
                        exec_id=exec_row["exec_id"],
                        detail=directive.unbound_reason,
                    )
                    continue
                await self._dispatch_stage(owner_id, exec_row, directive, rules, receipts)
                did_work = True
                continue
            if receipt["state"] == RECEIPT_DISPATCHED:
                await self._reconcile(owner_id, exec_row, rules, receipts)
                continue
            if receipt["state"] == RECEIPT_FAILED:
                await self._reconcile(owner_id, exec_row, rules, receipts)
                continue
            if receipt["state"] == RECEIPT_UNKNOWN and receipt["kind"] == "native_command":
                await asyncio.to_thread(
                    self.store.upsert_receipt,
                    exec_id=exec_row["exec_id"],
                    stage=stage,
                    idempotency_key=receipt["idempotency_key"],
                    kind=receipt["kind"],
                    target=receipt["target"],
                    payload=receipt["payload"],
                    state=RECEIPT_FAILED,
                    detail="command outcome unknown after interruption; manual verification advised",
                )
                await asyncio.to_thread(
                    self.store.fail_exec_if_active, owner_id=owner_id,
                    exec_id=exec_row["exec_id"],
                    detail="native command outcome unknown after interruption",
                )
                continue
            # RECEIPT_PENDING (prompt). Before retrying, check the durable feed
            # for a terminal Run carrying this idempotency key: if the prompt
            # was already accepted and its Run finished (crash between accept
            # and the 'dispatched' write), retrying would risk a duplicate side
            # effect. Honour the persisted completion instead.
            try:
                terminal, _instance = await self._terminal_for_receipt(owner_id, receipt)
                # An unavailable peer can still be safely retried with the same key.
            except RemoteTerminalUnavailable:
                terminal = None
            except Exception as exc:  # noqa: BLE001
                # A transient terminal-evidence read failure must neither kill
                # the runtime loop nor be treated as "no related Run" (which
                # would risk a duplicate re-dispatch of an already-accepted
                # prompt). Leave the receipt pending and retry on a later cycle.
                self.logger.warning(
                    "HAL terminal evidence read failed for exec %s stage %s: %s",
                    exec_row["exec_id"], stage, exc,
                )
                continue
            if terminal is not None:
                await self._settle_pending_from_terminal(
                    owner_id, exec_row, receipt, terminal
                )
                continue
            # Otherwise re-dispatch with the same idempotency key, but with a
            # bounded retry count and exponential backoff so a network failure
            # cannot become a continuous request storm.
            attempts = int(receipt.get("attempts") or 0)
            if attempts >= MAX_RETRY_ATTEMPTS:
                # Never destroy the last real error: the retry cap is context,
                # not the cause. Both the receipt and the execution keep it.
                limit_detail = "prompt retry limit exceeded"
                last_error = " ".join(str(receipt.get("detail") or "").split())
                if last_error and last_error != limit_detail:
                    limit_detail = f"{limit_detail} (last error: {last_error})"
                self.logger.error(
                    "HAL prompt dispatch gave up exec=%s stage=%s target=%s "
                    "attempts=%s last_error=%s",
                    exec_row["exec_id"],
                    stage,
                    receipt["target"],
                    attempts,
                    last_error or "unknown",
                )
                await asyncio.to_thread(
                    self.store.upsert_receipt,
                    exec_id=exec_row["exec_id"],
                    stage=stage,
                    idempotency_key=receipt["idempotency_key"],
                    kind=receipt["kind"],
                    target=receipt["target"],
                    payload=receipt["payload"],
                    state=RECEIPT_FAILED,
                    detail=limit_detail,
                )
                await asyncio.to_thread(
                    self.store.fail_exec_if_active, owner_id=owner_id,
                    exec_id=exec_row["exec_id"],
                    detail=limit_detail,
                )
                continue
            next_retry_at = receipt.get("next_retry_at")
            if next_retry_at is not None and float(next_retry_at) > now:
                continue
            directive = DispatchDirective(
                exec_id=exec_row["exec_id"],
                stage=stage,
                kind=receipt["kind"],
                target=receipt["target"],
                payload=receipt["payload"],
                idempotency_key=receipt["idempotency_key"],
            )
            await self._dispatch_stage(owner_id, exec_row, directive, rules, receipts)
            did_work = True
        if await self._recover_dependent_stages(owner_id, rules):
            did_work = True
        return did_work

    async def _recover_dependent_stages(
        self,
        owner_id: str,
        rules: list[dict[str, Any]],
    ) -> bool:
        """Durably drive dependent stages whose correlated terminal event is
        already BEHIND the owner feed cursor.

        Recovery writes the prior receipt as ``dispatched`` and advances the
        execution to the next stage, then the correlated terminal event must
        drive the dependent stage. A crash can land between that durable settle
        and the re-application of the recovered trigger, or a transient
        terminal-evidence read failure can advance the feed cursor past the
        correlated event before it is re-applied. In both cases any in-memory
        trigger is gone, so re-derive it from the only facts that survive a
        restart: the prior stage's durable receipt idempotency key plus the
        persisted terminal Run evidence (``terminal_event_for_key``).

        This adds no event queue, no executor, no background worker and no new
        polling cadence: it runs inside the existing recovery pass at the
        existing <=5s interval, does at most one indexed point lookup per
        waiting dependent stage, and returns True only when a dispatch was
        actually attempted (so a normal "still waiting" state never enters the
        0.1s fast loop).
        """
        execs = await asyncio.to_thread(self.store.list_active_execs, owner_id)
        loaded: list[dict[str, Any]] = []
        for e in execs:
            try:
                e["program"] = program_from_json(json.loads(e["program_blob"]))
            except (ValueError, KeyError, TypeError):
                e["program"] = None
            if e.get("program") is not None:
                loaded.append(e)
        execs_by_id = {str(e["exec_id"]): e for e in loaded}
        receipts = await self._load_receipts(loaded)

        did_work = False
        consumed: dict[str, set[str]] = {}
        for exec_row in execs_by_id.values():
            if str(exec_row.get("state")) != EXEC_ACTIVE:
                continue
            stage = int(exec_row["stage"])
            if stage <= 0:
                continue  # the entry stage is never "dependent"
            if receipts.get((exec_row["exec_id"], stage)) is not None:
                continue  # already dispatched / pending retry / failed
            prior = receipts.get((exec_row["exec_id"], stage - 1))
            if prior is None or prior.get("state") != RECEIPT_DISPATCHED:
                continue
            if prior.get("kind") != "prompt":
                # Native commands carry no durable Run terminal evidence and
                # must never be blindly replayed; their acceptance is already
                # recorded as 'unverified'. Leave such a stage to normal matching.
                continue
            try:
                terminal, instance = await self._terminal_for_receipt(owner_id, prior)
            except RemoteTerminalUnavailable as exc:
                if exec_row.get("detail") != str(exc):
                    await asyncio.to_thread(
                        self.store.advance_exec_if_active,
                        owner_id=owner_id, exec_id=exec_row["exec_id"], stage=stage,
                        state=EXEC_ACTIVE, detail=str(exc),
                    )
                continue
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "HAL dependent-stage evidence read failed for exec %s stage %s: %s",
                    exec_row["exec_id"], stage, exc,
                )
                continue
            if terminal is None:
                if instance:
                    detail = f"waiting for Run completion on @{instance}"
                    if exec_row.get("detail") != detail:
                        await asyncio.to_thread(
                            self.store.advance_exec_if_active,
                            owner_id=owner_id, exec_id=exec_row["exec_id"], stage=stage,
                            state=EXEC_ACTIVE, detail=detail,
                        )
                continue
            trigger = self._terminal_trigger(terminal, instance)
            if trigger is None:
                continue
            if await self._apply_trigger(
                owner_id, trigger, rules, execs_by_id, receipts, consumed
            ):
                did_work = True
        return did_work

    async def _terminal_for_receipt(self, owner_id: str, receipt: Mapping[str, Any]):
        from orchestrator.automation_events import terminal_event_for_key

        agent, instance = split_agent_spec(receipt.get("target"))
        key = receipt["idempotency_key"]
        if instance and instance != self._current_instance_id().casefold():
            if self.peer is None or owner_id.startswith("enterprise:"):
                raise RemoteTerminalUnavailable(f"HAL peer @{instance} unavailable")
            return await self.peer.terminal_event(instance, agent, key), instance
        terminal = await asyncio.to_thread(
            terminal_event_for_key, self.session_store, owner_id=owner_id,
            run_idempotency_key=key,
        )
        return terminal, None

    def _terminal_trigger(
        self, terminal: Mapping[str, Any], instance: str | None = None,
    ) -> HalTrigger | None:
        """Convert a durable terminal Run event into a canonical HAL trigger.

        Mirrors the feed-event loop's identity canonicalisation so the recovered
        trigger matches dependent stages declared with ``@name@instance``.
        """
        trigger = feed_event_hal_event(terminal)
        if trigger is None:
            return None
        if trigger.agent_identity:
            instance_id = instance.upper() if instance else self._current_instance_id()
            suffix = f"@{instance_id}" if instance_id else ""
            trigger = _with_agent_identity(trigger, f"@{trigger.agent_identity}{suffix}")
        return trigger

    async def _settle_pending_from_terminal(
        self,
        owner_id: str,
        exec_row: Mapping[str, Any],
        receipt: Mapping[str, Any],
        terminal: Mapping[str, Any],
    ) -> None:
        """Settle a ``pending_retry`` prompt whose Run already terminated.

        The dispatch side effect already happened (proven by the Run's
        idempotency key), so we must not re-send it. The Run's outcome
        (completed vs failed) is a SEPARATE signal handled by the normal event
        matching logic as ``#turn_finish`` / ``#turn_error``; here we only
        record the dispatch receipt and advance the stage, exactly as the
        accepted path would. A failed Run still proves the prompt was accepted,
        so a legal dependent ``#turn_error`` stage must still fire rather than
        being swallowed by prematurely failing the whole execution. The
        dependent stage is driven durably by ``_recover_dependent_stages``
        (re-derived from the prior receipt, never an in-memory trigger).
        """
        stage = int(exec_row["stage"])
        outcome = "failed" if terminal["type"] == "agent.run.failed" else "completed"
        await asyncio.to_thread(
            self.store.upsert_receipt,
            exec_id=exec_row["exec_id"],
            stage=stage,
            idempotency_key=receipt["idempotency_key"],
            kind=receipt["kind"],
            target=receipt["target"],
            payload=receipt["payload"],
            state=RECEIPT_DISPATCHED,
            detail=f"prompt accepted ({outcome} run observed)",
        )
        program = program_from_json(json.loads(exec_row["program_blob"]))
        workflow = program.workflows[int(exec_row["workflow_index"])]
        next_stage, state = stage_after_dispatch(stage, len(workflow))
        detail = None
        if state == EXEC_ACTIVE and next_stage < len(workflow):
            detail = unavailable_wait_detail(workflow[next_stage].event)
        await asyncio.to_thread(
            self.store.advance_exec_if_active,
            owner_id=owner_id,
            exec_id=exec_row["exec_id"],
            stage=next_stage,
            state=state,
            detail=detail,
        )


# ----------------------------------------------------------------- module APIs


def _with_agent_identity(trigger: HalTrigger, identity: str) -> HalTrigger:
    import dataclasses

    return dataclasses.replace(trigger, agent_identity=identity)


def _feed_page(session_store, owner_id: str, after: int, limit: int) -> dict[str, Any]:
    from orchestrator.automation_events import terminal_events_page

    return terminal_events_page(session_store, owner_id=owner_id, after=after, limit=limit)


def _event_run_id(event: Mapping[str, Any]) -> str:
    return str(event.get("run_id") or event.get("event_id") or "")


def _flatten_consumed(consumed: dict[str, set[str]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for exec_id, event_ids in consumed.items():
        for event_id in event_ids:
            out.add((exec_id, event_id))
    return out


def _program_for_rule(rules: list[dict[str, Any]], rule_id: str):
    for rule in rules:
        if str(rule["rule_id"]) == str(rule_id):
            return rule["program"]
    return None


def _rule_waits_for_whiteboard(program) -> bool:
    return rule_waits_for_whiteboard(program)


def _script_has_whiteboard_entry(script: str) -> bool:
    """Compile-and-check, used before enabling a rule."""
    try:
        return rule_waits_for_whiteboard(compile_hal(script))
    except Exception:  # noqa: BLE001 — an uncompilable script is rejected earlier
        return False


_ALL_EVENT_AVAILABILITY = {
    "#turn_finish": (True, "real durable run-terminal feed event (run.completed)"),
    "#turn_error": (True, "real durable run-terminal feed event (run.failed)"),
    "#whiteboard_stage": (
        True,
        "whiteboard stage-transition feed (requires a configured board endpoint)",
    ),
    "#system_idle": (False, "no reliable active-to-idle transition event source in v0.1"),
    "#turn_commentary": (False, "no commentary event source exists in the durable event feed"),
    "#hashideploy_finish": (False, "no deployment-completion event source exists"),
    "#reboot_ok": (False, "no reboot-completion event source exists"),
}


def _bridge_home(server) -> Path:
    return Path(
        getattr(getattr(server, "global_config", None), "bridge_home", None)
        or getattr(server, "config_path", Path(".")).parent
    )


def _default_store_path(server) -> Path:
    """Runtime state belongs under <bridge_home>/state, never in the source tree.

    The v0.1 default used to be ``<bridge_home>/hal/hal_v1.sqlite``, which put a
    database of user automation rules inside the checked-out repository. That
    risked committing private scripts and left every checkout permanently
    dirty, which also blocks deploy's clean-checkout gate.
    """
    return _bridge_home(server) / "state" / "hal" / "hal_v1.sqlite"


def _legacy_store_path(server) -> Path:
    return _bridge_home(server) / "hal" / "hal_v1.sqlite"


def _migrate_legacy_store(legacy: Path, target: Path, logger) -> Path:
    """Return the store path the service must open.

    A database created by the old in-tree default is copied into the state dir
    with ``sqlite3.Connection.backup()``, which yields one consistent file with
    every committed frame (including frames that only live in the source
    ``-wal``). The finished copy is published with ``os.replace``; HAL's store
    is single-process, so no cross-process publication guard is needed.

    On any failure the legacy database is kept and returned, so the service
    keeps serving the existing rules and execution records instead of starting
    from an empty state database.
    """
    if target.exists() or not legacy.is_file():
        return target
    temp: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=target.name + ".migrating-", suffix=".tmp", dir=target.parent
        )
        os.close(fd)
        temp = Path(temp_name)
        temp.unlink()  # sqlite3.connect must create the file itself
        source = sqlite3.connect(str(legacy))
        try:
            destination = sqlite3.connect(str(temp))
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        if target.exists():  # a valid state database is never replaced
            return target
        os.replace(temp, target)
        temp = None
    except Exception as exc:  # noqa: BLE001 — migration must never disable HAL
        if logger is not None:
            logger.warning(
                "HAL store migration failed (%s: %s); keeping legacy store at %s",
                type(exc).__name__,
                exc,
                legacy,
            )
        return legacy
    finally:
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass
    for suffix in ("", "-wal", "-shm"):
        leftover = legacy.with_name(legacy.name + suffix)
        try:
            leftover.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # e.g. still open in another process
            if logger is not None:
                logger.info("HAL store legacy file kept: %s (%s)", leftover, exc)
    if logger is not None:
        logger.info("HAL store migrated from %s to %s", legacy, target)
    return target


def ensure_service(server) -> HalService | None:
    """Build the service once per Workbench API server process."""
    service = getattr(server, "_hal_service", None)
    if service is not None:
        return service
    try:
        from hal.store import HalStore
        from hal.dispatch import GatewayDispatchAdapter
        from hal.peer import HalPeerClient

        logger = getattr(server, "logger", None) or logging.getLogger("hal.service")
        store_path = _migrate_legacy_store(
            _legacy_store_path(server), _default_store_path(server), logger
        )
        store = HalStore(store_path)
        from hal.whiteboard import WhiteboardClient, DEFAULT_BASE_URL

        board_url = (os.environ.get("HASHI_HAL_WHITEBOARD_URL") or "").strip() or None
        board_token = (os.environ.get("HASHI_HAL_WHITEBOARD_TOKEN") or "").strip() or None
        service = HalService(
            store=store,
            session_store=server.session_store,
            dispatch=GatewayDispatchAdapter(server, logger=logger),
            peer=HalPeerClient(
                Path(server.config_path).parent, server.session_store.instance_id,
            ),
            # Default to the LAN board so a stock install can use whiteboard
            # rules without new configuration; HASHI_HAL_WHITEBOARD_URL="" 
            # disables the source explicitly.
            whiteboard=(
                WhiteboardClient(board_url or DEFAULT_BASE_URL, token=board_token)
                if os.environ.get("HASHI_HAL_WHITEBOARD_URL") != ""
                else None
            ),
            logger=logger,
        )
        server._hal_service = service
    except Exception as exc:  # noqa: BLE001 — HAL must never break the gateway
        logger = getattr(server, "logger", None) or logging.getLogger("hal.service")
        logger.warning("HAL service unavailable: %s: %s", type(exc).__name__, exc)
        server._hal_service = None
        return None
    return service


def _require_auth(server, request):
    owner_id = server._v1_owner_id(request)
    if owner_id is None or not server._check_admin_auth(request):
        return None, web.json_response(
            {"ok": False, "error_code": "auth_required"}, status=401
        )
    return owner_id, None


async def _read_bounded_body(request: web.Request, max_bytes: int) -> tuple[bytes | None, web.Response | None]:
    if request.content_length is not None and request.content_length > max_bytes:
        return None, web.json_response(
            {"ok": False, "error_code": "body_too_large"}, status=413
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await request.content.read(4096)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None, web.json_response(
                {"ok": False, "error_code": "body_too_large"}, status=413
            )
        chunks.append(chunk)
    return b"".join(chunks), None


def _service_or_503(server):
    service = ensure_service(server)
    if service is None:
        return None, web.json_response(
            {"ok": False, "error_code": "hal_unavailable"}, status=503
        )
    return service, None


async def handle_hal_peer(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    body, error = await _read_bounded_body(request, MAX_SCRIPT_BYTES * 2)
    if error is not None:
        return error
    return await handle_hal_peer_request(server, owner_id, body)


async def handle_hal_status(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    return web.json_response(service.status(), headers={"Cache-Control": "no-store"})


async def handle_hal_notices(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    try:
        limit = int(request.query.get("limit", "50"))
    except (TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error_code": "invalid_limit"}, status=400
        )
    return web.json_response(
        {
            "ok": True,
            "schema": SCHEMA_ID,
            "notices": service.store.list_notices(owner_id, limit=limit),
        },
        headers={"Cache-Control": "no-store"},
    )


async def handle_hal_validate(server, request):
    _owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    body, error = await _read_bounded_body(request, MAX_SCRIPT_BYTES)
    if error is not None:
        return error
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
        script = payload.get("script")
        if not isinstance(script, str) or not script.strip():
            return web.json_response(
                {"ok": False, "error_code": "script_required"}, status=400
            )
    except (ValueError, UnicodeDecodeError):
        return web.json_response(
            {"ok": False, "error_code": "invalid_json"}, status=400
        )
    result = service.validate(script)
    return web.json_response(
        {"ok": result["ok"], "schema": SCHEMA_ID, **result},
        headers={"Cache-Control": "no-store"},
    )


async def handle_hal_rules_get(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    return web.json_response(
        {"ok": True, "schema": SCHEMA_ID, "rules": service.list_rules(owner_id)},
        headers={"Cache-Control": "no-store"},
    )


async def handle_hal_rules_post(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    body, error = await _read_bounded_body(request, MAX_SCRIPT_BYTES)
    if error is not None:
        return error
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
        name = payload.get("name")
        script = payload.get("script")
        rule_id = payload.get("rule_id")
        expected_revision = payload.get("expected_revision")
        migration_key = payload.get("migration_key")
        enable = bool(payload.get("enabled"))
        if not isinstance(name, str) or not isinstance(script, str):
            return web.json_response(
                {"ok": False, "error_code": "name_and_script_required"}, status=400
            )
        if rule_id is not None and not isinstance(rule_id, str):
            return web.json_response(
                {"ok": False, "error_code": "invalid_rule_id"}, status=400
            )
        if migration_key is not None:
            if not isinstance(migration_key, str) or not migration_key.strip():
                return web.json_response(
                    {"ok": False, "error_code": "invalid_migration_key"}, status=400
                )
            migration_key = migration_key.strip()
            if len(migration_key) > 256:
                return web.json_response(
                    {"ok": False, "error_code": "migration_key_too_long"}, status=400
                )
    except (ValueError, UnicodeDecodeError):
        return web.json_response(
            {"ok": False, "error_code": "invalid_json"}, status=400
        )

    try:
        rule, created = service.upsert_rule(
            owner_id=owner_id,
            name=name,
            script=script,
            rule_id=rule_id,
            expected_revision=expected_revision,
            migration_key=migration_key if rule_id is None else None,
        )
        if enable:
            await service.enable_rule(owner_id=owner_id, rule_id=rule["rule_id"])
            rule = service.store.get_rule(owner_id, rule["rule_id"])
    except HalStoreError as exc:
        return web.json_response(
            {"ok": False, "error_code": "hal_store_error", "detail": str(exc)},
            status=409 if "revision" in str(exc) else 400,
        )

    return web.json_response(
        {
            "ok": True,
            "schema": SCHEMA_ID,
            "rule": {
                "rule_id": rule["rule_id"],
                "name": rule["name"],
                "script": rule["script"],
                "revision": rule["revision"],
                "enabled": bool(rule["enabled"]),
                "created_at": rule["created_at"],
                "updated_at": rule["updated_at"],
            },
            "created": created,
        },
        status=201 if created else 200,
    )


async def handle_hal_rule_enable(server, request):
    return await _handle_hal_rule_toggle(server, request, True)


async def handle_hal_rule_disable(server, request):
    return await _handle_hal_rule_toggle(server, request, False)


async def _handle_hal_rule_toggle(server, request, enabled: bool):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    rule_id = request.match_info.get("rule_id")
    body, error = await _read_bounded_body(request, MAX_SCRIPT_BYTES)
    if error is not None:
        return error
    expected_revision = None
    if body:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            expected_revision = payload.get("expected_revision")
        except (ValueError, UnicodeDecodeError):
            return web.json_response(
                {"ok": False, "error_code": "invalid_json"}, status=400
            )
    try:
        if enabled:
            # The board baseline read is awaited inside enable_rule: the event
            # loop keeps serving the API while a slow board answers.
            rule = await service.enable_rule(
                owner_id=owner_id,
                rule_id=rule_id,
                expected_revision=expected_revision,
            )
        else:
            # Disabling never reads the board: an outage must not pin
            # automation on.
            rule = service.set_enabled(
                owner_id=owner_id,
                rule_id=rule_id,
                enabled=False,
                expected_revision=expected_revision,
            )
    except HalStoreError as exc:
        status = 404 if "not found" in str(exc) else (409 if "revision" in str(exc) else 409)
        return web.json_response(
            {"ok": False, "error_code": "hal_store_error", "detail": str(exc)},
            status=status,
        )
    if rule is None:
        return web.json_response(
            {"ok": False, "error_code": "rule_not_found"}, status=404
        )
    return web.json_response(
        {
            "ok": True,
            "schema": SCHEMA_ID,
            "rule": {
                "rule_id": rule["rule_id"],
                "name": rule["name"],
                "revision": rule["revision"],
                "enabled": bool(rule["enabled"]),
            },
        }
    )


async def handle_hal_runs_get(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    try:
        limit = int(request.query.get("limit", "50"))
    except (TypeError, ValueError):
        limit = 50
    # Optional filter so a caller inspecting one rule sees that rule's latest
    # runs. Absent or blank keeps the previous "all rules" behaviour.
    rule_id = (request.query.get("rule_id") or "").strip() or None
    return web.json_response(
        {
            "ok": True,
            "schema": SCHEMA_ID,
            "runs": service.list_runs(owner_id, limit, rule_id=rule_id),
        },
        headers={"Cache-Control": "no-store"},
    )


async def handle_hal_run_get(server, request):
    owner_id, error = _require_auth(server, request)
    if error is not None:
        return error
    service, error = _service_or_503(server)
    if error is not None:
        return error
    exec_id = request.match_info.get("exec_id")
    detail = service.run_detail(owner_id, exec_id)
    if detail is None:
        return web.json_response(
            {"ok": False, "error_code": "run_not_found"}, status=404
        )
    return web.json_response(
        {"ok": True, "schema": SCHEMA_ID, "run": detail},
        headers={"Cache-Control": "no-store"},
    )
# HASHI-Fingerprint: hfp_ae365f75babe440f8247fb1d9ba7c5a2
