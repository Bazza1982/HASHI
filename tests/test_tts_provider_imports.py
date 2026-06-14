from __future__ import annotations

import sys

import pytest


def test_voice_manager_import_does_not_require_edge_tts():
    sys.modules.pop("edge_tts", None)

    from orchestrator.voice_manager import VoiceManager
    from orchestrator.tts_providers import list_provider_names

    assert VoiceManager is not None
    assert "edge" in list_provider_names()


def test_build_edge_provider_reports_missing_optional_dependency(monkeypatch):
    from orchestrator import tts_providers

    real_import_module = tts_providers.importlib.import_module

    def fake_import_module(name):
        if name == "orchestrator.tts_providers.edge":
            raise ModuleNotFoundError("No module named 'edge_tts'", name="edge_tts")
        return real_import_module(name)

    monkeypatch.setattr(tts_providers.importlib, "import_module", fake_import_module)

    with pytest.raises(RuntimeError, match="edge_tts"):
        tts_providers.build_provider("edge")
