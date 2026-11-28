"""Traditional Chinese (zh-TW) support and frontend language alignment.

The assertions here pin the rendered result a user actually sees, and the
request-language decision the frontend command bridge makes. They deliberately
compare against the catalog text itself rather than banning words.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_menu_views
from orchestrator import ui_language
from orchestrator.command_interaction_bridge import _request_locale


def _runtime(bridge_home, *, default_locale="en", actor=7):
    config = SimpleNamespace(
        bridge_home=bridge_home,
        project_root=bridge_home,
        ui_language=default_locale,
        authorized_id=actor,
    )
    return SimpleNamespace(global_config=config)


class TestCatalog:
    def test_traditional_chinese_is_a_supported_locale(self):
        assert "zh-TW" in ui_language.SUPPORTED_LOCALES
        catalog = ui_language.load_catalog("zh-TW")
        assert catalog.locale == "zh-TW"
        assert catalog.native_name == "繁體中文"
        assert catalog.display_name == "Chinese (Traditional)"

    @pytest.mark.parametrize(
        "alias",
        ["zh-TW", "zh-tw", "zh-hant", "zh-HK", "zh-MO", "繁體中文", "繁体中文", "繁中"],
    )
    def test_traditional_chinese_aliases_resolve(self, alias):
        assert ui_language.normalize_locale(alias) == "zh-TW"

    def test_simplified_chinese_still_resolves_to_its_own_catalog(self):
        assert ui_language.normalize_locale("zh-CN") == "zh-CN"
        assert ui_language.normalize_locale("中文") == "zh-CN"

    @pytest.mark.parametrize("unsupported", ["ja", "ko", "de", "fr", "ru", "ar"])
    def test_unsupported_frontend_languages_fall_back_to_english(self, unsupported):
        assert ui_language.normalize_locale(unsupported) == "en"

    def test_catalogs_are_complete_and_share_placeholders(self):
        assert ui_language.validate_catalogs() == []

    def test_traditional_catalog_covers_every_english_key(self):
        english = ui_language.load_catalog("en")
        traditional = ui_language.load_catalog("zh-TW")
        assert set(english.strings) <= set(traditional.strings)
        assert set(english.commands) <= set(traditional.commands)
        assert set(english.titles) <= set(traditional.titles)

    def test_language_options_list_traditional_chinese(self):
        locales = [catalog.locale for catalog in ui_language.language_options()]
        assert locales == ["en", "zh-CN", "zh-TW"]


class TestRenderedMenus:
    def test_commentary_card_renders_the_traditional_catalog(self):
        with ui_language.language_scope(SimpleNamespace(), locale="zh-TW"):
            card = runtime_menu_views.her_commentary_text(
                saved_mode="packaged", effective_mode="packaged"
            )
            title = ui_language.tr("menu.commentary.title")
            mode = ui_language.tr("menu.commentary.mode.packaged")
            description = ui_language.tr("menu.commentary.mode.packaged.desc")
        assert title == "過程播報"
        assert f"<b>{title}</b>" in card
        assert mode in card
        assert description in card
        assert ui_language.tr("menu.commentary.mode.off", locale="zh-TW") == "關閉"

    def test_model_card_names_the_reasoning_level_in_traditional(self):
        with ui_language.language_scope(SimpleNamespace(), locale="zh-TW"):
            card = runtime_menu_views.her_v3_model_menu_text(
                provider="openrouter", model="deepseek-chat", effort="max"
            )
            label = ui_language.tr("common.effort")
            level = ui_language.tr("menu.her.reasoning.level.max")
        assert label in card
        assert level == "最大"
        assert level in card

    def test_traditional_and_simplified_cards_differ(self):
        with ui_language.language_scope(SimpleNamespace(), locale="zh-TW"):
            traditional = runtime_menu_views.her_commentary_text(
                saved_mode="packaged", effective_mode="packaged"
            )
        with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
            simplified = runtime_menu_views.her_commentary_text(
                saved_mode="packaged", effective_mode="packaged"
            )
        assert traditional != simplified
        assert ui_language.tr("menu.commentary.title", locale="zh-TW") == "過程播報"
        assert ui_language.tr("menu.commentary.title", locale="zh-CN") == "过程播报"


class TestAgentTerm:
    """One concept, one spelling per script: 智能体 (zh-CN), 智慧體 (zh-TW)."""

    def test_each_script_keeps_its_own_spelling(self):
        simplified = ui_language.load_catalog("zh-CN")
        traditional = ui_language.load_catalog("zh-TW")
        assert simplified.commands["agents"] == "查看和管理智能体"
        assert traditional.commands["agents"] == "檢視和管理智慧體"

    def test_rendered_agent_presence_title_follows_the_script(self):
        with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
            simplified = ui_language.tr("menu.agentpresence.title")
        with ui_language.language_scope(SimpleNamespace(), locale="zh-TW"):
            traditional = ui_language.tr("menu.agentpresence.title")
        assert simplified == "智能体在场"
        assert traditional == "智慧體在場"


class TestRequestLocale:
    def test_request_language_wins_when_supported(self, tmp_path):
        runtime = _runtime(tmp_path, default_locale="en")
        assert _request_locale(runtime, {"ui_locale": "zh-TW"}, 7) == "zh-TW"
        assert _request_locale(runtime, {"ui_locale": "zh-tw"}, 7) == "zh-TW"
        assert _request_locale(runtime, {"ui_locale": "zh-CN"}, 7) == "zh-CN"

    def test_unsupported_request_language_falls_back_to_english(self, tmp_path):
        runtime = _runtime(tmp_path, default_locale="zh-TW")
        assert _request_locale(runtime, {"ui_locale": "ja"}, 7) == "en"

    def test_missing_request_language_keeps_the_actor_preference(self, tmp_path):
        runtime = _runtime(tmp_path, default_locale="zh-TW")
        assert _request_locale(runtime, {}, 7) == "zh-TW"
        assert _request_locale(runtime, {"ui_locale": "   "}, 7) == "zh-TW"

    def test_saved_preference_is_used_when_the_request_carries_none(self, tmp_path):
        runtime = _runtime(tmp_path, default_locale="en")
        ui_language.set_preferred_locale(runtime, "zh-TW", actor_id=7)
        assert _request_locale(runtime, {}, 7) == "zh-TW"
        assert _request_locale(runtime, {"ui_locale": "en"}, 7) == "en"
# HASHI-Fingerprint: hfp_85fdd83c154d4054bcb98f1a719a81d9
