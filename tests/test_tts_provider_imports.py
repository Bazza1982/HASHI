from __future__ import annotations

import builtins
import sys
from pathlib import Path


def test_voice_manager_import_does_not_require_edge_tts():
    sys.modules.pop("edge_tts", None)

    from orchestrator.voice_manager import VoiceManager
    from orchestrator.tts_providers import list_provider_names

    assert VoiceManager is not None
    assert "edge" in list_provider_names()


def test_build_edge_provider_does_not_import_optional_dependency(monkeypatch):
    from orchestrator.tts_providers import build_provider

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "edge_tts":
            raise ModuleNotFoundError("No module named 'edge_tts'", name="edge_tts")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    provider = build_provider("edge")

    assert provider.provider_name == "edge"


def test_voice_generation_contains_configured_providers_catalogue_and_isolated_worker():
    from orchestrator.function_generation import build_source_manifest

    root = Path(__file__).resolve().parents[1]
    manifest = build_source_manifest(
        ["orchestrator.voice_manager"],
        code_root=root,
    )

    assert {
        "orchestrator.openai_compatible_speech",
        "orchestrator.tts_providers.edge",
        "orchestrator.tts_providers.openai_compatible",
        "orchestrator.voice_catalog",
        "orchestrator.voice_synthesis_runtime",
        "orchestrator.voice_synthesis_worker",
    } <= set(manifest.module_names)
    assert "orchestrator/voice_catalog.default.json" in {
        asset.relative_path for asset in manifest.assets
    }
