from __future__ import annotations
import copy
import functools
import hashlib
import html
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator import ui_language
from orchestrator.compute_services import ComputeServiceError, ComputeServicesConfig
from orchestrator.config_json import ConfigConflictError, read_config_json, write_config_json
from orchestrator.tts_routing import fallback_providers, normalize_language, resolve_tts_route
from orchestrator.tts_providers import build_provider, list_provider_names
from orchestrator.tts_voice_previews import (
    DEFAULT_TTS_VOICE_PREVIEW_ASSET_ROOT,
    get_tts_voice_preview_assets as resolve_tts_voice_preview_assets,
)
from orchestrator.voice_preview_bundle import (
    DEFAULT_VOICE_PREVIEW_ASSET_ROOT,
    VOICE_PREVIEW_VERSION as PRODUCT_VOICE_PREVIEW_VERSION,
    VoicePreviewBundleError,
    validate_voice_preview_bundle,
)
from orchestrator.voice_synthesis_runtime import (
    isolated_tts_configured,
    isolated_tts_supports,
    resolve_tts_python,
)
from orchestrator.voice_synthesizer import (
    VoiceAsset,
    VoiceProviderFailure,
    VoiceSynthesisError,
    safe_voice_error_detail,
    spoken_text_language,
)
from orchestrator.voice_services import (
    VoiceChoice,
    VoiceService,
    VoiceServiceCatalogue,
    discover_voice_services,
)
from orchestrator.command_ui import setting_card


def _default_tts_provider() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "edge"


def _provider_label(provider_name: str) -> str:
    return {
        "edge": "Edge TTS",
        "windows": "Windows TTS",
        "macos": "macOS TTS",
        "piper": "Piper TTS",
        "kokoro": "Kokoro TTS",
        "coqui": "Coqui TTS",
        "hashi_compute": "Hashi Compute TTS",
        "openai_compatible": "Gemini TTS via OpenRouter",
    }.get(str(provider_name or "").strip().casefold(), f"{provider_name or 'TTS'} provider")


def _provider_failure(provider_name: str, exc: Exception) -> VoiceProviderFailure:
    provider = str(provider_name or "unknown").strip().casefold() or "unknown"
    label = _provider_label(provider)
    detail = safe_voice_error_detail(exc)
    lowered = detail.casefold()
    declared_code = str(getattr(exc, "code", "") or "").strip().casefold()
    declared_retryable = getattr(exc, "retryable", None)
    status = getattr(exc, "status", None)

    if (
        declared_code == "tts_protocol_rejected"
        or status == 403
        or ("wsserverhandshakeerror" in lowered and re.search(r"\b403\b", lowered))
    ):
        return VoiceProviderFailure(
            provider,
            "tts_protocol_rejected",
            f"{label} rejected the speech request (HTTP 403).",
            False,
        )
    if declared_code == "tts_timeout" or "timed out" in lowered or "second limit" in lowered:
        return VoiceProviderFailure(
            provider,
            "tts_timeout",
            f"{label} did not respond before the speech request timed out.",
            True,
        )
    if "no installed windows tts voice for language" in lowered:
        language = re.search(r"language:\s*([a-z-]+)", detail, flags=re.IGNORECASE)
        suffix = f" for language: {language.group(1)}" if language else ""
        return VoiceProviderFailure(
            provider,
            "tts_voice_unavailable",
            f"{label} has no installed voice{suffix}.",
            False,
        )
    if "configured windows tts voice is unavailable" in lowered:
        return VoiceProviderFailure(
            provider,
            "tts_voice_unavailable",
            f"{label} cannot find the configured voice.",
            False,
        )
    if "isolated tts runtime" in lowered or declared_code == "tts_runtime_failed":
        return VoiceProviderFailure(
            provider,
            "tts_runtime_unavailable",
            f"{label} runtime is unavailable or misconfigured: {detail}",
            False,
        )
    if "no spoken text" in lowered or "speech text is required" in lowered:
        return VoiceProviderFailure(
            provider,
            "tts_text_invalid",
            f"{label} received no speakable text.",
            False,
        )
    if "ffmpeg" in lowered or "malformed ogg" in lowered:
        return VoiceProviderFailure(
            provider,
            "tts_audio_conversion_failed",
            f"{label} generated audio, but conversion to a playable file failed: {detail}",
            False,
        )
    return VoiceProviderFailure(
        provider,
        declared_code or "tts_provider_failed",
        detail if detail.casefold().startswith(label.casefold()) else f"{label} failed: {detail}",
        bool(declared_retryable) if declared_retryable is not None else True,
    )


@functools.lru_cache(maxsize=1)
def _installed_windows_voice_names() -> frozenset[str]:
    """Return the enabled SAPI voices without claiming unavailable presets."""

    if sys.platform != "win32":
        return frozenset()
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
        "Add-Type -AssemblyName System.Speech\n"
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "try {\n"
        "  $synth.GetInstalledVoices() | Where-Object { $_.Enabled } | "
        "ForEach-Object { $_.VoiceInfo.Name }\n"
        "} finally { $synth.Dispose() }\n"
    )
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if completed.returncode:
        return frozenset()
    return frozenset(line.strip() for line in completed.stdout.splitlines() if line.strip())


class VoiceManager:
    PROVIDER_FAILURE_COOLDOWN_SECONDS = 300.0
    PROJECT_ROOT = Path(
        os.environ.get("HASHI_SOURCE_ROOT")
        or os.environ.get("BRIDGE_HOME")
        or Path(__file__).resolve().parent.parent
    ).resolve()
    PIPER_MODEL_DIR = PROJECT_ROOT / "voice_models" / "piper"
    VOICE_PREVIEW_VERSION = PRODUCT_VOICE_PREVIEW_VERSION
    VOICE_PREVIEW_RENDERERS = ("native", "tts")
    DEFAULT_STATE = {
        "schema_version": 2,
        "enabled": False,
        "mode": "text_and_voice",
        "provider": _default_tts_provider(),
        "voice_name": None,
        "rate": 0,
        "max_chars": 1200,
        "provider_options": {},
        # A user-facing semantic profile is independent from the concrete TTS
        # voice and from provider-specific performance instructions.  It may
        # guide selection of a compatible voice, but never becomes a style
        # prompt.  None means the user has not chosen a semantic profile.
        "voice_profile": None,
        # Ordered, complete TTS routes.  Index 0 is the preferred route; every
        # later entry is an explicit fallback.  Endpoint URLs, credentials and
        # privacy labels are resolved from their owning service catalogues and
        # are never trusted from this Agent-owned file.
        "tts_routes": [],
        "external_fallback_enabled": False,
        # Controls when text-model replies receive a TTS rendition.  It is
        # deliberately independent from the synthesis mode and route chain.
        "reply_trigger": "all",
        # Controls the delivered reply shape for every voice path.  The
        # nested native copy remains a compatibility mirror for native-audio
        # terminal overrides; this top-level value is the Agent default.
        "reply_content": "audio_and_text",
        # Voice is available, but automatic speech requires explicit opt-in.
        # A user's saved off/native/tts choice remains exact.
        "native": {
            "mode": "off",
            "reply_trigger": "voice_message",
            "reply_content": "audio_and_text",
            "provider": None,
            "model": None,
            "voice": None,
            "format": None,
            "fallback": "local_chain",
            "input_transcript_echo": False,
            "output_transcript_echo": True,
            "retention_seconds": 3600,
            "audio_model_tools": False,
            "terminal_overrides": {},
        },
    }
    VOICE_PRESETS = {
        "eus": {
            "provider": "edge",
            "voice_name": "en-US-EmmaNeural",
            "label": "Emma [US Edge]",
            "language": "English",
            "locale": "en-US",
            "gender": "female",
        },
        "eusm": {
            "provider": "edge",
            "voice_name": "en-US-GuyNeural",
            "label": "Guy [US Edge]",
            "language": "English",
            "locale": "en-US",
            "gender": "male",
        },
        "euk": {
            "provider": "edge",
            "voice_name": "en-GB-SoniaNeural",
            "label": "Sonia [UK Edge]",
            "language": "English",
            "locale": "en-GB",
            "gender": "female",
        },
        "eukm": {
            "provider": "edge",
            "voice_name": "en-GB-RyanNeural",
            "label": "Ryan [UK Edge]",
            "language": "English",
            "locale": "en-GB",
            "gender": "male",
        },
        "ecn": {
            "provider": "edge",
            "voice_name": "zh-CN-XiaoxiaoNeural",
            "label": "Xiaoxiao [CN Edge]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "female",
        },
        "ecny": {
            "provider": "edge",
            "voice_name": "zh-CN-XiaoyiNeural",
            "label": "Xiaoyi [CN Edge]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "female",
        },
        "ecnm": {
            "provider": "edge",
            "voice_name": "zh-CN-YunxiNeural",
            "label": "Yunxi [CN Edge]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "male",
        },
        # XiaochenNeural and XiaohanNeural discontinued by Microsoft (2026-03)
        "ectc": {
            "provider": "edge",
            "voice_name": "zh-TW-HsiaoChenNeural",
            "label": "HsiaoChen [TW Edge]",
            "language": "Chinese",
            "locale": "zh-TW",
            "gender": "female",
        },
        "ecty": {
            "provider": "edge",
            "voice_name": "zh-TW-HsiaoYuNeural",
            "label": "HsiaoYu [TW Edge]",
            "language": "Chinese",
            "locale": "zh-TW",
            "gender": "female",
        },
        "ectm": {
            "provider": "edge",
            "voice_name": "zh-TW-YunJheNeural",
            "label": "YunJhe [TW Edge]",
            "language": "Chinese",
            "locale": "zh-TW",
            "gender": "male",
        },
        "echm": {
            "provider": "edge",
            "voice_name": "zh-HK-HiuMaanNeural",
            "label": "HiuMaan [HK Edge]",
            "language": "Chinese",
            "locale": "zh-HK",
            "gender": "female",
        },
        "echg": {
            "provider": "edge",
            "voice_name": "zh-HK-HiuGaaiNeural",
            "label": "HiuGaai [HK Edge]",
            "language": "Chinese",
            "locale": "zh-HK",
            "gender": "female",
        },
        "echkm": {
            "provider": "edge",
            "voice_name": "zh-HK-WanLungNeural",
            "label": "WanLung [HK Edge]",
            "language": "Chinese",
            "locale": "zh-HK",
            "gender": "male",
        },
        "eja": {
            "provider": "edge",
            "voice_name": "ja-JP-NanamiNeural",
            "label": "Nanami [JP Edge]",
            "language": "Japanese",
            "locale": "ja-JP",
            "gender": "female",
        },
        "ejam": {
            "provider": "edge",
            "voice_name": "ja-JP-KeitaNeural",
            "label": "Keita [JP Edge]",
            "language": "Japanese",
            "locale": "ja-JP",
            "gender": "male",
        },
        "us": {
            "provider": "piper",
            "voice_name": str(PIPER_MODEL_DIR / "en_US-lessac-high.onnx"),
            "label": "Lessac [Piper-US]",
            "language": "English",
            "locale": "en-US",
            "gender": "female",
        },
        "uk": {
            "provider": "piper",
            "voice_name": str(PIPER_MODEL_DIR / "en_GB-cori-high.onnx"),
            "label": "Cori [Piper-UK]",
            "language": "English",
            "locale": "en-GB",
            "gender": "female",
        },
        "pcn": {
            "provider": "piper",
            "voice_name": str(PIPER_MODEL_DIR / "zh_CN-huayan-medium.onnx"),
            "label": "Huayan [Piper-CN]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "female",
        },
        "cn": {
            "provider": "windows",
            "voice_name": "Microsoft Huihui Desktop",
            "label": "Huihui [Win-CN]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "female",
        },
        "wus": {
            "provider": "windows",
            "voice_name": "Microsoft Zira Desktop",
            "label": "Zira [Win-US]",
            "language": "English",
            "locale": "en-US",
            "gender": "female",
        },
        "wusm": {
            "provider": "windows",
            "voice_name": "Microsoft David Desktop",
            "label": "David [Win-US]",
            "language": "English",
            "locale": "en-US",
            "gender": "male",
        },
        # Kokoro local TTS presets — runs on CPU, no API key needed
        "ko_bella": {
            "provider": "kokoro",
            "voice_name": "af_bella",
            "label": "Bella [Kokoro EN]",
            "language": "English",
            "locale": "en-US",
            "gender": "female",
            "provider_options": {"lang_code": "a"},
        },
        "ko_heart": {
            "provider": "kokoro",
            "voice_name": "af_heart",
            "label": "Heart [Kokoro EN]",
            "language": "English",
            "locale": "en-US",
            "gender": "female",
            "provider_options": {"lang_code": "a"},
        },
        "ko_michael": {
            "provider": "kokoro",
            "voice_name": "am_michael",
            "label": "Michael [Kokoro EN-US]",
            "language": "English",
            "locale": "en-US",
            "gender": "male",
            "provider_options": {"lang_code": "a"},
        },
        "ko_emma": {
            "provider": "kokoro",
            "voice_name": "bf_emma",
            "label": "Emma [Kokoro EN-GB]",
            "language": "English",
            "locale": "en-GB",
            "gender": "female",
            "provider_options": {"lang_code": "b"},
        },
        "ko_george": {
            "provider": "kokoro",
            "voice_name": "bm_george",
            "label": "George [Kokoro EN-GB]",
            "language": "English",
            "locale": "en-GB",
            "gender": "male",
            "provider_options": {"lang_code": "b"},
        },
        "ko_xiaoxiao": {
            "provider": "kokoro",
            "voice_name": "zf_xiaoxiao",
            "label": "Xiaoxiao [Kokoro ZH]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "female",
            "provider_options": {"lang_code": "z"},
        },
        "ko_xiaobei": {
            "provider": "kokoro",
            "voice_name": "zf_xiaobei",
            "label": "Xiaobei [Kokoro ZH]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "female",
            "provider_options": {"lang_code": "z"},
        },
        "ko_yunxi": {
            "provider": "kokoro",
            "voice_name": "zm_yunxi",
            "label": "Yunxi [Kokoro ZH]",
            "language": "Chinese",
            "locale": "zh-CN",
            "gender": "male",
            "provider_options": {"lang_code": "z"},
        },
        "ko_alpha": {
            "provider": "kokoro",
            "voice_name": "jf_alpha",
            "label": "Alpha [Kokoro JA]",
            "language": "Japanese",
            "locale": "ja-JP",
            "gender": "female",
            "provider_options": {"lang_code": "j"},
        },
        "ko_kumo": {
            "provider": "kokoro",
            "voice_name": "jm_kumo",
            "label": "Kumo [Kokoro JA]",
            "language": "Japanese",
            "locale": "ja-JP",
            "gender": "male",
            "provider_options": {"lang_code": "j"},
        },
    }

    # Keep the main /voice surface provider-neutral and deliberately small.
    # These IDs and TTS mappings align with Aptenra's four voice profiles.  The
    # native candidate lists are ordered preferences: capability declarations
    # decide which concrete provider voice is safe to persist.
    VOICE_PROFILES = {
        "warm_female": {
            "label": "👩 Warm",
            "native_voices": ("marin", "shimmer", "alloy"),
            "tts_voices": {
                "en": "en-US-EmmaMultilingualNeural",
                "zh": "zh-CN-XiaoxiaoNeural",
                "ja": "ja-JP-NanamiNeural",
            },
        },
        "clear_female": {
            "label": "👩 Clear",
            "native_voices": ("coral", "sage", "shimmer"),
            "tts_voices": {
                "en": "en-US-AriaNeural",
                "zh": "zh-CN-XiaoyiNeural",
                "ja": "ja-JP-AoiNeural",
            },
        },
        "warm_male": {
            "label": "👨 Warm",
            "native_voices": ("cedar", "verse", "ash"),
            "tts_voices": {
                "en": "en-US-GuyNeural",
                "zh": "zh-CN-YunxiNeural",
                "ja": "ja-JP-KeitaNeural",
            },
        },
        "calm_male": {
            "label": "👨 Calm",
            "native_voices": ("echo", "ash", "ballad"),
            "tts_voices": {
                "en": "en-US-ChristopherNeural",
                "zh": "zh-CN-YunjianNeural",
                "ja": "ja-JP-NaokiNeural",
            },
        },
    }

    MACOS_VOICE_PRESETS = {
        "maus_ava": {
            "provider": "macos",
            "voice_name": "Ava",
            "label": "Ava [macOS EN-US]",
            "language": "en-US",
            "locale": "en-US",
            "gender": "female",
        },
        "maus_samantha": {
            "provider": "macos",
            "voice_name": "Samantha",
            "label": "Samantha [macOS EN-US]",
            "language": "en-US",
            "locale": "en-US",
            "gender": "female",
        },
        "maus_alex": {
            "provider": "macos",
            "voice_name": "Alex",
            "label": "Alex [macOS EN-US male]",
            "language": "en-US",
            "locale": "en-US",
            "gender": "male",
        },
        "maus_daniel": {
            "provider": "macos",
            "voice_name": "Daniel",
            "label": "Daniel [macOS EN-GB]",
            "language": "en-GB",
            "locale": "en-GB",
            "gender": "male",
        },
        "maus_moira": {
            "provider": "macos",
            "voice_name": "Moira",
            "label": "Moira [macOS EN-IE]",
            "language": "en-IE",
            "locale": "en-IE",
            "gender": "female",
        },
    }

    if sys.platform == "darwin":
        VOICE_PRESETS.update(MACOS_VOICE_PRESETS)

    def __init__(
        self,
        workspace_dir: Path,
        media_dir: Path,
        ffmpeg_cmd: str = "ffmpeg",
        secrets: dict | None = None,
        native_capabilities: list[dict] | None = None,
        preview_asset_root: Path | None = None,
        tts_preview_asset_root: Path | None = None,
        project_root: Path | None = None,
    ):
        self.workspace_dir = workspace_dir
        self.media_dir = media_dir
        self.state_path = workspace_dir / "voice_state.json"
        self.output_dir = media_dir / "voice"
        self.ffmpeg_cmd = ffmpeg_cmd
        self.project_root = Path(project_root or self.PROJECT_ROOT).resolve()
        self.preview_asset_root = Path(
            preview_asset_root or DEFAULT_VOICE_PREVIEW_ASSET_ROOT
        ).resolve()
        self.tts_preview_asset_root = Path(
            tts_preview_asset_root or DEFAULT_TTS_VOICE_PREVIEW_ASSET_ROOT
        ).resolve()
        self._secrets = secrets or {}
        self._native_capabilities = tuple(
            dict(item) for item in (native_capabilities or ()) if isinstance(item, dict)
        )
        self._provider_cooldowns: dict[str, tuple[float, VoiceProviderFailure]] = {}
        self.voice_audit_path = self.workspace_dir / "logs" / "voice_synthesis.jsonl"
        self._voice_audit_lock = threading.Lock()

    def _active_provider_failure(self, cooldown_key: str) -> VoiceProviderFailure | None:
        key = str(cooldown_key or "").strip().casefold()
        current = self._provider_cooldowns.get(key)
        if current is None:
            return None
        unavailable_until, failure = current
        if time.monotonic() >= unavailable_until:
            self._provider_cooldowns.pop(key, None)
            return None
        return VoiceProviderFailure(
            failure.provider,
            failure.code,
            f"{failure.reason} {_provider_label(failure.provider)} is temporarily bypassed after that failure.",
            False,
        )

    def _remember_provider_failure(
        self,
        failure: VoiceProviderFailure,
        *,
        cooldown_key: str | None = None,
    ) -> None:
        key = str(cooldown_key or failure.provider).strip().casefold()
        self._provider_cooldowns[key] = (
            time.monotonic() + self.PROVIDER_FAILURE_COOLDOWN_SECONDS,
            failure,
        )

    @classmethod
    def _tts_route_cooldown_key(cls, route: Mapping[str, Any]) -> str:
        provider = cls._provider_for_tts_route(route)
        if provider == "hashi_compute":
            return "|".join(
                (
                    provider,
                    str(route.get("node_id") or ""),
                    str(route.get("offering_id") or ""),
                    str(route.get("model") or ""),
                )
            ).casefold()
        if provider == "openai_compatible":
            return f"{provider}|{route.get('model') or ''}".casefold()
        return provider

    def _default_piper_exe(self) -> str:
        piper_exe = Path(sys.executable).with_name("piper.exe")
        if piper_exe.exists():
            return str(piper_exe)
        return shutil.which("piper") or "piper"

    def _default_python_exe(self) -> str:
        return sys.executable

    def _available_windows_voice_names(self) -> frozenset[str]:
        return _installed_windows_voice_names()

    def _provider_status(self, provider_name: str) -> str:
        name = (provider_name or "").strip().lower()
        if name == "windows":
            return "installed" if self._available_windows_voice_names() else "not installed"
        if name == "edge":
            return (
                "installed"
                if isolated_tts_configured() or importlib.util.find_spec("edge_tts")
                else "not installed"
            )
        if name == "piper":
            exe = self._default_piper_exe()
            executable_available = Path(exe).is_file() or shutil.which(exe) is not None
            return (
                "installed"
                if importlib.util.find_spec("piper") or executable_available
                else "not installed"
            )
        if name == "kokoro":
            return (
                "installed"
                if isolated_tts_supports("kokoro") or importlib.util.find_spec("kokoro")
                else "not installed"
            )
        if name == "coqui":
            return f"not installed in Python {sys.version_info.major}.{sys.version_info.minor}"
        return "unknown"

    def _preset_payload(self, alias: str) -> dict | None:
        key = (alias or "").strip().lower()
        preset = self.VOICE_PRESETS.get(key)
        if not preset:
            return None
        payload = dict(preset)
        provider_options = dict(payload.get("provider_options") or {})
        if payload.get("provider") == "piper":
            provider_options.setdefault("exe", self._default_piper_exe())
            provider_options.setdefault("python_exe", self._default_python_exe())
            provider_options.setdefault(
                "module_mode", importlib.util.find_spec("piper") is not None
            )
        if payload.get("provider") == "kokoro":
            try:
                python = resolve_tts_python()
            except RuntimeError:
                python = None
            if python is not None:
                provider_options.setdefault("python_exe", str(python))
                provider_options.setdefault(
                    "model_cache", str(python.parent.parent / "model-cache")
                )
        payload["provider_options"] = provider_options
        return payload

    def get_active_preset_alias(self) -> str | None:
        state = self._load()
        provider = state.get("provider")
        voice_name = state.get("voice_name")
        provider_options = state.get("provider_options") or {}
        for alias, preset in self.VOICE_PRESETS.items():
            payload = self._preset_payload(alias) or {}
            if (
                provider == payload.get("provider")
                and voice_name == payload.get("voice_name")
                and (
                    provider_options == (payload.get("provider_options") or {})
                    or self._durable_route_options(provider_options)
                    == self._durable_route_options(
                        payload.get("provider_options") or {}
                    )
                )
            ):
                return alias
        return None

    def list_voice_presets(self) -> str:
        lines = ["Voice presets"]
        active_alias = self.get_active_preset_alias()
        for alias, preset, available in self.get_voice_presets():
            marker = ">>" if alias == active_alias else "  "
            lines.append(f"{marker} {alias}: {preset['label']} [{available}]")
        lines.append("")
        lines.append("Use: /voice use <alias>")
        return "\n".join(lines)

    def get_voice_presets(self) -> list[tuple[str, dict, str]]:
        rows: list[tuple[str, dict, str]] = []
        windows_voices = self._available_windows_voice_names()
        provider_status = {
            provider: self._provider_status(provider)
            for provider in {str(item["provider"]) for item in self.VOICE_PRESETS.values()}
            if provider not in {"piper", "windows"}
        }
        for alias, preset in self.VOICE_PRESETS.items():
            available = "ready"
            if preset["provider"] == "piper":
                available = "ready" if Path(preset["voice_name"]).exists() else "missing model"
            elif preset["provider"] == "windows":
                available = (
                    "ready"
                    if str(preset["voice_name"]) in windows_voices
                    else "missing system voice"
                )
            elif provider_status[preset["provider"]] != "installed":
                available = provider_status[preset["provider"]]
            rows.append((alias, dict(preset), available))
        return rows

    def get_voice_profiles(self) -> list[tuple[str, dict]]:
        return [
            (profile_id, copy.deepcopy(profile))
            for profile_id, profile in self.VOICE_PROFILES.items()
        ]

    @staticmethod
    def _text_language(text: str) -> str:
        return spoken_text_language(text)

    @classmethod
    def _voice_profile_from_state(cls, state: dict) -> str | None:
        configured = str(state.get("voice_profile") or "").strip().casefold()
        if configured in cls.VOICE_PROFILES:
            return configured
        return None

    def get_voice_profile_id(self) -> str | None:
        return self._voice_profile_from_state(self._load())

    def _supported_native_voices(self, policy: dict) -> tuple[str, ...]:
        provider = str(policy.get("provider") or "").strip().casefold()
        model = str(policy.get("model") or "").strip().casefold()
        if not provider or not model:
            return ()
        for row in self._native_capabilities:
            engine = str(row.get("engine") or "").strip().casefold()
            if engine != provider:
                continue
            models: list[str] = []
            if row.get("model"):
                models.append(str(row["model"]))
            raw_models = row.get("models")
            if isinstance(raw_models, (list, tuple)):
                for item in raw_models:
                    if isinstance(item, dict):
                        value = item.get("id") or item.get("model") or item.get("name")
                    else:
                        value = item
                    if value:
                        models.append(str(value))
            if models and model not in {value.strip().casefold() for value in models}:
                continue
            voices = row.get("supported_voices") or row.get("native_audio_voices")
            if isinstance(voices, str):
                voices = [voices]
            if isinstance(voices, (list, tuple)):
                return tuple(
                    str(value).strip()
                    for value in voices
                    if str(value or "").strip()
                )
        return ()

    @staticmethod
    def _capability_values(value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            values = (value,)
        elif isinstance(value, (list, tuple, set, frozenset)):
            values = tuple(value)
        else:
            values = ()
        return tuple(
            str(item).strip()
            for item in values
            if str(item or "").strip()
        )

    @classmethod
    def _is_complete_native_audio_capability(cls, row: dict) -> bool:
        """Require every capability dimension used by the native route."""

        nested = row.get("extra")
        resolved = dict(nested) if isinstance(nested, dict) else {}
        resolved.update(row)
        input_modalities = {
            value.casefold()
            for value in cls._capability_values(resolved.get("input_modalities"))
        }
        output_modalities = {
            value.casefold()
            for value in cls._capability_values(resolved.get("output_modalities"))
        }
        input_transports = resolved.get("input_transports")
        input_formats = resolved.get("input_formats")
        output_formats = resolved.get("output_formats")
        audio_transports = (
            cls._capability_values(input_transports.get("audio"))
            if isinstance(input_transports, dict)
            else ()
        )
        audio_input_formats = (
            cls._capability_values(input_formats.get("audio"))
            if isinstance(input_formats, dict)
            else ()
        )
        audio_output_formats = (
            cls._capability_values(output_formats.get("audio"))
            if isinstance(output_formats, dict)
            else ()
        )
        output_streaming = str(
            resolved.get("output_streaming") or ""
        ).strip().casefold()
        api_surface = str(resolved.get("api_surface") or "").strip().casefold()
        supported_voices = cls._capability_values(
            resolved.get("supported_voices")
            or resolved.get("native_audio_voices")
        )
        return bool(
            str(resolved.get("engine") or "").strip()
            and str(resolved.get("model") or "").strip()
            and "audio" in input_modalities
            and "audio" in output_modalities
            and audio_transports
            and audio_input_formats
            and audio_output_formats
            and output_streaming not in {"", "none", "unknown"}
            and api_surface not in {"", "none", "unknown"}
            and supported_voices
        )

    def _native_audio_targets(self) -> tuple[tuple[str, str], ...]:
        targets: list[tuple[str, str]] = []
        for row in self._native_capabilities:
            if not self._is_complete_native_audio_capability(row):
                continue
            target = (
                str(row.get("engine") or "").strip(),
                str(row.get("model") or "").strip(),
            )
            if target not in targets:
                targets.append(target)
        return tuple(targets)

    @staticmethod
    def _target_matches(
        provider: object,
        model: object,
        target: tuple[str, str],
    ) -> bool:
        return (
            str(provider or "").strip().casefold() == target[0].casefold()
            and str(model or "").strip().casefold() == target[1].casefold()
        )

    def _prepare_native_mode(self, state: dict, native: dict) -> None:
        """Resolve a declared target before native mode becomes persistent."""

        targets = self._native_audio_targets()
        selected = next(
            (
                target
                for target in targets
                if self._target_matches(
                    native.get("provider"), native.get("model"), target
                )
            ),
            None,
        )
        if selected is None and targets:
            selected = targets[0]
        if selected is None:
            # Tests and advanced embedders may construct VoiceManager without
            # the Agent capability registry.  A complete manually configured
            # target remains valid there; a real Agent always supplies the
            # registry and therefore fails closed when no audio row qualifies.
            manually_configured = bool(
                not self._native_capabilities
                and native.get("provider")
                and native.get("model")
            )
            if not manually_configured:
                raise RuntimeError(ui_language.tr("voice.native.unavailable"))
        else:
            native["provider"], native["model"] = selected

        profile_id = self._voice_profile_from_state(state)
        if profile_id is None and selected is not None:
            profile_id = "warm_female"
            state["voice_profile"] = profile_id
        if profile_id is not None:
            native["voice"] = self._native_voice_for_profile(
                profile_id, native
            )

    def _native_voice_for_profile(self, profile_id: str, policy: dict) -> str:
        profile = self.VOICE_PROFILES[profile_id]
        candidates = tuple(
            str(value).strip()
            for value in profile.get("native_voices") or ()
            if str(value or "").strip()
        )
        supported = self._supported_native_voices(policy)
        if supported:
            indexed = {value.casefold(): value for value in supported}
            for candidate in candidates:
                if candidate.casefold() in indexed:
                    return indexed[candidate.casefold()]
            current = str(policy.get("voice") or "").strip()
            if current.casefold() in indexed:
                return indexed[current.casefold()]
            return supported[0]
        current = str(policy.get("voice") or "").strip()
        return candidates[0] if candidates else current

    def _tts_voice_for_profile(self, profile_id: str, text: str) -> str | None:
        profile = self.VOICE_PROFILES.get(profile_id)
        if not profile:
            return None
        voices = profile.get("tts_voices") or {}
        language = self._text_language(text)
        return str(voices.get(language) or voices.get("en") or "").strip() or None

    def set_voice_profile(self, profile_id: str) -> str:
        key = str(profile_id or "").strip().casefold()
        profile = self.VOICE_PROFILES.get(key)
        if not profile:
            raise RuntimeError(
                f"Unknown voice profile: {profile_id}. "
                f"Available: {', '.join(self.VOICE_PROFILES)}"
            )
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            native["voice"] = self._native_voice_for_profile(key, native)
            state["native"] = native
            state["voice_profile"] = key

        state = self._update(mutate)
        native = self._native_policy_from_state(state)
        return (
            f"Voice set to {profile['label']} "
            f"(native {native['voice']}; unsupported TTS style controls are ignored safely)."
        )

    def get_reply_mode(self) -> str:
        state = self._load()
        native_mode = self._native_policy_from_state(state)["mode"]
        if native_mode in {"native", "tts"}:
            return native_mode
        return "tts" if state.get("enabled") else "off"

    def set_reply_mode(self, mode: str) -> str:
        normalized = str(mode or "").strip().casefold()
        # ``auto`` never acquired behaviour distinct from ``native``.  Keep
        # accepting the old value so persisted state, typed commands, and
        # already-open Telegram keyboards migrate without an error.
        if normalized == "auto":
            normalized = "native"
        if normalized not in {"off", "tts", "native"}:
            raise RuntimeError("Voice mode must be off, tts, or native.")
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            if normalized == "native":
                self._prepare_native_mode(state, native)
            native["mode"] = normalized
            state["native"] = native
            state["enabled"] = normalized == "tts"

        self._update(mutate)
        return f"Voice mode set to {normalized.upper()}."

    _VOICE_LOCAL_NAMES = {
        "ecnm": "云希",
        "ecn": "晓晓",
        "ecny": "晓伊",
        "ectc": "曉臻",
        "ecty": "曉雨",
        "ectm": "雲哲",
        "echm": "曉曼",
        "echg": "曉佳",
        "echkm": "雲龍",
        "eja": "七海",
        "ejam": "圭太",
        "pcn": "华妍",
        "cn": "慧慧",
        "ko_xiaoxiao": "晓晓",
        "ko_xiaobei": "晓蓓",
        "ko_yunxi": "云希",
    }
    _VOICE_LANGUAGE_ANNOTATIONS = {
        "eus": ("English (US)", "美式英语"),
        "eusm": ("English (US)", "美式英语"),
        "euk": ("English (UK)", "英式英语"),
        "eukm": ("English (UK)", "英式英语"),
        "ecn": ("Simplified Chinese", "简中"),
        "ecny": ("Simplified Chinese", "简中"),
        "ecnm": ("Simplified Chinese", "简中"),
        "ectc": ("Traditional Chinese (Taiwan)", "繁中·台湾"),
        "ecty": ("Traditional Chinese (Taiwan)", "繁中·台湾"),
        "ectm": ("Traditional Chinese (Taiwan)", "繁中·台湾"),
        "echm": ("Cantonese (Hong Kong)", "粤语·香港"),
        "echg": ("Cantonese (Hong Kong)", "粤语·香港"),
        "echkm": ("Cantonese (Hong Kong)", "粤语·香港"),
        "eja": ("Japanese", "日语"),
        "ejam": ("Japanese", "日语"),
        "us": ("English (US)", "美式英语"),
        "uk": ("English (UK)", "英式英语"),
        "pcn": ("Simplified Chinese", "简中"),
        "cn": ("Simplified Chinese", "简中"),
        "wus": ("English (US)", "美式英语"),
        "wusm": ("English (US)", "美式英语"),
        "ko_bella": ("English (US)", "美式英语"),
        "ko_heart": ("English (US)", "美式英语"),
        "ko_michael": ("English (US)", "美式英语"),
        "ko_emma": ("English (UK)", "英式英语"),
        "ko_george": ("English (UK)", "英式英语"),
        "ko_xiaoxiao": ("Simplified Chinese", "简中"),
        "ko_xiaobei": ("Simplified Chinese", "简中"),
        "ko_yunxi": ("Simplified Chinese", "简中"),
        "ko_alpha": ("Japanese", "日语"),
        "ko_kumo": ("Japanese", "日语"),
    }
    _VOICE_PROVIDER_SHORT = {
        "edge": "Edge",
        "windows": "Windows",
        "macos": "macOS",
        "piper": "Piper",
        "kokoro": "Kokoro",
        "coqui": "Coqui",
    }

    def voice_preset_menu_label(self, alias: str, *, locale: str | None = None) -> str:
        key = str(alias or "").strip().casefold()
        preset = self.VOICE_PRESETS.get(key)
        if not preset:
            return key
        selected_locale = ui_language.normalize_locale(locale or ui_language.current_locale())
        raw_name = str(preset.get("label") or key).split("[", 1)[0].strip()
        name = (
            self._VOICE_LOCAL_NAMES.get(key, raw_name)
            if selected_locale == "zh-CN"
            else raw_name
        )
        annotations = self._VOICE_LANGUAGE_ANNOTATIONS.get(key)
        if annotations:
            language = annotations[1] if selected_locale == "zh-CN" else annotations[0]
        else:
            language = str(preset.get("language") or "Auto")
        provider = self._VOICE_PROVIDER_SHORT.get(
            str(preset.get("provider") or "").strip().casefold(),
            str(preset.get("provider") or "TTS"),
        )
        gender_id = str(preset.get("gender") or "").strip().casefold()
        gender = (
            ui_language.tr(f"voice.gender.{gender_id}", locale=selected_locale)
            if gender_id in {"female", "male"}
            else ""
        )
        details = " · ".join(part for part in (language, gender, provider) if part)
        if selected_locale == "zh-CN":
            return f"{name}（{details}）"
        return f"{name} ({details})"

    def active_tts_voice_label(self, *, locale: str | None = None) -> str:
        alias = self.get_active_preset_alias()
        if alias:
            return self.voice_preset_menu_label(alias, locale=locale)
        state = self._load()
        voice_name = str(state.get("voice_name") or "").strip()
        if voice_name:
            return voice_name
        return ui_language.tr("voice.tts.auto", locale=locale)

    def voice_choice_menu_text(self) -> str:
        return setting_card(
            "🗣️",
            ui_language.tr("voice.tts.title"),
            current=html.escape(self.active_tts_voice_label()),
            facts=[
                html.escape(ui_language.tr("voice.tts.language_rule")),
                html.escape(ui_language.tr("voice.tts.english_rule")),
                html.escape(ui_language.tr("voice.tts.temporary_rule")),
            ],
            action=ui_language.tr("voice.tts.action"),
        )

    def voice_menu_text(self) -> str:
        state = self._load()
        mode = self.get_reply_mode()
        profile_id = self._voice_profile_from_state(state)
        profile_label = (
            ui_language.tr(f"voice.profile.{profile_id}")
            if profile_id
            else ui_language.tr("voice.profile.unset")
        )
        mode_label = ui_language.tr(f"voice.mode.{mode}")
        current = f"<b>{html.escape(mode.upper())}</b>"
        if ui_language.current_locale() != ui_language.DEFAULT_LOCALE:
            current = f"<b>{html.escape(mode_label)}</b> · <code>{html.escape(mode)}</code>"
        trigger_label = ui_language.tr(
            f"voice.trigger.{self.get_reply_trigger()}"
        )
        reply_content_label = ui_language.tr(
            f"voice.reply.{self.get_reply_content()}"
        )
        route_facts = [
            f"<b>{html.escape(ui_language.tr('voice.section.rules'))}</b> · "
            f"{html.escape(trigger_label)} · {html.escape(mode_label)} · "
            f"{html.escape(reply_content_label)}",
            f"<b>{html.escape(ui_language.tr('voice.section.routes'))}</b>",
        ]
        routes = self.get_tts_routes()
        if routes:
            for index, route in enumerate(routes):
                role = (
                    ui_language.tr("voice.route.preferred")
                    if index == 0
                    else ui_language.tr("voice.route.fallback", index=index)
                )
                route_facts.append(
                    f"• <b>{html.escape(role)}</b> · "
                    f"{html.escape(self.tts_route_label(route))} · "
                    f"{html.escape(self.tts_route_privacy_label(route))}"
                )
        else:
            route_facts.append(
                f"• {html.escape(ui_language.tr('voice.route.not_configured'))}"
            )
        route_facts.extend(
            [
                (
                    f"<b>{html.escape(ui_language.tr('voice.external.title'))}</b> · "
                    + html.escape(
                        ui_language.tr(
                            "voice.external.allowed"
                            if state.get("external_fallback_enabled")
                            else "voice.external.blocked"
                        )
                    )
                ),
                (
                    f"<b>{html.escape(ui_language.tr('voice.field.voice'))}</b> · "
                    f"{html.escape(str(profile_label))}"
                ),
            ]
        )
        return setting_card(
            "🔊",
            ui_language.tr("voice.title"),
            current=current,
            facts=route_facts,
            action=ui_language.tr("voice.menu.action"),
        )

    @staticmethod
    def voice_service_privacy_label(service: VoiceService) -> str:
        if service.privacy == "local_private":
            return ui_language.tr(
                "voice.privacy.local_lan"
                if service.location == "lan"
                else "voice.privacy.local_device"
            )
        return ui_language.tr("voice.privacy.external")

    @staticmethod
    def voice_service_label(service: VoiceService) -> str:
        if service.service == "hashi_compute":
            return ui_language.tr(
                "voice.service.hashi_advanced"
                if service.supports_style
                else "voice.service.hashi"
            )
        key = {
            "windows": "voice.service.windows",
            "edge": "voice.service.edge",
            "kokoro": "voice.service.kokoro",
            "openrouter": "voice.service.openrouter",
        }.get(service.service)
        return ui_language.tr(key) if key else service.label

    @staticmethod
    def voice_service_availability_label(service: VoiceService) -> str:
        status = str(service.availability or "").strip().casefold()
        if service.available and status in {"ready", "busy", "installed"}:
            return ui_language.tr("voice.availability.ready")
        key = {
            "not installed": "voice.availability.unavailable",
            "no installed voice detected": "voice.availability.no_voice",
            "voice catalogue unavailable": "voice.availability.catalogue_unavailable",
            "requires configured openrouter credential": "voice.availability.credential_missing",
            "credential not configured": "voice.availability.credential_missing",
        }.get(status)
        return ui_language.tr(key) if key else str(service.availability)

    @classmethod
    def voice_service_summary_label(cls, service: VoiceService) -> str:
        label = cls.voice_service_label(service)
        model = str(service.model or "").strip()
        return f"{label} · {model}" if model else label

    def voice_rules_menu_text(self) -> str:
        mode = self.get_reply_mode()
        reply_content_label = ui_language.tr(
            f"voice.reply.{self.get_reply_content()}"
        )
        return setting_card(
            "📋",
            ui_language.tr("voice.rules.title"),
            current=html.escape(ui_language.tr(f"voice.mode.{mode}")),
            facts=[
                f"<b>{html.escape(ui_language.tr('voice.rules.when'))}</b> · "
                f"{html.escape(ui_language.tr(f'voice.trigger.{self.get_reply_trigger()}'))}",
                f"<b>{html.escape(ui_language.tr('voice.rules.how'))}</b> · "
                f"{html.escape(ui_language.tr(f'voice.mode.{mode}'))}",
                f"<b>{html.escape(ui_language.tr('voice.field.reply'))}</b> · "
                f"{html.escape(reply_content_label)} · "
                f"{html.escape(ui_language.tr('voice.reply.scope'))}",
            ],
            action=ui_language.tr("voice.rules.action"),
        )

    def voice_routes_menu_text(self) -> str:
        state = self._load()
        routes = self.get_tts_routes()
        facts: list[str] = []
        if routes:
            for index, route in enumerate(routes):
                role = (
                    ui_language.tr("voice.route.preferred")
                    if index == 0
                    else ui_language.tr("voice.route.fallback", index=index)
                )
                facts.append(
                    f"<b>{html.escape(role)}</b> · "
                    f"{html.escape(self.tts_route_label(route))} · "
                    f"{html.escape(self.tts_route_privacy_label(route))}"
                )
        else:
            facts.append(html.escape(ui_language.tr("voice.route.not_configured")))
        facts.append(
            f"<b>{html.escape(ui_language.tr('voice.external.title'))}</b> · "
            + html.escape(
                ui_language.tr(
                    "voice.external.allowed"
                    if state.get("external_fallback_enabled")
                    else "voice.external.blocked"
                )
            )
        )
        return setting_card(
            "🧭",
            ui_language.tr("voice.routes.title"),
            current=ui_language.tr("voice.routes.current", count=len(routes)),
            facts=facts,
            action=ui_language.tr("voice.routes.action"),
        )

    def voice_route_menu_text(
        self,
        index: int,
        catalogue: VoiceServiceCatalogue,
    ) -> str:
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected >= len(routes):
            raise RuntimeError("Voice route position is invalid.")
        route = routes[selected]
        role = (
            ui_language.tr("voice.route.preferred")
            if selected == 0
            else ui_language.tr("voice.route.fallback", index=selected)
        )
        try:
            service = catalogue.matching_route(route)
            service_label = self.voice_service_label(service)
            privacy = self.voice_service_privacy_label(service)
            speed = (
                str((route.get("options") or {}).get("speed", 1.0))
                if service.supports_speed
                else ui_language.tr("voice.capability.unsupported")
            )
            style = (
                str((route.get("options") or {}).get("style") or ui_language.tr("voice.style.normal"))
                if service.supports_style
                else ui_language.tr("voice.capability.unsupported")
            )
        except RuntimeError:
            service_label = self.tts_route_label(route)
            privacy = self.tts_route_privacy_label(route)
            speed = ui_language.tr("voice.capability.unavailable")
            style = ui_language.tr("voice.capability.unavailable")
        return setting_card(
            "🎛️",
            ui_language.tr("voice.route.title", role=role),
            current=html.escape(service_label),
            facts=[
                f"<b>{html.escape(ui_language.tr('voice.field.tts_voice'))}</b> · "
                f"{html.escape(str(route.get('voice') or '—'))}",
                f"<b>{html.escape(ui_language.tr('voice.field.engine'))}</b> · "
                f"{html.escape(str(route.get('model') or ui_language.tr('voice.engine.built_in')))}",
                f"<b>{html.escape(ui_language.tr('voice.field.privacy'))}</b> · {html.escape(privacy)}",
                f"<b>{html.escape(ui_language.tr('voice.field.speed'))}</b> · {html.escape(speed)}",
                f"<b>{html.escape(ui_language.tr('voice.field.performance'))}</b> · {html.escape(style)}",
            ],
            action=ui_language.tr("voice.route.action"),
        )

    def voice_services_menu_text(
        self,
        index: int,
        catalogue: VoiceServiceCatalogue,
    ) -> str:
        facts = [
            f"<b>{html.escape(self.voice_service_summary_label(service))}</b> · "
            f"{html.escape(self.voice_service_privacy_label(service))} · "
            f"{html.escape(self.voice_service_availability_label(service))}"
            for service in catalogue.services
        ]
        return setting_card(
            "🏭",
            ui_language.tr("voice.services.title"),
            current=ui_language.tr("voice.services.position", position=int(index) + 1),
            facts=facts,
            action=ui_language.tr("voice.services.action"),
        )

    def voice_choices_menu_text(
        self,
        index: int,
        service: VoiceService,
        *,
        page: int,
        pages: int,
    ) -> str:
        return setting_card(
            "🗣️",
            ui_language.tr("voice.choices.title"),
            current=html.escape(self.voice_service_summary_label(service)),
            facts=[
                f"<b>{html.escape(ui_language.tr('voice.field.privacy'))}</b> · "
                f"{html.escape(self.voice_service_privacy_label(service))}",
                ui_language.tr(
                    "voice.choices.page",
                    page=page + 1,
                    pages=max(1, pages),
                    count=len(service.voices),
                ),
                ui_language.tr("voice.choices.catalogue_rule"),
            ],
            action=ui_language.tr("voice.choices.action", position=int(index) + 1),
        )

    def voice_speed_menu_text(self, index: int, service: VoiceService) -> str:
        route = self.get_tts_routes()[int(index)]
        speed = float((route.get("options") or {}).get("speed", 1.0))
        return setting_card(
            "⏱️",
            ui_language.tr("voice.speed.title"),
            current=f"{speed:g}×",
            facts=[html.escape(service.label)],
            action=ui_language.tr("voice.speed.action"),
        )

    def voice_style_menu_text(self, index: int, service: VoiceService) -> str:
        route = self.get_tts_routes()[int(index)]
        style = str((route.get("options") or {}).get("style") or "").strip()
        return setting_card(
            "🎭",
            ui_language.tr("voice.performance.title"),
            current=html.escape(style or ui_language.tr("voice.style.normal")),
            facts=[html.escape(service.label)],
            action=ui_language.tr("voice.performance.action", position=int(index) + 1),
        )

    def voice_profiles_menu_text(self) -> str:
        profile_id = self.get_voice_profile_id()
        current = (
            ui_language.tr(f"voice.profile.{profile_id}")
            if profile_id
            else ui_language.tr("voice.profile.unset")
        )
        return setting_card(
            "🎨",
            ui_language.tr("voice.profiles.title"),
            current=html.escape(current),
            facts=[html.escape(ui_language.tr("voice.profiles.effect"))],
            action=ui_language.tr("voice.profiles.action"),
        )

    def voice_preview_path(
        self,
        profile_id: str,
        renderer: str,
        *,
        locale: str | None = None,
    ) -> Path:
        """Return the stable, shared path for one prerecorded preview."""

        profile = str(profile_id or "").strip().casefold()
        if profile not in self.VOICE_PROFILES:
            raise RuntimeError(f"Unknown voice profile: {profile_id}.")
        output_renderer = str(renderer or "").strip().casefold()
        if output_renderer not in self.VOICE_PREVIEW_RENDERERS:
            raise RuntimeError(f"Unknown voice preview renderer: {renderer}.")
        selected_locale = ui_language.normalize_locale(
            locale or ui_language.current_locale()
        )
        return (
            self.media_dir.parent
            / "_voice_previews"
            / self.VOICE_PREVIEW_VERSION
            / selected_locale
            / profile
            / f"{output_renderer}.ogg"
        )

    def get_voice_preview_assets(
        self,
        profile_id: str,
        *,
        renderers: tuple[str, ...] | None = None,
        locale: str | None = None,
    ) -> tuple[tuple[str, Path], ...]:
        """Resolve readable OGG previews without generating or mutating them."""

        selected = renderers or self.VOICE_PREVIEW_RENDERERS
        selected_locale = ui_language.normalize_locale(
            locale or ui_language.current_locale()
        )
        profile = str(profile_id or "").strip().casefold()
        assets: list[tuple[str, Path]] = []
        bundled: dict[tuple[str, str, str], Path] | None = None
        for renderer in selected:
            output_renderer = str(renderer or "").strip().casefold()
            path = self.voice_preview_path(
                profile,
                output_renderer,
                locale=selected_locale,
            )
            try:
                valid = path.is_file() and path.stat().st_size > 4
                if valid:
                    with path.open("rb") as handle:
                        valid = handle.read(4) == b"OggS"
            except OSError:
                valid = False
            if valid:
                assets.append((output_renderer, path))
                continue
            if bundled is None:
                try:
                    bundled = validate_voice_preview_bundle(
                        root=self.preview_asset_root,
                        version=self.VOICE_PREVIEW_VERSION,
                        require_complete=False,
                    )
                except VoicePreviewBundleError:
                    bundled = {}
            fallback = bundled.get((selected_locale, profile, output_renderer))
            if fallback is not None:
                assets.append((output_renderer, fallback))
        return tuple(assets)

    def get_tts_voice_preview_assets(
        self,
        alias: str,
        *,
        telegram: bool,
    ) -> tuple[tuple[str, Path], ...]:
        """Resolve the exact prerecorded sample for one concrete TTS voice."""

        key = str(alias or "").strip().casefold()
        preset = self.VOICE_PRESETS.get(key)
        if not preset:
            return ()
        return resolve_tts_voice_preview_assets(
            key,
            preset,
            telegram=telegram,
            root=self.tts_preview_asset_root,
        )

    def tts_voice_preview_caption(
        self,
        alias: str,
        *,
        locale: str | None = None,
    ) -> str:
        return ui_language.tr(
            "voice.tts.preview.caption",
            locale=locale,
            voice=self.voice_preset_menu_label(alias, locale=locale),
        )

    def _normalise_state(self, data: dict) -> dict:
        merged = copy.deepcopy(self.DEFAULT_STATE)
        merged.update(data)
        if not isinstance(merged.get("provider_options"), dict):
            merged["provider_options"] = {}
        voice_profile = str(merged.get("voice_profile") or "").strip().casefold()
        merged["voice_profile"] = (
            voice_profile if voice_profile in self.VOICE_PROFILES else None
        )
        raw_native = data.get("native")
        native = copy.deepcopy(self.DEFAULT_STATE["native"])
        if isinstance(raw_native, dict):
            native.update(raw_native)
        legacy_reply_content = (
            raw_native.get("reply_content")
            if isinstance(raw_native, dict)
            else None
        )
        reply_content = self._normalise_reply_content(
            data.get("reply_content", legacy_reply_content)
        )
        merged["reply_content"] = reply_content
        native["reply_content"] = reply_content
        merged["native"] = self._normalise_native_policy(native)
        routes = data.get("tts_routes")
        if isinstance(routes, list):
            merged["tts_routes"] = [
                self._normalise_tts_route(route) for route in routes
            ]
        elif data:
            provider = str(data.get("provider") or _default_tts_provider()).strip().casefold()
            # Old presets may contain executable/cache paths.  Those are
            # re-derived from the qualified local preset at synthesis time;
            # only portable voice controls belong in the durable route.
            legacy_options = self._durable_route_options(
                data.get("provider_options") or {}
            )
            merged["tts_routes"] = [
                self._normalise_tts_route(
                    {
                        "service": provider,
                        "voice": data.get("voice_name"),
                        "options": legacy_options,
                    }
                )
            ]
        else:
            merged["tts_routes"] = []
        merged["schema_version"] = 2
        merged["external_fallback_enabled"] = bool(
            data.get("external_fallback_enabled", False)
        )
        reply_trigger = str(
            data.get("reply_trigger") or self.DEFAULT_STATE["reply_trigger"]
        ).strip().casefold()
        merged["reply_trigger"] = (
            reply_trigger
            if reply_trigger in {"all", "voice_message"}
            else self.DEFAULT_STATE["reply_trigger"]
        )
        return merged

    @staticmethod
    def _durable_route_options(raw: object) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            return {}
        allowed = {"speed", "style", "rate_percent", "lang_code"}
        return {
            str(key).strip().casefold(): value
            for key, value in raw.items()
            if str(key).strip().casefold() in allowed
        }

    @staticmethod
    def _normalise_tts_route(raw: object) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise RuntimeError("Voice route must be an object.")
        service = str(raw.get("service") or "").strip().casefold()
        if service not in {
            "edge",
            "windows",
            "macos",
            "piper",
            "kokoro",
            "coqui",
            "hashi_compute",
            "openrouter",
        }:
            raise RuntimeError(f"Unknown voice service: {service or 'empty'}.")

        def optional(key: str, maximum: int = 200) -> str | None:
            value = str(raw.get(key) or "").strip()
            if len(value) > maximum or any(ord(character) < 32 for character in value):
                raise RuntimeError(f"Voice route {key} is invalid.")
            return value or None

        options = raw.get("options") or {}
        if not isinstance(options, Mapping) or len(options) > 16:
            raise RuntimeError("Voice route options are invalid.")
        normalized_options: dict[str, Any] = {}
        for key, value in options.items():
            name = str(key or "").strip().casefold()
            if name not in {"speed", "style", "rate_percent", "lang_code"}:
                raise RuntimeError(f"Unsupported voice route option: {name or 'empty'}.")
            if name == "speed":
                if type(value) not in {int, float} or not 0.5 <= float(value) <= 2.0:
                    raise RuntimeError("Voice speed must be between 0.5 and 2.0.")
                value = float(value)
            elif name == "rate_percent":
                if type(value) is not int or not -100 <= value <= 100:
                    raise RuntimeError("Voice rate percent must be between -100 and 100.")
            else:
                value = str(value or "").strip()
                maximum = 500 if name == "style" else 8
                if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
                    raise RuntimeError(f"Voice route {name} is invalid.")
            normalized_options[name] = value
        node_id = optional("node_id", 100)
        offering_id = optional("offering_id", 100)
        model = optional("model", 200)
        voice = optional("voice", 160)
        if service == "hashi_compute" and not all((node_id, offering_id, model, voice)):
            raise RuntimeError(
                "Hashi Compute routes require node, offering, model, and voice."
            )
        if service == "openrouter" and not all((model, voice)):
            raise RuntimeError("OpenRouter routes require model and voice.")
        return {
            "service": service,
            "node_id": node_id,
            "offering_id": offering_id,
            "model": model,
            "voice": voice,
            "options": normalized_options,
        }

    def tts_route_privacy(self, route: Mapping[str, Any]) -> tuple[str, str]:
        service = str(route.get("service") or "").strip().casefold()
        if service == "hashi_compute":
            node_id = str(route.get("node_id") or "").strip()
            try:
                _document, nodes = ComputeServicesConfig(
                    self.project_root / "compute_services.json"
                ).read()
            except ComputeServiceError:
                return "unknown", "unknown"
            node = next((item for item in nodes if item.id == node_id), None)
            if node is None:
                return "unknown", "unknown"
            if node.location in {"local", "lan"}:
                return "local_private", node.location
            return "external", "network"
        if service in {"windows", "macos", "piper", "kokoro", "coqui"}:
            return "local_private", "device"
        return "external", "network"

    def tts_route_privacy_label(self, route: Mapping[str, Any]) -> str:
        level, location = self.tts_route_privacy(route)
        if level == "local_private":
            return ui_language.tr(
                "voice.privacy.local_lan"
                if location == "lan"
                else "voice.privacy.local_device"
            )
        if level == "external":
            return ui_language.tr("voice.privacy.external")
        return ui_language.tr("voice.privacy.unknown")

    @staticmethod
    def tts_route_label(route: Mapping[str, Any]) -> str:
        service = str(route.get("service") or "").strip().casefold()
        advanced = (
            str(route.get("offering_id") or "").strip().casefold()
            == "advanced_tts"
            or "minicpm" in str(route.get("model") or "").casefold()
        )
        labels = {
            "hashi_compute": ui_language.tr(
                "voice.service.hashi_advanced"
                if advanced
                else "voice.service.hashi"
            ),
            "windows": ui_language.tr("voice.service.windows"),
            "macos": "macOS TTS",
            "edge": ui_language.tr("voice.service.edge"),
            "openrouter": ui_language.tr("voice.service.openrouter"),
            "piper": "Piper TTS",
            "kokoro": ui_language.tr("voice.service.kokoro"),
            "coqui": "Coqui TTS",
        }
        label = labels.get(service, service or "TTS")
        model = str(route.get("model") or "").strip()
        if model:
            label = f"{label} / {model}"
        voice = str(route.get("voice") or "").strip()
        return f"{label} · {voice}" if voice else label

    def get_tts_routes(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._load().get("tts_routes") or [])

    def replace_tts_routes(self, routes: object) -> str:
        if not isinstance(routes, list) or not 1 <= len(routes) <= 8:
            raise RuntimeError("Voice routes must contain between 1 and 8 entries.")
        normalized = [self._normalise_tts_route(route) for route in routes]

        def mutate(state: dict) -> None:
            state["schema_version"] = 2
            state["tts_routes"] = copy.deepcopy(normalized)
            primary = normalized[0]
            state["provider"] = self._provider_for_tts_route(primary)
            state["voice_name"] = primary.get("voice")
            state["provider_options"] = dict(primary.get("options") or {})

        self._update(mutate)
        return "Voice route updated."

    def set_external_fallback_enabled(self, enabled: bool) -> str:
        self._update(
            lambda state: state.__setitem__(
                "external_fallback_enabled", bool(enabled)
            )
        )
        return ui_language.tr(
            "voice.notice.external_allowed"
            if enabled
            else "voice.notice.external_blocked"
        )

    def get_reply_trigger(self) -> str:
        value = str(self._load().get("reply_trigger") or "all").strip().casefold()
        return value if value in {"all", "voice_message"} else "all"

    @staticmethod
    def _normalise_reply_content(value: object) -> str:
        aliases = {
            "both": "audio_and_text",
            "audio": "audio_only",
            "text": "text_only",
        }
        normalized = str(value or "audio_and_text").strip().casefold()
        normalized = aliases.get(normalized, normalized)
        return (
            normalized
            if normalized in {"audio_and_text", "audio_only", "text_only"}
            else "audio_and_text"
        )

    def get_reply_content(self) -> str:
        """Return this Agent's default reply shape for every voice path."""

        return self._normalise_reply_content(self._load().get("reply_content"))

    def set_reply_content(self, content: str) -> str:
        normalized = self._normalise_reply_content(content)
        raw = str(content or "").strip().casefold()
        if raw not in {
            "both",
            "audio",
            "text",
            "audio_and_text",
            "audio_only",
            "text_only",
        }:
            raise RuntimeError("Reply content must be both, audio, or text.")

        def mutate(state: dict) -> None:
            state["reply_content"] = normalized
            native = self._native_policy_from_state(state)
            native["reply_content"] = normalized
            state["native"] = native

        self._update(mutate)
        return f"Reply content set to {normalized}."

    def set_reply_trigger(self, trigger: str) -> str:
        aliases = {"voice": "voice_message", "messages": "all"}
        normalized = aliases.get(
            str(trigger or "").strip().casefold(),
            str(trigger or "").strip().casefold(),
        )
        if normalized not in {"all", "voice_message"}:
            raise RuntimeError("Voice reply trigger must be all or voice_message.")
        self._update(lambda state: state.__setitem__("reply_trigger", normalized))
        return ui_language.tr(f"voice.notice.trigger_{normalized}")

    def should_synthesize_reply(self, *, voice_origin: bool = False) -> bool:
        """Return whether an ordinary text-model reply should receive TTS."""

        if self.get_reply_mode() != "tts" or self.get_reply_content() == "text_only":
            return False
        trigger = self.get_reply_trigger()
        return trigger == "all" or bool(voice_origin)

    def _catalogue_voice_choices(self) -> dict[str, tuple[VoiceChoice, ...]]:
        choices: dict[str, list[VoiceChoice]] = {
            "edge": [],
            "windows": [],
            "kokoro": [],
        }
        for _alias, preset, availability in self.get_voice_presets():
            provider = str(preset.get("provider") or "").strip().casefold()
            if provider not in choices:
                continue
            if provider in {"windows", "kokoro"} and availability != "ready":
                continue
            choices[provider].append(
                VoiceChoice(
                    id=str(preset.get("voice_name") or ""),
                    label=str(preset.get("label") or preset.get("voice_name") or ""),
                    language=str(preset.get("locale") or "") or None,
                    gender=str(preset.get("gender") or "") or None,
                    options=self._durable_route_options(
                        preset.get("provider_options") or {}
                    ),
                )
            )
        return {key: tuple(value) for key, value in choices.items()}

    async def get_voice_service_catalogue(self, *, client: Any = None) -> VoiceServiceCatalogue:
        catalogue = await discover_voice_services(
            self.project_root,
            client=client,
            secrets=self._secrets,
            built_in_voices=self._catalogue_voice_choices(),
        )
        services = []
        for service in catalogue.services:
            if service.id == "edge":
                service = replace(
                    service,
                    available=self._provider_status("edge") == "installed",
                    availability=self._provider_status("edge"),
                )
            elif service.id == "device-kokoro":
                service = replace(
                    service,
                    available=self._provider_status("kokoro") == "installed",
                    availability=self._provider_status("kokoro"),
                )
            elif service.id == "openrouter-gemini":
                configured = any(
                    bool(self._secrets.get(key))
                    for key in (
                        "openrouter_key",
                        "openrouter-api_key",
                        "openrouter_api_key",
                    )
                )
                service = replace(
                    service,
                    available=configured,
                    availability=("ready" if configured else "credential not configured"),
                )
            services.append(service)
        return VoiceServiceCatalogue(tuple(services), catalogue.errors)

    @staticmethod
    def _preferred_service_voice(service: VoiceService) -> VoiceChoice:
        preferred_ids = (
            "zf_001",
            "Microsoft Huihui Desktop",
            "zh-CN-XiaoxiaoNeural",
        )
        by_id = {voice.id: voice for voice in service.voices}
        for voice_id in preferred_ids:
            if voice_id in by_id:
                return by_id[voice_id]
        selected = next((voice for voice in service.voices if voice.callable), None)
        if selected is None and service.voices:
            selected = service.voices[0]
        if selected is None:
            raise RuntimeError(f"Voice service has no catalogue voices: {service.label}.")
        return selected

    async def apply_recommended_local_routes(
        self,
        *,
        advanced: bool = False,
        client: Any = None,
    ) -> str:
        """Install a privacy-preserving route chain from live service metadata."""

        catalogue = await self.get_voice_service_catalogue(client=client)
        local_compute = [
            service
            for service in catalogue.services
            if service.service == "hashi_compute"
            and service.privacy == "local_private"
            and service.available
            and service.voices
        ]
        ordinary = next(
            (service for service in local_compute if not service.supports_style),
            None,
        )
        expressive = next(
            (service for service in local_compute if service.supports_style),
            None,
        )
        windows = next(
            (
                service
                for service in catalogue.services
                if service.service == "windows"
                and service.available
                and service.voices
            ),
            None,
        )
        selected_services: list[VoiceService] = []
        if advanced:
            if expressive is None:
                raise RuntimeError("Hashi Compute advanced TTS is unavailable.")
            selected_services.append(expressive)
        if ordinary is None:
            raise RuntimeError("Hashi Compute TTS is unavailable.")
        selected_services.append(ordinary)
        if windows is not None:
            selected_services.append(windows)
        if not selected_services:
            raise RuntimeError("No local private voice service is currently available.")
        routes = []
        for service in selected_services:
            options: dict[str, Any] = {}
            if service.supports_speed:
                options["speed"] = 1.0
            routes.append(
                service.route(
                    self._preferred_service_voice(service).id,
                    options=options,
                )
            )
        self.replace_tts_routes(routes)
        return ui_language.tr(
            "voice.notice.recommended_advanced"
            if advanced
            else "voice.notice.recommended_local"
        )

    async def set_tts_route_service(
        self,
        index: int,
        service_id: str,
        *,
        client: Any = None,
    ) -> str:
        catalogue = await self.get_voice_service_catalogue(client=client)
        service = catalogue.service(service_id)
        if not service.available:
            raise RuntimeError(f"Voice service is unavailable: {service.availability}.")
        voice = self._preferred_service_voice(service)
        options: dict[str, Any] = {}
        if service.supports_speed:
            options["speed"] = 1.0
        route = service.route(voice.id, options=options)
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected > len(routes) or selected >= 8:
            raise RuntimeError("Voice route position is invalid.")
        if selected == len(routes):
            routes.append(route)
        else:
            routes[selected] = route
        self.replace_tts_routes(routes)
        return f"Voice route {selected + 1} set to {service.label}."

    async def set_catalogue_tts_route_voice(
        self,
        index: int,
        service_id: str,
        voice_id: str,
        *,
        client: Any = None,
        catalogue: VoiceServiceCatalogue | None = None,
    ) -> str:
        """Persist an exact catalogue voice without treating preview metadata as a lock."""

        live = catalogue or await self.get_voice_service_catalogue(client=client)
        service = live.service(service_id)
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected >= len(routes):
            raise RuntimeError("Voice route position is invalid.")
        configured_service = live.matching_route(routes[selected])
        if configured_service.id != service.id:
            raise RuntimeError("Voice service changed; reopen this route and try again.")
        voice = next((item for item in service.voices if item.id == voice_id), None)
        if voice is None:
            raise RuntimeError("Voice is no longer present in the service catalogue.")
        # `callable` is catalogue status metadata, not a second permission gate.
        voice_option_keys = {
            str(key).strip().casefold()
            for item in service.voices
            for key in item.options
        }
        options = dict(routes[selected].get("options") or {})
        for key in voice_option_keys:
            options.pop(key, None)
        routes[selected]["voice"] = voice.id
        routes[selected]["options"] = {**dict(voice.options), **options}
        self.replace_tts_routes(routes)
        return f"Voice route {selected + 1} voice updated."

    async def download_tts_route_preview(
        self,
        index: int,
        *,
        locale: str,
        catalogue: VoiceServiceCatalogue | None = None,
        client: Any = None,
    ) -> Any | None:
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected >= len(routes):
            raise RuntimeError("Voice route position is invalid.")
        live = catalogue or await self.get_voice_service_catalogue(client=client)
        service = live.matching_route(routes[selected])
        if service.compute_directory is None:
            return None
        return await service.compute_directory.download_preview(
            str(routes[selected].get("voice") or ""),
            locale,
        )

    def set_tts_route_voice(self, index: int, voice_id: str) -> str:
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected >= len(routes):
            raise RuntimeError("Voice route position is invalid.")
        voice = str(voice_id or "").strip()
        if not voice or len(voice) > 160 or any(ord(char) < 32 for char in voice):
            raise RuntimeError("Voice identifier is invalid.")
        routes[selected]["voice"] = voice
        self.replace_tts_routes(routes)
        return f"Voice route {selected + 1} voice updated."

    def set_tts_route_speed(self, index: int, speed: float) -> str:
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected >= len(routes):
            raise RuntimeError("Voice route position is invalid.")
        value = float(speed)
        if not 0.5 <= value <= 2.0:
            raise RuntimeError("Voice speed must be between 0.5 and 2.0.")
        options = routes[selected].setdefault("options", {})
        options["speed"] = value
        # The new service-neutral speed is authoritative.  Keeping the old
        # Edge-only percentage beside it would silently override the menu.
        options.pop("rate_percent", None)
        self.replace_tts_routes(routes)
        return f"Voice route {selected + 1} speed set to {value:g}."

    def set_tts_route_style(self, index: int, style: str | None) -> str:
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected >= len(routes):
            raise RuntimeError("Voice route position is invalid.")
        value = str(style or "").strip()
        if len(value) > 500 or any(ord(char) < 32 for char in value):
            raise RuntimeError("Performance style is invalid.")
        options = routes[selected].setdefault("options", {})
        if value:
            options["style"] = value
        else:
            options.pop("style", None)
        self.replace_tts_routes(routes)
        return f"Voice route {selected + 1} performance style updated."

    def move_tts_route(self, index: int, direction: int) -> str:
        routes = self.get_tts_routes()
        selected = int(index)
        target = selected + (1 if int(direction) > 0 else -1)
        if selected < 0 or selected >= len(routes) or target < 0 or target >= len(routes):
            raise RuntimeError("Voice route cannot move in that direction.")
        routes[selected], routes[target] = routes[target], routes[selected]
        self.replace_tts_routes(routes)
        return "Voice fallback order updated."

    def delete_tts_route(self, index: int) -> str:
        routes = self.get_tts_routes()
        selected = int(index)
        if selected < 0 or selected >= len(routes):
            raise RuntimeError("Voice route position is invalid.")
        routes.pop(selected)
        if not routes:
            raise RuntimeError("At least one TTS route is required.")
        self.replace_tts_routes(routes)
        return "Voice fallback removed."

    @staticmethod
    def _provider_for_tts_route(route: Mapping[str, Any]) -> str:
        service = str(route.get("service") or "").strip().casefold()
        if service == "hashi_compute":
            return "hashi_compute"
        if service == "openrouter":
            return "openai_compatible"
        return service

    def _load(self) -> dict:
        try:
            return self._normalise_state(dict(read_config_json(self.state_path)))
        except FileNotFoundError:
            return copy.deepcopy(self.DEFAULT_STATE)
        except (OSError, ValueError, TypeError):
            # Reads remain backwards compatible and fail to safe defaults.
            return copy.deepcopy(self.DEFAULT_STATE)

    def _update(self, mutate: Callable[[dict], None], *, retries: int = 4) -> dict:
        """Fresh-read mutation that never overwrites corrupt or newer state."""

        del retries  # retained for private-call compatibility; stale actions are not replayed
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            document = read_config_json(self.state_path)
            revision: str | None = document.revision
            state = self._normalise_state(dict(document))
        except FileNotFoundError:
            revision = None
            state = copy.deepcopy(self.DEFAULT_STATE)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(
                f"Voice state is unreadable; no settings were changed: {exc}"
            ) from exc
        mutate(state)
        try:
            write_config_json(
                self.state_path,
                state,
                expected_revision=revision,
            )
        except ConfigConflictError as exc:
            raise RuntimeError(
                "Voice state changed since it was opened; reopen the setting and try again"
            ) from exc
        return state

    def get_state(self) -> dict:
        return self._load()

    @staticmethod
    def _retention_label(value: object) -> str:
        if str(value).strip().casefold() in {"indefinite", "forever"}:
            return "indefinite"
        seconds = max(60, int(value or 3600))
        return f"{seconds // 60} min"

    @staticmethod
    def _native_target_label(policy: dict) -> str:
        provider = str(policy.get("provider") or "configured route")
        model = str(policy.get("model") or "configured model")
        return f"{provider}/{model}"

    @classmethod
    def _normalise_native_policy(cls, raw: dict) -> dict:
        policy = copy.deepcopy(cls.DEFAULT_STATE["native"])
        policy.update(raw if isinstance(raw, dict) else {})
        mode = str(policy.get("mode") or "off").strip().casefold()
        if mode == "auto":
            mode = "native"
        policy["mode"] = mode if mode in {"off", "tts", "native"} else "off"
        trigger = str(policy.get("reply_trigger") or "voice_message").strip().casefold()
        policy["reply_trigger"] = trigger if trigger in {"voice_message", "all"} else "voice_message"
        content = str(policy.get("reply_content") or "audio_and_text").strip().casefold()
        policy["reply_content"] = content if content in {"audio_and_text", "audio_only", "text_only"} else "audio_and_text"
        fallback = str(policy.get("fallback") or "local_chain").strip().casefold()
        policy["fallback"] = fallback if fallback in {"local_chain", "native_only"} else "local_chain"
        retention = policy.get("retention_seconds", 3600)
        if str(retention).strip().casefold() in {"indefinite", "forever"}:
            policy["retention_seconds"] = "indefinite"
        else:
            policy["retention_seconds"] = max(60, int(retention or 3600))
        for key in ("provider", "model", "voice", "format"):
            value = str(policy.get(key) or "").strip()
            policy[key] = value or None
        policy["input_transcript_echo"] = bool(policy.get("input_transcript_echo"))
        policy["output_transcript_echo"] = bool(policy.get("output_transcript_echo", True))
        # The contract reserves this field for a later activation boundary.
        # Phase 1 always forces the audio model itself to be tool-free.
        policy["audio_model_tools"] = False
        if not isinstance(policy.get("terminal_overrides"), dict):
            policy["terminal_overrides"] = {}
        return policy

    @classmethod
    def _native_policy_from_state(cls, state: dict) -> dict:
        return cls._normalise_native_policy(
            state.get("native") if isinstance(state.get("native"), dict) else {}
        )

    @property
    def native_policy(self) -> dict:
        return self._native_policy_from_state(self._load())

    def native_policy_for_terminal(self, terminal: str | None = None) -> dict:
        """Resolve the Agent policy with an optional configured terminal layer.

        Terminal overrides live in the Agent-owned voice state.  A request may
        choose a presentation preference later, but cannot use this mechanism
        to invent a provider/model or enable native voice when the Agent-level
        policy is off.
        """

        policy = self.native_policy
        terminal_key = str(terminal or "").strip().casefold()
        overrides = policy.get("terminal_overrides")
        override = (
            overrides.get(terminal_key)
            if terminal_key and isinstance(overrides, dict)
            else None
        )
        if not isinstance(override, dict):
            return policy
        merged = dict(policy)
        merged.update(override)
        merged["terminal_overrides"] = dict(overrides)
        resolved = self._normalise_native_policy(merged)
        if policy["mode"] != "native":
            resolved["mode"] = policy["mode"]
        return resolved

    def native_audio_enabled(self, terminal: str | None = None) -> bool:
        return self.native_policy_for_terminal(terminal)["mode"] == "native"

    def set_native_terminal_override(
        self,
        terminal: str,
        override: dict[str, object] | None,
    ) -> dict:
        """Persist one presentation-only terminal override safely."""

        terminal_key = str(terminal or "").strip().casefold()
        if not terminal_key:
            raise RuntimeError("Terminal name is required.")
        raw = dict(override or {})
        allowed = {"mode", "reply_content", "retention_seconds"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise RuntimeError(
                "Unsupported native terminal override: " + ", ".join(unknown)
            )

        normalized: dict[str, object] = {}
        if "mode" in raw:
            mode = str(raw["mode"] or "").strip().casefold()
            mode = "native" if mode == "auto" else mode
            if mode not in {"off", "tts", "native"}:
                raise RuntimeError("Terminal mode must be off, tts, or native.")
            normalized["mode"] = mode
        if "reply_content" in raw:
            aliases = {
                "both": "audio_and_text",
                "audio": "audio_only",
                "text": "text_only",
            }
            content = str(raw["reply_content"] or "").strip().casefold()
            content = aliases.get(content, content)
            if content not in {"audio_and_text", "audio_only", "text_only"}:
                raise RuntimeError("Reply content must be both, audio, or text.")
            normalized["reply_content"] = content
        if "retention_seconds" in raw:
            retention_raw = str(raw["retention_seconds"] or "").strip().casefold()
            if retention_raw in {"indefinite", "forever"}:
                retention: int | str = "indefinite"
            else:
                try:
                    retention = int(retention_raw)
                except ValueError as exc:
                    raise RuntimeError(
                        "Terminal retention must be whole seconds or indefinite."
                    ) from exc
                if retention < 60:
                    raise RuntimeError(
                        "Terminal retention must be at least 60 seconds."
                    )
            normalized["retention_seconds"] = retention

        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            overrides = dict(native.get("terminal_overrides") or {})
            if normalized:
                overrides[terminal_key] = normalized
            else:
                overrides.pop(terminal_key, None)
            native["terminal_overrides"] = overrides
            state["native"] = native

        self._update(mutate)
        return self.native_policy_for_terminal(terminal_key)

    def set_native_mode(self, mode: str) -> str:
        return self.set_reply_mode(mode)

    def set_native_target(self, provider: str | None, model: str | None) -> str:
        selected_provider = str(provider or "").strip() or None
        selected_model = str(model or "").strip() or None
        if bool(selected_provider) != bool(selected_model):
            raise RuntimeError("Native provider and model must be configured together.")
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            native["provider"] = selected_provider
            native["model"] = selected_model
            profile_id = str(state.get("voice_profile") or "").strip().casefold()
            if profile_id:
                native["voice"] = self._native_voice_for_profile(profile_id, native)
            state["native"] = native

        state = self._update(mutate)
        native = self._native_policy_from_state(state)
        return f"Native target set to {self._native_target_label(native)}."

    def set_native_voice(self, voice: str | None) -> str:
        selected_voice = str(voice or "").strip() or None
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            native["voice"] = selected_voice
            state["native"] = native
            state["voice_profile"] = None

        state = self._update(mutate)
        native = self._native_policy_from_state(state)
        return f"Native voice set to {native['voice'] or 'configured default'}."

    def set_native_format(self, audio_format: str | None) -> str:
        selected_format = str(audio_format or "").strip().casefold() or None
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            native["format"] = selected_format
            state["native"] = native

        state = self._update(mutate)
        native = self._native_policy_from_state(state)
        return f"Native format set to {native['format'] or 'capability-selected default'}."

    def set_native_reply_content(self, content: str) -> str:
        """Compatibility alias for the Agent-wide voice reply rule."""

        return self.set_reply_content(content)

    def set_native_fallback(self, fallback: str) -> str:
        aliases = {"local": "local_chain", "none": "native_only"}
        normalized = aliases.get(str(fallback or "").strip().casefold(), str(fallback or "").strip().casefold())
        if normalized not in {"local_chain", "native_only"}:
            raise RuntimeError("Fallback must be local_chain or native_only.")
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            native["fallback"] = normalized
            state["native"] = native

        self._update(mutate)
        return f"Native fallback set to {normalized}."

    def set_native_retention(self, value: str | int) -> str:
        raw = str(value or "").strip().casefold()
        retention: int | str
        if raw in {"indefinite", "forever"}:
            retention = "indefinite"
        else:
            try:
                minutes = int(raw)
            except ValueError as exc:
                raise RuntimeError("Retention must be whole minutes or indefinite.") from exc
            if minutes < 1:
                raise RuntimeError("Retention must be at least one minute.")
            retention = minutes * 60
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            native["retention_seconds"] = retention
            state["native"] = native

        self._update(mutate)
        return f"Native audio retention set to {self._retention_label(retention)}."

    def set_output_transcript_echo(self, enabled: bool) -> str:
        def mutate(state: dict) -> None:
            native = self._native_policy_from_state(state)
            native["output_transcript_echo"] = bool(enabled)
            native["reply_content"] = (
                "audio_and_text" if enabled else "audio_only"
            )
            state["native"] = native

        self._update(mutate)
        return f"Native output transcript echo is now {'ON' if enabled else 'OFF'}."

    def is_enabled(self) -> bool:
        return bool(self._load().get("enabled"))

    def get_provider_name(self) -> str:
        return str(self._load().get("provider") or "windows")

    def list_providers(self) -> list[str]:
        return list_provider_names()

    def describe(self) -> str:
        state = self._load()
        native = self._native_policy_from_state(state)
        status = self.get_reply_mode().upper()
        voice_name = state.get("voice_name") or "default"
        preset = self.get_active_preset_alias()
        preset_line = f"Preset: {preset}\n" if preset else ""
        voice_profile = self._voice_profile_from_state(state)
        profile_line = f"Voice Profile: {voice_profile}\n" if voice_profile else ""
        return (
            f"Voice Mode: {status}\n"
            f"Mode: {state.get('mode', 'text_and_voice')}\n"
            f"Provider: {state.get('provider', 'windows')}\n"
            f"{profile_line}"
            f"{preset_line}"
            f"Voice Name: {voice_name}\n"
            f"Rate: {state.get('rate', 0)}\n"
            f"Max Chars: {state.get('max_chars', 1200)}\n"
            f"Native Mode: {native['mode']}\n"
            f"Native Target: {self._native_target_label(native)}\n"
            f"Native Reply: {native['reply_content']}\n"
            f"Native Fallback: {native['fallback']}\n"
            f"Native Retention: {self._retention_label(native['retention_seconds'])}\n"
            "Audio-model Tools: OFF (PoC)"
        )

    def set_enabled(self, enabled: bool) -> str:
        self._update(lambda state: state.__setitem__("enabled", bool(enabled)))
        return f"Voice replies are now {'ON' if enabled else 'OFF'}."

    def set_provider(self, provider_name: str) -> str:
        name = (provider_name or "").strip().lower()
        if name not in self.list_providers():
            raise RuntimeError(f"Unknown voice provider: {provider_name}. Available: {', '.join(self.list_providers())}")
        def mutate(state: dict) -> None:
            state["provider"] = name

        self._update(mutate)
        return f"Voice provider set to {name}."

    def set_voice_name(self, voice_name: str) -> str:
        preset = self._preset_payload(voice_name)
        if preset:
            return self.apply_voice_preset(voice_name)
        selected_name = voice_name.strip() or None
        def mutate(state: dict) -> None:
            state["voice_name"] = selected_name

        self._update(mutate)
        return f"Voice name set to {selected_name or 'default'}."

    def set_rate(self, rate: int) -> str:
        self._update(lambda state: state.__setitem__("rate", int(rate)))
        return f"Voice rate set to {int(rate)}."

    def provider_hints(self) -> str:
        return (
            "Providers\n"
            f"- windows: built-in Windows voice fallback [{self._provider_status('windows')}]\n"
            f"- edge: Microsoft Edge online neural voices [{self._provider_status('edge')}]\n"
            f"- piper: local Piper CLI/model [{self._provider_status('piper')}]\n"
            f"- kokoro: Kokoro Python package [{self._provider_status('kokoro')}]\n"
            f"- coqui: Coqui TTS Python package [{self._provider_status('coqui')}]\n"
        )

    def apply_voice_preset(self, alias: str) -> str:
        preset = self._preset_payload(alias)
        if not preset:
            raise RuntimeError(f"Unknown voice preset: {alias}. Use /voice voices.")
        if preset["provider"] == "piper" and not Path(preset["voice_name"]).exists():
            raise RuntimeError(f"Voice preset {alias} is not ready on disk: {preset['voice_name']}")
        def mutate(state: dict) -> None:
            state["provider"] = preset["provider"]
            state["voice_name"] = preset["voice_name"]
            state["provider_options"] = dict(preset.get("provider_options") or {})
            route = self._normalise_tts_route(
                {
                    "service": preset["provider"],
                    "voice": preset["voice_name"],
                    "options": self._durable_route_options(
                        preset.get("provider_options") or {}
                    ),
                }
            )
            routes = list(state.get("tts_routes") or [])
            if routes:
                routes[0] = route
            else:
                routes.append(route)
            state["schema_version"] = 2
            state["tts_routes"] = routes

        self._update(mutate)
        return f"Voice preset set to {alias}: {preset['label']}."

    def _write_voice_audit(self, record: dict, logger: logging.Logger) -> None:
        payload = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **record,
        }
        try:
            self.voice_audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            with self._voice_audit_lock:
                with self.voice_audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
        except OSError as exc:
            logger.warning(
                "Voice synthesis audit could not be written: %s",
                safe_voice_error_detail(exc),
            )

    @staticmethod
    def _notice_language_name(language: str | None, *, chinese: bool) -> str:
        if chinese:
            return {"en": "英语", "zh": "中文", "ja": "日语"}.get(language, "当前语言")
        return {"en": "English", "zh": "Chinese", "ja": "Japanese"}.get(
            language, "the detected language"
        )

    def _runtime_voice_label(
        self,
        provider: str,
        voice_name: str | None,
        *,
        locale: str | None,
    ) -> str:
        folded_voice = str(voice_name or "").strip().casefold()
        for alias, preset in self.VOICE_PRESETS.items():
            if (
                str(preset.get("provider") or "").strip().casefold() == provider
                and str(preset.get("voice_name") or "").strip().casefold() == folded_voice
            ):
                return self.voice_preset_menu_label(alias, locale=locale)
        if voice_name:
            return str(voice_name)
        return _provider_label(provider)

    def _routing_notice(
        self,
        *,
        primary_route,
        final_route,
        final_voice_name: str | None,
        fallback_from: str | None,
        fallback_reason: str | None,
        locale: str | None,
    ) -> str | None:
        chinese = str(locale or "").strip().casefold().startswith("zh")
        notices: list[str] = []
        if final_route.adapted_from_voice:
            old_voice = self._runtime_voice_label(
                final_route.provider,
                final_route.adapted_from_voice,
                locale=locale,
            )
            new_voice = self._runtime_voice_label(
                final_route.provider,
                final_voice_name or final_route.voice_name,
                locale=locale,
            )
            language = self._notice_language_name(
                final_route.language.language,
                chinese=chinese,
            )
            if chinese:
                notices.append(
                    f"已为{language}内容临时从“{old_voice}”切换为“{new_voice}”；"
                    "您的默认语音未改变。"
                )
            else:
                notices.append(
                    f"For {language} text, HASHI temporarily switched from "
                    f"“{old_voice}” to “{new_voice}”; your default voice was not changed."
                )
        if fallback_from:
            primary = _provider_label(fallback_from)
            fallback = _provider_label(final_route.provider)
            reason = safe_voice_error_detail(fallback_reason or "")[:240]
            if chinese:
                notices.append(
                    f"{primary} 暂时不可用，已降级为 {fallback}；"
                    f"当前音色可能与默认设置不同。原因：{reason}"
                )
            else:
                notices.append(
                    f"{primary} was unavailable, so HASHI downgraded to {fallback}. "
                    f"The voice may differ from your default. Reason: {reason}"
                )
        return " ".join(notices) or None

    async def synthesize_reply(
        self,
        agent_name: str,
        request_id: str,
        text: str,
        max_retries: int = 2,
        force: bool = False,
        max_chars_override: int | None = None,
        source_language: str | None = None,
        source_surface: str = "unknown",
    ) -> VoiceAsset | None:
        import asyncio

        logger = logging.getLogger(f"Runtime.{agent_name}.voice")
        state = self._load()
        if not force and not state.get("enabled"):
            return None

        configured_specs = [
            self._normalise_tts_route(item)
            for item in (state.get("tts_routes") or [])
        ]
        if configured_specs:
            primary_spec = configured_specs[0]
            fallback_specs = configured_specs[1:]
            primary_name = self._provider_for_tts_route(primary_spec)
            primary_voice = primary_spec.get("voice")
            primary_options = dict(primary_spec.get("options") or {})
        else:
            primary_name = str(
                state.get("provider") or _default_tts_provider()
            ).strip().casefold()
            primary_spec = {
                "service": primary_name,
                "node_id": None,
                "offering_id": None,
                "model": None,
                "voice": state.get("voice_name"),
                "options": dict(state.get("provider_options") or {}),
            }
            primary_voice = state.get("voice_name")
            primary_options = dict(state.get("provider_options") or {})
            available_providers = set(self.list_providers())
            fallback_specs = [
                {
                    "service": name,
                    "node_id": None,
                    "offering_id": None,
                    "model": None,
                    "voice": None,
                    "options": {},
                }
                for name in fallback_providers(primary_name, available_providers)
            ]
        style_id = self._voice_profile_from_state(state)
        max_attempts = max(0, int(max_retries)) + 1
        max_chars = (
            max(1, int(max_chars_override))
            if max_chars_override is not None
            else int(state.get("max_chars", 1200))
        )
        def resolve_spec(spec: Mapping[str, Any], *, fallback: bool = False):
            provider = self._provider_for_tts_route(spec)
            options: dict[str, Any] = {}
            selected_voice = str(spec.get("voice") or "").strip()
            if selected_voice:
                for alias, declared in self.VOICE_PRESETS.items():
                    if (
                        str(declared.get("provider") or "").strip().casefold()
                        == provider
                        and str(declared.get("voice_name") or "").strip()
                        == selected_voice
                    ):
                        preset = self._preset_payload(alias) or {}
                        # Rehydrate runtime-owned executable/cache settings
                        # without persisting them in the portable route.
                        options.update(preset.get("provider_options") or {})
                        break
            options.update(spec.get("options") or {})
            service = str(spec.get("service") or "").strip().casefold()
            if service in {"hashi_compute", "openrouter"}:
                options["service"] = service
                options["model"] = spec.get("model")
                options["node_id"] = spec.get("node_id")
                options["offering_id"] = spec.get("offering_id")
            requested_style = options.get("style")
            return resolve_tts_route(
                provider=provider,
                voice_name=spec.get("voice"),
                provider_options=options,
                requested_style=requested_style,
                text=text,
                source_language=(
                    primary_route.language.language or source_language
                    if fallback
                    else source_language
                ),
                presets=self.VOICE_PRESETS,
                profiles=self.VOICE_PROFILES,
                voice_profile=style_id,
            )

        primary_route = resolve_spec(primary_spec)
        primary_privacy = self.tts_route_privacy(primary_spec)[0]
        external_allowed = bool(state.get("external_fallback_enabled"))
        blocked_fallbacks: list[str] = []
        permitted_fallback_specs: list[dict[str, Any]] = []
        for spec in fallback_specs:
            if (
                primary_privacy != "external"
                and self.tts_route_privacy(spec)[0] == "external"
                and not external_allowed
            ):
                blocked_fallbacks.append(self.tts_route_label(spec))
                continue
            permitted_fallback_specs.append(spec)
        fallback_chain = tuple(
            self._provider_for_tts_route(spec) for spec in permitted_fallback_specs
        )
        attempts: list[dict[str, object]] = []
        audit: dict[str, object] = {
            "event": "tts_synthesis",
            "agent": str(agent_name),
            "request_id": str(request_id),
            "source_surface": str(source_surface or "unknown"),
            "source_language": normalize_language(source_language),
            "text_sha256": hashlib.sha256(str(text).encode("utf-8")).hexdigest(),
            "text_characters": len(str(text)),
            "configured": {
                "provider": primary_name,
                "voice_name": primary_voice,
                "voice_profile": style_id,
                "performance_style": primary_options.get("style"),
                "provider_option_keys": sorted(
                    str(key) for key in primary_options
                ),
                "route": copy.deepcopy(primary_spec),
                "external_fallback_enabled": external_allowed,
            },
            "language": primary_route.language.as_dict(),
            "routing": {
                "primary": {
                    "provider": primary_route.provider,
                    "voice_name": primary_route.voice_name,
                    "voice_language": primary_route.voice_language,
                    "adapted_from_voice": primary_route.adapted_from_voice,
                    "adaptation_reason": primary_route.adaptation_reason,
                    "style_applied": primary_route.style_applied,
                    "style_reason": primary_route.style_reason,
                    "capability_source": primary_route.capability_source,
                },
                "fallback_candidates": list(fallback_chain),
                "privacy_blocked_candidates": blocked_fallbacks,
            },
            "attempts": attempts,
        }

        def unsupported_failure(route) -> VoiceProviderFailure:
            language = route.language.language or normalize_language(source_language) or "unknown"
            return VoiceProviderFailure(
                route.provider,
                "tts_language_unsupported",
                (
                    f"{_provider_label(route.provider)} has no configured voice "
                    f"that supports language: {language}."
                ),
                False,
            )

        def route_evidence(route) -> dict[str, object]:
            return {
                "voice_language": route.voice_language,
                "adapted_from_voice": route.adapted_from_voice,
                "adaptation_reason": route.adaptation_reason,
                "requested_style": route.requested_style,
                "style_applied": route.style_applied,
                "style_reason": route.style_reason,
                "capability_source": route.capability_source,
                "unsupported_language": route.unsupported_language,
                "provider_option_keys": sorted(route.provider_options),
            }

        async def synthesize_with(route, *, attempt: int, phase: str):
            started = time.perf_counter()
            if route.unsupported_language:
                failure = unsupported_failure(route)
                attempts.append(
                    {
                        "phase": phase,
                        "attempt": attempt + 1,
                        "provider": route.provider,
                        "voice_name": route.voice_name,
                        "status": "skipped",
                        **route_evidence(route),
                        **failure.as_dict(),
                        "duration_ms": 0,
                    }
                )
                return None, failure
            stem = (
                f"{agent_name}_{request_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                f"_{route.provider}_{attempt}"
            )
            try:
                route_rate = int(state.get("rate", 0))
                configured_speed = route.provider_options.get("speed")
                if configured_speed is not None and route.provider in {
                    "edge",
                    "windows",
                    "macos",
                }:
                    route_rate = max(
                        -10,
                        min(10, round((float(configured_speed) - 1.0) * 10)),
                    )
                provider = build_provider(
                    route.provider,
                    ffmpeg_cmd=self.ffmpeg_cmd,
                    project_root=self.project_root,
                    secrets=self._secrets,
                )
                asset = await provider.synthesize(
                    text=text,
                    output_dir=self.output_dir,
                    stem=stem,
                    voice_name=route.voice_name,
                    rate=route_rate,
                    max_chars=max_chars,
                    provider_options=route.provider_options,
                )
            except Exception as exc:
                failure = _provider_failure(route.provider, exc)
                attempts.append(
                    {
                        "phase": phase,
                        "attempt": attempt + 1,
                        "provider": route.provider,
                        "voice_name": route.voice_name,
                        "status": "failed",
                        **route_evidence(route),
                        **failure.as_dict(),
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                    }
                )
                return None, failure
            attempts.append(
                {
                    "phase": phase,
                    "attempt": attempt + 1,
                    "provider": route.provider,
                    "voice_name": asset.voice_name or route.voice_name,
                    "status": "succeeded",
                    **route_evidence(route),
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            )
            return (
                replace(
                    asset,
                    voice_name=asset.voice_name or route.voice_name,
                    language=primary_route.language.language,
                    language_confidence=primary_route.language.confidence,
                    language_scores=dict(primary_route.language.scores),
                    adapted_from_voice=route.adapted_from_voice,
                    adaptation_reason=route.adaptation_reason,
                    style_requested=route.requested_style,
                    style_applied=route.style_applied,
                    style_reason=route.style_reason,
                ),
                None,
            )

        primary_cooldown_key = self._tts_route_cooldown_key(primary_spec)
        primary_failure = self._active_provider_failure(primary_cooldown_key)
        primary_asset: VoiceAsset | None = None
        if primary_failure is None:
            for attempt in range(max_attempts):
                primary_asset, primary_failure = await synthesize_with(
                    primary_route,
                    attempt=attempt,
                    phase="primary",
                )
                if primary_asset is not None:
                    self._provider_cooldowns.pop(primary_cooldown_key, None)
                    notice = self._routing_notice(
                        primary_route=primary_route,
                        final_route=primary_route,
                        final_voice_name=primary_asset.voice_name,
                        fallback_from=None,
                        fallback_reason=None,
                        locale=source_language,
                    )
                    primary_asset = replace(primary_asset, notice=notice)
                    audit["result"] = {
                        "status": "succeeded",
                        "provider": primary_asset.provider,
                        "voice_name": primary_asset.voice_name,
                        "notice": notice,
                    }
                    self._write_voice_audit(audit, logger)
                    return primary_asset
                assert primary_failure is not None
                if not primary_failure.retryable or attempt + 1 >= max_attempts:
                    break
                logger.warning(
                    "Voice synthesis attempt %s/%s failed: %s [%s]. Retrying...",
                    attempt + 1,
                    max_attempts,
                    primary_failure.reason,
                    primary_failure.code,
                )
                await asyncio.sleep(1.5 * (attempt + 1))
        else:
            attempts.append(
                {
                    "phase": "primary",
                    "attempt": 0,
                    "provider": primary_name,
                    "voice_name": primary_route.voice_name,
                    "status": "bypassed",
                    **route_evidence(primary_route),
                    **primary_failure.as_dict(),
                    "duration_ms": 0,
                }
            )

        assert primary_failure is not None
        failures: list[VoiceProviderFailure] = [primary_failure]
        if primary_failure.code != "tts_language_unsupported":
            self._remember_provider_failure(
                primary_failure,
                cooldown_key=primary_cooldown_key,
            )

        for fallback_spec in permitted_fallback_specs:
            fallback_name = self._provider_for_tts_route(fallback_spec)
            fallback_cooldown_key = self._tts_route_cooldown_key(fallback_spec)
            logger.warning(
                "Voice provider %s failed: %s [%s]. Trying monotonic fallback %s.",
                primary_name,
                primary_failure.reason,
                primary_failure.code,
                fallback_name,
            )
            fallback_route = resolve_spec(fallback_spec, fallback=True)
            active_failure = self._active_provider_failure(fallback_cooldown_key)
            if active_failure is not None:
                failures.append(active_failure)
                attempts.append(
                    {
                        "phase": "fallback",
                        "attempt": 0,
                        "provider": fallback_name,
                        "voice_name": fallback_route.voice_name,
                        "status": "bypassed",
                        **route_evidence(fallback_route),
                        **active_failure.as_dict(),
                        "duration_ms": 0,
                    }
                )
                continue
            fallback_asset, fallback_failure = await synthesize_with(
                fallback_route,
                attempt=0,
                phase="fallback",
            )
            if fallback_asset is None:
                assert fallback_failure is not None
                failures.append(fallback_failure)
                if fallback_failure.code != "tts_language_unsupported":
                    self._remember_provider_failure(
                        fallback_failure,
                        cooldown_key=fallback_cooldown_key,
                    )
                continue
            self._provider_cooldowns.pop(fallback_cooldown_key, None)
            notice = self._routing_notice(
                primary_route=primary_route,
                final_route=fallback_route,
                final_voice_name=fallback_asset.voice_name,
                fallback_from=primary_name,
                fallback_reason=primary_failure.reason,
                locale=source_language,
            )
            fallback_asset = replace(
                fallback_asset,
                fallback_from=primary_name,
                fallback_reason_code=primary_failure.code,
                fallback_reason=primary_failure.reason,
                notice=notice,
            )
            audit["result"] = {
                "status": "succeeded_with_fallback",
                "provider": fallback_asset.provider,
                "voice_name": fallback_asset.voice_name,
                "fallback_from": primary_name,
                "fallback_reason_code": primary_failure.code,
                "notice": notice,
            }
            self._write_voice_audit(audit, logger)
            return fallback_asset

        code = (
            primary_failure.code
            if len(failures) == 1
            else "tts_all_providers_failed"
        )
        message = (
            primary_failure.reason
            if len(failures) == 1
            else " ".join(failure.reason for failure in failures)
        )
        audit["result"] = {
            "status": "failed",
            "code": code,
            "failures": [failure.as_dict() for failure in failures],
        }
        self._write_voice_audit(audit, logger)
        raise VoiceSynthesisError(code, message, failures=failures) from None
