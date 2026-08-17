from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestrator import runtime_menu_views

ROOT = Path(__file__).resolve().parents[1]


class _Slots:
    SLOTS = ("1", "2")

    def __init__(self) -> None:
        self.values = {
            "1": {"active": True, "text": "Keep <private> safe."},
            "2": {"active": False, "text": ""},
        }

    def _slot(self, slot_id: str) -> dict:
        return self.values[slot_id]


def _assert_standard_card(text: str, title: str) -> None:
    assert f"<b>{title}</b>" in text
    assert "━━━━━━━━━━━━━━━━" in text
    assert "<b>Current</b> ·" in text
    assert text.index("━━━━━━━━━━━━━━━━") < text.index("<b>Current</b> ·")


def test_command_card_divider_is_centralized() -> None:
    offenders = []
    for path in (ROOT / "orchestrator").rglob("*.py"):
        if path.name == "command_ui.py":
            continue
        if "━━━━━━━━━━━━━━━━" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def test_active_and_legacy_runtimes_share_critical_menu_renderers() -> None:
    required = (
        "runtime_menu_views.parked_topics_text(",
        "runtime_menu_views.sys_slots_text(",
        "runtime_menu_views.loop_list_text(",
        "runtime_menu_views.safevoice_menu_text(",
    )
    for relative_path in (
        "orchestrator/flexible_agent_runtime.py",
        "orchestrator/legacy/bridge_agent_runtime.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert all(call in source for call in required)


def test_parked_topics_card_escapes_values_and_keeps_actions_last() -> None:
    text = runtime_menu_views.parked_topics_text(
        [
            {
                "slot_id": 7,
                "title": "Review <unsafe>",
                "summary_short": "A & B",
                "followup": {"status": "scheduled", "attempts": 1},
            }
        ]
    )

    _assert_standard_card(text, "PARKED TOPICS")
    assert "Review &lt;unsafe&gt;" in text
    assert "A &amp; B" in text
    assert "Review <unsafe>" not in text
    assert text.index("<b>TOPICS</b>") < text.index("<b>Use</b>")
    assert "<code>/load &lt;slot&gt;</code>" in text


def test_ticket_and_loop_lists_escape_dynamic_content() -> None:
    tickets = runtime_menu_views.ticket_list_text(
        [{"ticket_id": "T<1>", "source_agent": "a&b", "summary": "fix <menu>"}],
        [],
    )
    loops = runtime_menu_views.loop_list_text(
        [
            (
                "cron",
                {
                    "id": "job<1>",
                    "enabled": True,
                    "schedule": "0 9 * * *",
                    "note": "check A&B",
                    "loop_meta": {"count": 2, "max": 10},
                },
            )
        ]
    )

    _assert_standard_card(tickets, "SUPPORT TICKETS")
    _assert_standard_card(loops, "LOOPS")
    assert "T&lt;1&gt;" in tickets and "fix &lt;menu&gt;" in tickets
    assert "job&lt;1&gt;" in loops and "check A&amp;B" in loops


def test_system_slot_cards_report_state_and_escape_prompt_text() -> None:
    manager = _Slots()

    overview = runtime_menu_views.sys_slots_text(manager)
    detail = runtime_menu_views.sys_slot_text(manager, "1")

    _assert_standard_card(overview, "SYSTEM PROMPT SLOTS")
    _assert_standard_card(detail, "SYSTEM PROMPT SLOT")
    assert "<code>1</code> · <b>ON</b>" in overview
    assert "Keep &lt;private&gt; safe." in overview
    assert "<pre>Keep &lt;private&gt; safe.</pre>" in detail


def test_settings_cards_keep_current_value_before_facts_and_escape_names() -> None:
    safevoice = runtime_menu_views.safevoice_menu_text(enabled=False)
    timeout = runtime_menu_views.timeout_menu_text(
        agent_name="agent<one>",
        backend_name="codex<cli>",
        idle_minutes=30,
        hard_minutes=120,
        default_idle_minutes=5,
        default_hard_minutes=30,
        idle_source="user <override>",
        hard_source="backend & config",
    )
    wol = runtime_menu_views.wol_targets_text(
        [{"name": "pc<1>", "label": "Desk & PC", "description": "Main <host>"}],
        instance_id="HASHI&2",
    )

    _assert_standard_card(safevoice, "SAFE VOICE")
    _assert_standard_card(timeout, "BACKEND TIMEOUT")
    _assert_standard_card(wol, "WAKE-ON-LAN TARGETS")
    assert "<b>Current</b> · <b>OFF</b>" in safevoice
    assert "agent&lt;one&gt;" in timeout
    assert "codex&lt;cli&gt;" in timeout
    assert "user &lt;override&gt;" in timeout
    assert "backend &amp; config" in timeout
    assert "HASHI&amp;2" in wol
    assert "pc&lt;1&gt;" in wol and "Desk &amp; PC" in wol and "Main &lt;host&gt;" in wol


def test_backend_and_model_cards_share_standard_order_and_escape_values() -> None:
    backend = runtime_menu_views.backend_menu_text(active_backend="api<one>")
    picker = runtime_menu_views.backend_model_prompt_text(
        backend="api<one>",
        current_model="model&a",
        with_context=True,
    )
    model = runtime_menu_views.model_menu_text(
        model="model&a",
        backend="api<one>",
        has_choices=False,
        persists=True,
        provider="provider<one>",
    )

    _assert_standard_card(backend, "HASHI BACKEND")
    _assert_standard_card(picker, "CHOOSE MODEL")
    _assert_standard_card(model, "HASHI MODEL")
    assert "api&lt;one&gt;" in backend and "api&lt;one&gt;" in picker
    assert "model&amp;a" in picker and "model&amp;a" in model
    assert "provider&lt;one&gt;" in model
    assert "<code>/model &lt;name&gt;</code>" in model


def test_claw_provider_cards_follow_standard_order_and_escape_values() -> None:
    provider = runtime_menu_views.claw_provider_menu_text(
        current_provider="open<router>",
        available_count=2,
        unavailable=[("local&host", "provider is disabled")],
        backend_flow=True,
    )
    model = runtime_menu_views.claw_provider_model_text(
        provider="deep<seek>",
        current_model="model&one",
        model_count=3,
        with_context=True,
    )
    unavailable = runtime_menu_views.claw_provider_unavailable_text(backend="codex<cli>")

    _assert_standard_card(provider, "HER PROVIDER")
    _assert_standard_card(model, "CHOOSE HER MODEL")
    _assert_standard_card(unavailable, "HER PROVIDER")
    assert "open&lt;router&gt;" in provider
    assert "local&amp;host" in provider
    assert "deep&lt;seek&gt;" in model
    assert "model&amp;one" in model
    assert "codex&lt;cli&gt;" in unavailable


def test_skill_detail_escapes_reference_and_reports_standard_package() -> None:
    skill = SimpleNamespace(
        id="debug<strict>",
        name="Debug & inspect",
        description="Use <carefully>.",
        body="Never expose A&B.",
    )
    manager = SimpleNamespace(get_active_toggle_ids=lambda _workspace: {"debug<strict>"})

    text = runtime_menu_views.skill_detail_text(
        skill,
        Path("/tmp/workspace"),
        manager=manager,
    )

    _assert_standard_card(text, "DEBUG &amp; INSPECT")
    assert "<b>Current</b> · <b>READY</b>" in text
    assert "<b>Format</b> · <code>SKILL.md</code>" in text
    assert "Use &lt;carefully&gt;." in text
    assert "Never expose A&amp;B." in text
    assert "debug&lt;strict&gt;" in text
