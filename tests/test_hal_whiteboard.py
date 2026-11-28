"""HAL v2 whiteboard stage-transition source.

The board is faked (no network, no live board) but everything else is real: the
compiler, the planner, the SQLite store, the idempotency keys and the runtime
loop. Each test pins one guarantee from the Phase 2 card.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading

import pytest

from hal import compile_hal, HALEvent, entry_matches, resolve_prompt, resolve_role_target
from hal.dispatch import RecordingDispatchAdapter
from hal.event_source import (
    EVENT_WHITEBOARD_STAGE,
    HalTrigger,
    SOURCE_WHITEBOARD,
    whiteboard_event_trigger,
)
from hal.runtime import halted_execs_for, plan_for_trigger, whiteboard_halt_reason
from hal.service import HalService
from hal.store import HalStore

OWNER = "user:7"
CARD = "card-1"


class FakeBoard:
    """Stand-in for :class:`hal.whiteboard.WhiteboardClient`."""

    base_url = "http://board.test"

    def __init__(self, stages=None, *, comment_ok=True, stages_error=None):
        self.events: list[dict] = []
        self.head = 0
        self.comments: list[tuple[str, str]] = []
        self.comment_calls = 0
        self.stage_calls = 0
        self._stages = stages or {}
        self._comment_ok = comment_ok
        self._stages_error = stages_error

    def append(self, **fields) -> dict:
        self.head += 1
        event = {
            "seq": self.head,
            "event_id": f"{fields.get('card_id', CARD)}:{fields.get('revision', self.head)}",
            "card_id": CARD,
            "work_key": "hashi/x",
            "project": "Hashi",
            "revision": self.head,
            "from_stage": "working",
            "to_stage": "in_review",
            "category": "active",
            "round": 1,
            "max_rounds": 3,
            "status": "in_progress",
            "implementer": "lily",
            "reviewer": "may",
            "tester": "",
            "pr_url": "https://github.com/x/y/pull/1",
            "head_sha": "abc123",
            "last_verdict": "",
            "test_env": "",
            "next_step": "review it",
            "at": "2026-11-28T00:00:00Z",
        }
        event.update(fields)
        self.events.append(event)
        return event

    async def fetch_events(self, after: int, limit: int = 200):
        pending = [e for e in self.events if e["seq"] > after]
        page = pending[:limit]
        return {
            "events": page,
            "next_cursor": page[-1]["seq"] if page else after,
            "has_more": len(pending) > limit,
            "gap": None,
        }

    def head_seq_blocking(self) -> int:
        return self.head

    async def stages(self, project: str = ""):
        self.stage_calls += 1
        if self._stages_error is not None:
            raise self._stages_error
        return self._stages

    async def post_comment(self, card_id: str, body: str, *, actor: str = "HAL"):
        self.comment_calls += 1
        if not self._comment_ok:
            return False, "board unreachable"
        self.comments.append((card_id, body))
        return True, "comment posted"


class FakeFeed:
    """The Run feed is not under test here; it stays empty."""

    def __init__(self, instance_id="TESTINST"):
        self.instance_id = instance_id
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE automation_event_index (
                feed_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT, run_id TEXT, session_id TEXT, kind TEXT,
                created_at TEXT, agent_id TEXT, owner_id TEXT
            );
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE runs (run_id TEXT PRIMARY KEY, idempotency_key TEXT, session_id TEXT);
            """
        )

    def _connection(self):
        class _Ctx:
            def __init__(self, conn):
                self.conn = conn

            def __enter__(self):
                return self.conn

            def __exit__(self, *exc):
                return False

        return _Ctx(self._conn)


class FakePeer:
    def __init__(self):
        self.prompts: list[tuple[str, str, str, str]] = []

    async def dispatch_prompt(self, instance, agent, text, key):
        from hal.dispatch import DispatchResult

        self.prompts.append((instance, agent, text, key))
        return DispatchResult(True, False, f"Run accepted on @{instance.upper()}")

    async def terminal_event(self, instance, agent, key):
        return None


def make_service(tmp_path, *, board=None, peer=None):
    store = HalStore(tmp_path / "hal" / "hal_v1.sqlite")
    adapter = RecordingDispatchAdapter()
    service = HalService(
        store=store,
        session_store=FakeFeed(),
        dispatch=adapter,
        logger=logging.getLogger("hal.test"),
        peer=peer,
        whiteboard=board,
    )
    return service, adapter


def make_rule(service, script, name="rule"):
    return service.upsert_rule(owner_id=OWNER, name=name, script=script)[0]


def run(coro):
    return asyncio.run(coro)


REVIEW_RULE = (
    'if #whiteboard_stage where [project="Hashi", to_stage="in_review"] then '
    '@{reviewer} $prompt "PR {pr_url} HEAD {head_sha} on {card_id} '
    'round {round}/{max_rounds} ({from_stage}->{to_stage}) next: {next_step}".'
)


# --------------------------------------------------------------- language


def test_custom_stage_names_need_no_code_change():
    """A board that invents stage keys still matches: they are author data."""
    program = compile_hal(
        'if #whiteboard_stage where [to_stage="waiting_for_legal_review"] then '
        '@{tester} $prompt "please test {card_id}".'
    )
    rule = program.workflows[0][0]
    event = HALEvent(
        EVENT_WHITEBOARD_STAGE,
        context={"to_stage": "waiting_for_legal_review", "card_id": "c9"},
    )
    assert entry_matches(rule, event)
    assert not entry_matches(
        rule, HALEvent(EVENT_WHITEBOARD_STAGE, context={"to_stage": "in_review"})
    )
    # A declared condition the event does not carry never matches.
    assert not entry_matches(rule, HALEvent(EVENT_WHITEBOARD_STAGE, context={}))


def test_role_target_and_variables_render_from_the_card():
    rule = compile_hal(REVIEW_RULE).workflows[0][0]
    context = {
        "card_id": CARD,
        "reviewer": "yun@hashi6",
        "pr_url": "https://x/1",
        "head_sha": "abc123",
        "round": 2,
        "max_rounds": 3,
        "from_stage": "working",
        "to_stage": "in_review",
        "next_step": "review it",
    }
    assert resolve_role_target(rule.action, context=context) == "@yun@hashi6"
    assert resolve_prompt(rule.action, agent_trigger=None, context=context) == (
        "PR https://x/1 HEAD abc123 on card-1 round 2/3 (working->in_review) next: review it"
    )
    # A bare name is qualified as an @name spec; an empty role is not invented.
    assert resolve_role_target(
        rule.action, context={"reviewer": "may"}
    ) == "@may"
    assert resolve_role_target(rule.action, context={}) == ""


@pytest.mark.parametrize(
    "script",
    [
        'if #whiteboard_stage where [nonsense="x"] then /x.',
        'if #whiteboard_stage where [project=Hashi] then /x.',
        'if #reboot_ok where [project="Hashi"] then /x.',
        'if #whiteboard_stage then @{boss} $prompt "x".',
        'if #whiteboard_stage then @{reviewer} $prompt "{unknown_var}".',
        'if @lily #whiteboard_stage then @lily $prompt "x".',
        'if #reboot_ok then @{reviewer} $prompt "x".',
    ],
)
def test_whiteboard_syntax_errors_are_explicit(script):
    from hal import HALSyntaxError

    with pytest.raises(HALSyntaxError):
        compile_hal(script)


def test_non_context_prompts_keep_braces_literal():
    """v0.1 rules are unchanged: braces are text when there is no card."""
    program = compile_hal('if #reboot_ok then @lily $prompt "template {literal} stays".')
    action = program.workflows[0][0].action
    assert resolve_prompt(action, agent_trigger=None) == "template {literal} stays"


# ----------------------------------------------------------------- runtime


def test_halt_reasons_cover_fuse_gate_budget_and_category():
    def trigger(**context):
        return HalTrigger(
            event=EVENT_WHITEBOARD_STAGE,
            agent_identity=None,
            feed_event_id="c:rev:1",
            feed_kind="whiteboard.stage",
            run_idempotency_key=None,
            automation_origin=False,
            feed_cursor=1,
            source=SOURCE_WHITEBOARD,
            context={"card_id": CARD, **context},
        )

    assert whiteboard_halt_reason(trigger(fused=True, reason="round fuse")) == "round fuse"
    assert "halted" in whiteboard_halt_reason(trigger(category="halted", to_stage="blocked"))
    assert "human decision" in whiteboard_halt_reason(trigger(human_gate=True, to_stage="merged"))
    assert "max_rounds" in whiteboard_halt_reason(trigger(round=3, max_rounds=3))
    assert whiteboard_halt_reason(trigger(round=2, max_rounds=3)) is None
    # A feed trigger is never halted by board rules.
    feed = HalTrigger(
        event="#turn_finish", agent_identity="@lily@x", feed_event_id="e1",
        feed_kind="run.completed", run_idempotency_key=None, automation_origin=False,
    )
    assert whiteboard_halt_reason(feed) is None


def test_whiteboard_baseline_uses_the_board_cursor_not_the_feed_cursor():
    rule_row = {
        "rule_id": "r1",
        "revision": 1,
        "name": "r",
        "enabled": True,
        "enable_cursor": 100,     # feed baseline is far ahead
        "wb_enable_cursor": 5,    # board baseline is behind the event
        "program": compile_hal(REVIEW_RULE),
    }
    trigger = whiteboard_event_trigger(
        {"card_id": CARD, "revision": 1, "seq": 6, "to_stage": "in_review", "project": "Hashi"}
    )
    creates, _, _ = plan_for_trigger(trigger, [rule_row], [], [], set())
    assert len(creates) == 1  # 6 > 5 even though 6 <= 100

    older = whiteboard_event_trigger(
        {"card_id": CARD, "revision": 1, "seq": 5, "to_stage": "in_review", "project": "Hashi"}
    )
    creates, _, _ = plan_for_trigger(older, [rule_row], [], [], set())
    assert creates == []  # at or below the board baseline never replays


def test_halted_execs_only_match_the_same_card():
    execs = [
        {"exec_id": "e1", "state": "active", "context_blob": '{"card_id": "card-1"}'},
        {"exec_id": "e2", "state": "active", "context_blob": '{"card_id": "card-2"}'},
        {"exec_id": "e3", "state": "completed", "context_blob": '{"card_id": "card-1"}'},
    ]
    trigger = whiteboard_event_trigger(
        {"card_id": "card-1", "revision": 2, "seq": 9, "fused": True, "to_stage": "blocked"}
    )
    halts = halted_execs_for(trigger, execs)
    assert [halt.exec_id for halt in halts] == ["e1"]


# ----------------------------------------------------------------- service


def test_enable_never_replays_board_history(tmp_path):
    board = FakeBoard()
    board.append(card_id=CARD, revision=1)  # happened before HAL was enabled
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)

    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_wb_cursor(OWNER) >= 1
    run(service._process_owner(OWNER))
    assert adapter.prompt_calls == []

    board.append(card_id=CARD, revision=2)
    run(service._process_owner(OWNER))
    assert len(adapter.prompt_calls) == 1


def test_rule_enabled_later_uses_its_own_board_baseline(tmp_path):
    board = FakeBoard()
    service, adapter = make_service(tmp_path, board=board)
    first = make_rule(service, REVIEW_RULE, name="first")
    service.set_enabled(owner_id=OWNER, rule_id=first["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=1)  # committed BEFORE the second rule

    second = make_rule(
        service,
        'if #whiteboard_stage where [to_stage="in_review"] then @lily $prompt "second {card_id}".',
        name="second",
    )
    service.set_enabled(owner_id=OWNER, rule_id=second["rule_id"], enabled=True)
    run(service._process_owner(OWNER))

    # Only the rule that saw the transition after its own enable fires.
    assert [call["target_agent"] for call in adapter.prompt_calls] == ["may"]

    board.append(card_id=CARD, revision=2)
    run(service._process_owner(OWNER))

    targets = [call["target_agent"] for call in adapter.prompt_calls]
    assert targets.count("lily") == 1
    assert targets == ["may", "may", "lily"]


def test_same_card_revision_dispatches_once(tmp_path):
    board = FakeBoard()
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=3)

    run(service._process_owner(OWNER))
    run(service._process_owner(OWNER))
    # Re-deliver the very same transition by rewinding the cursor: the
    # (rule, card, revision) identity must swallow it.
    service.store.set_wb_cursor(OWNER, 0)
    run(service._process_owner(OWNER))

    assert len(adapter.prompt_calls) == 1
    assert len(service.store.list_execs(OWNER)) == 1


def test_prompt_carries_card_context_and_routes_to_the_reviewer(tmp_path):
    board = FakeBoard()
    peer = FakePeer()
    service, adapter = make_service(tmp_path, board=board, peer=peer)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(
        card_id=CARD, revision=4, round=2, pr_url="https://x/9",
        head_sha="deadbeef", reviewer="yun@hashi6",
    )

    run(service._process_owner(OWNER))

    # The card names a remote instance, so the existing peer channel carries it.
    assert adapter.prompt_calls == []
    assert len(peer.prompts) == 1
    instance, agent, text, key = peer.prompts[0]
    assert (instance, agent) == ("hashi6", "yun")
    assert "https://x/9" in text and "deadbeef" in text and "round 2/3" in text
    assert key.startswith("hal:")


def test_local_reviewer_uses_the_local_dispatch_path(tmp_path):
    board = FakeBoard()
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=5, reviewer="may")

    run(service._process_owner(OWNER))

    assert len(adapter.prompt_calls) == 1
    assert adapter.prompt_calls[0]["target_agent"] == "may"


def test_missing_reviewer_fails_honestly_instead_of_guessing(tmp_path):
    board = FakeBoard()
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=6, reviewer="")

    run(service._process_owner(OWNER))

    assert adapter.prompt_calls == []
    execs = service.store.list_execs(OWNER)
    assert execs[0]["state"] == "failed"
    assert "no value on card" in (execs[0]["detail"] or "")


def test_fused_transition_stops_the_chain_and_notifies(tmp_path):
    board = FakeBoard()
    service, adapter = make_service(tmp_path, board=board)
    # Two stages: the first dispatch leaves the execution waiting for the
    # implementer's turn, so there is a live chain for the fuse to stop.
    rule = make_rule(
        service,
        'if #whiteboard_stage where [to_stage="in_review"] then '
        '@{reviewer} $prompt "review {card_id}";'
        'if @agent_any #turn_finish then @{implementer} $prompt "fix {card_id}".',
        name="chain",
    )
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=1)
    run(service._process_owner(OWNER))

    assert len(adapter.prompt_calls) == 1
    assert any(row["state"] == "active" for row in service.store.list_execs(OWNER))

    board.append(
        card_id=CARD, revision=2, fused=True,
        reason="Round 4 exceeded max_rounds 3; the card was fused into blocked.",
    )
    run(service._process_owner(OWNER))

    assert all(row["state"] != "active" for row in service.store.list_execs(OWNER))
    notices = service.store.list_notices(OWNER)
    assert len(notices) == 1
    assert notices[0]["kind"] == "halted"
    assert "max_rounds" in notices[0]["detail"]
    assert board.comments and "HAL" in board.comments[0][1]
    # The stop is reported once, not once per pass.
    run(service._process_owner(OWNER))
    assert len(service.store.list_notices(OWNER)) == 1


def test_human_gate_stage_stops_and_notifies(tmp_path):
    board = FakeBoard(
        stages={"merged": {"key": "merged", "human_gate": True}, "in_review": {"key": "in_review"}}
    )
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(
        service,
        'if #whiteboard_stage where [to_stage="merged"] then @{reviewer} $prompt "merge it".',
    )
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=7, to_stage="merged", status="done")

    run(service._process_owner(OWNER))

    assert adapter.prompt_calls == []
    notices = service.store.list_notices(OWNER)
    assert len(notices) == 1 and "human decision" in notices[0]["detail"]


def test_human_gate_move_without_automation_is_silent(tmp_path):
    """A gated transition no rule would automate must not raise a stop notice."""
    board = FakeBoard(stages={"merged": {"key": "merged", "human_gate": True}})
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)  # only matches to_stage=in_review
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=20, to_stage="merged")

    run(service._process_owner(OWNER))

    assert adapter.prompt_calls == []
    assert service.store.list_notices(OWNER) == []
    assert board.comments == []


def test_exhausted_round_budget_stops(tmp_path):
    board = FakeBoard()
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=8, round=3, max_rounds=3)

    run(service._process_owner(OWNER))

    assert adapter.prompt_calls == []
    assert "max_rounds" in service.store.list_notices(OWNER)[0]["detail"]


def test_unreadable_stage_table_refuses_to_auto_advance(tmp_path):
    board = FakeBoard(stages_error=RuntimeError("board down"))
    service, adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=9)

    run(service._process_owner(OWNER))

    assert adapter.prompt_calls == []
    assert "stage table unavailable" in service.store.list_notices(OWNER)[0]["detail"]


def test_notice_delivery_failure_is_recorded_not_hidden(tmp_path):
    board = FakeBoard(comment_ok=False)
    service, _adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    board.append(card_id=CARD, revision=10, fused=True)

    run(service._process_owner(OWNER))

    notices = service.store.list_notices(OWNER)
    assert notices and notices[0]["delivered"].startswith("not delivered")


def test_whiteboard_rule_refuses_enable_without_a_board(tmp_path):
    service, _adapter = make_service(tmp_path, board=None)
    rule = make_rule(service, REVIEW_RULE)
    with pytest.raises(Exception) as excinfo:
        service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    assert "whiteboard" in str(excinfo.value)


def test_whiteboard_cursor_does_not_move_the_run_feed_cursor(tmp_path):
    board = FakeBoard()
    service, _adapter = make_service(tmp_path, board=board)
    rule = make_rule(service, REVIEW_RULE)
    service.set_enabled(owner_id=OWNER, rule_id=rule["rule_id"], enabled=True)
    feed_cursor = service.store.get_cursor(OWNER)
    board.append(card_id=CARD, revision=11)

    run(service._process_owner(OWNER))

    assert service.store.get_cursor(OWNER) == feed_cursor
    assert service.store.get_wb_cursor(OWNER) == board.head
# HASHI-Fingerprint: hfp_7dd66c5efd3d4871b17aabe563f385ae

