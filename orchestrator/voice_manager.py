from __future__ import annotations
import copy
import html
import json
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

from orchestrator.tts_providers import build_provider, list_provider_names
from orchestrator.voice_synthesizer import VoiceAsset
from orchestrator.command_ui import setting_card


def _default_tts_provider() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "edge"


class VoiceManager:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    PIPER_MODEL_DIR = PROJECT_ROOT / "voice_models" / "piper"
    DEFAULT_STATE = {
        "enabled": False,
        "mode": "text_and_voice",
        "provider": _default_tts_provider(),
        "voice_name": None,
        "rate": 0,
        "max_chars": 1200,
        "provider_options": {},
        # A user-facing semantic profile controls both native audio output and
        # the local TTS fallback.  None preserves pre-profile workspaces until
        # their existing raw voice can be inferred or the user chooses one.
        "voice_profile": None,
        # Native audio chat is deliberately independent from the legacy TTS
        # switch above.  Existing workspaces therefore remain native-off.
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
        },
        "euk": {
            "provider": "edge",
            "voice_name": "en-GB-SoniaNeural",
            "label": "Sonia [UK Edge]",
            "language": "English",
        },
        "ecn": {
            "provider": "edge",
            "voice_name": "zh-CN-XiaoxiaoNeural",
            "label": "Xiaoxiao [CN Edge]",
            "language": "Chinese",
        },
        "ecny": {
            "provider": "edge",
            "voice_name": "zh-CN-XiaoyiNeural",
            "label": "Xiaoyi [CN Edge]",
            "language": "Chinese",
        },
        # XiaochenNeural and XiaohanNeural discontinued by Microsoft (2026-03)
        "ectc": {
            "provider": "edge",
            "voice_name": "zh-TW-HsiaoChenNeural",
            "label": "HsiaoChen [TW Edge]",
            "language": "Chinese",
        },
        "ecty": {
            "provider": "edge",
            "voice_name": "zh-TW-HsiaoYuNeural",
            "label": "HsiaoYu [TW Edge]",
            "language": "Chinese",
        },
        "echm": {
            "provider": "edge",
            "voice_name": "zh-HK-HiuMaanNeural",
            "label": "HiuMaan [HK Edge]",
            "language": "Chinese",
        },
        "echg": {
            "provider": "edge",
            "voice_name": "zh-HK-HiuGaaiNeural",
            "label": "HiuGaai [HK Edge]",
            "language": "Chinese",
        },
        "eja": {
            "provider": "edge",
            "voice_name": "ja-JP-NanamiNeural",
            "label": "Nanami [JP Edge]",
            "language": "Japanese",
        },
        "us": {
            "provider": "piper",
            "voice_name": str(PIPER_MODEL_DIR / "en_US-lessac-high.onnx"),
            "label": "Lessac [Piper-US]",
            "language": "English",
        },
        "uk": {
            "provider": "piper",
            "voice_name": str(PIPER_MODEL_DIR / "en_GB-cori-high.onnx"),
            "label": "Cori [Piper-UK]",
            "language": "English",
        },
        "pcn": {
            "provider": "piper",
            "voice_name": str(PIPER_MODEL_DIR / "zh_CN-huayan-medium.onnx"),
            "label": "Huayan [Piper-CN]",
            "language": "Chinese",
        },
        "cn": {
            "provider": "windows",
            "voice_name": "Microsoft Huihui Desktop",
            "label": "Huihui [Win-CN]",
            "language": "Chinese",
        },
        "wus": {
            "provider": "windows",
            "voice_name": "Microsoft Zira Desktop",
            "label": "Zira [Win-US]",
            "language": "English",
        },
        # Kokoro local TTS presets — runs on CPU, no API key needed
        "ko_bella": {
            "provider": "kokoro",
            "voice_name": "af_bella",
            "label": "Bella [Kokoro EN]",
            "language": "English",
            "provider_options": {"lang_code": "a"},
        },
        "ko_heart": {
            "provider": "kokoro",
            "voice_name": "af_heart",
            "label": "Heart [Kokoro EN]",
            "language": "English",
            "provider_options": {"lang_code": "a"},
        },
        "ko_xiaoxiao": {
            "provider": "kokoro",
            "voice_name": "zf_xiaoxiao",
            "label": "Xiaoxiao [Kokoro ZH]",
            "language": "Chinese",
            "provider_options": {"lang_code": "z"},
        },
        "ko_xiaobei": {
            "provider": "kokoro",
            "voice_name": "zf_xiaobei",
            "label": "Xiaobei [Kokoro ZH]",
            "language": "Chinese",
            "provider_options": {"lang_code": "z"},
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
        "maus_ava":      {"provider": "macos", "voice_name": "Ava",      "label": "Ava [macOS EN-US]",       "language": "en-US"},
        "maus_samantha": {"provider": "macos", "voice_name": "Samantha", "label": "Samantha [macOS EN-US]",   "language": "en-US"},
        "maus_alex":     {"provider": "macos", "voice_name": "Alex",     "label": "Alex [macOS EN-US male]",  "language": "en-US"},
        "maus_daniel":   {"provider": "macos", "voice_name": "Daniel",   "label": "Daniel [macOS EN-GB]",     "language": "en-GB"},
        "maus_moira":    {"provider": "macos", "voice_name": "Moira",    "label": "Moira [macOS EN-IE]",      "language": "en-IE"},
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
    ):
        self.workspace_dir = workspace_dir
        self.media_dir = media_dir
        self.state_path = workspace_dir / "voice_state.json"
        self.output_dir = media_dir / "voice"
        self.ffmpeg_cmd = ffmpeg_cmd
        self._secrets = secrets or {}
        self._native_capabilities = tuple(
            dict(item) for item in (native_capabilities or ()) if isinstance(item, dict)
        )

    def _default_piper_exe(self) -> str:
        piper_exe = Path(sys.executable).with_name("piper.exe")
        return str(piper_exe) if piper_exe.exists() else "piper"

    def _default_python_exe(self) -> str:
        return sys.executable

    def _provider_status(self, provider_name: str) -> str:
        name = (provider_name or "").strip().lower()
        if name == "windows":
            return "installed"
        if name == "edge":
            return "installed" if importlib.util.find_spec("edge_tts") else "not installed"
        if name == "piper":
            return "installed" if importlib.util.find_spec("piper") else "not installed"
        if name == "kokoro":
            return "installed" if importlib.util.find_spec("kokoro") else f"not installed in Python {sys.version_info.major}.{sys.version_info.minor}"
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
            provider_options.setdefault("module_mode", True)
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
                and provider_options == (payload.get("provider_options") or {})
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
        for alias, preset in self.VOICE_PRESETS.items():
            available = "ready"
            if preset["provider"] == "piper":
                available = "ready" if Path(preset["voice_name"]).exists() else "missing model"
            rows.append((alias, dict(preset), available))
        return rows

    def get_voice_profiles(self) -> list[tuple[str, dict]]:
        return [
            (profile_id, copy.deepcopy(profile))
            for profile_id, profile in self.VOICE_PROFILES.items()
        ]

    @staticmethod
    def _text_language(text: str) -> str:
        if any("\u3040" <= character <= "\u30ff" for character in text):
            return "ja"
        if any("\u3400" <= character <= "\u9fff" for character in text):
            return "zh"
        return "en"

    @classmethod
    def _voice_profile_from_state(cls, state: dict) -> str | None:
        configured = str(state.get("voice_profile") or "").strip().casefold()
        if configured in cls.VOICE_PROFILES:
            return configured

        # Compatibility inference keeps existing Xiaoxiao/alloy-style
        # workspaces visually stable without rewriting their raw settings.
        tts_voice = str(state.get("voice_name") or "").strip().casefold()
        if tts_voice:
            for profile_id, profile in cls.VOICE_PROFILES.items():
                tts_voices = profile.get("tts_voices") or {}
                if tts_voice in {
                    str(value or "").strip().casefold()
                    for value in tts_voices.values()
                }:
                    return profile_id

        native = cls._native_policy_from_state(state)
        native_voice = str(native.get("voice") or "").strip().casefold()
        if native_voice:
            for profile_id, profile in cls.VOICE_PROFILES.items():
                candidates = {
                    str(value or "").strip().casefold()
                    for value in profile.get("native_voices") or ()
                }
                if native_voice in candidates:
                    return profile_id
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
        state = self._load()
        native = self._native_policy_from_state(state)
        native["voice"] = self._native_voice_for_profile(key, native)
        state["native"] = native
        state["voice_profile"] = key
        state["provider"] = "edge"
        state["voice_name"] = self._tts_voice_for_profile(key, "Hello.")
        state["provider_options"] = {}
        self._save(state)
        return (
            f"Voice set to {profile['label']} "
            f"(native {native['voice']}; language-aware TTS fallback)."
        )

    def get_reply_mode(self) -> str:
        state = self._load()
        native_mode = self._native_policy_from_state(state)["mode"]
        if native_mode in {"auto", "native", "tts"}:
            return native_mode
        return "tts" if state.get("enabled") else "off"

    def set_reply_mode(self, mode: str) -> str:
        normalized = str(mode or "").strip().casefold()
        if normalized not in {"off", "tts", "native", "auto"}:
            raise RuntimeError("Voice mode must be off, tts, native, or auto.")
        state = self._load()
        native = self._native_policy_from_state(state)
        native["mode"] = normalized
        state["native"] = native
        state["enabled"] = normalized == "tts"
        self._save(state)
        return f"Voice mode set to {normalized.upper()}."

    def voice_menu_text(self) -> str:
        state = self._load()
        native = self._native_policy_from_state(state)
        mode = self.get_reply_mode()
        profile_id = self._voice_profile_from_state(state)
        profile_label = (
            self.VOICE_PROFILES[profile_id]["label"]
            if profile_id
            else "Custom"
        )
        reply_labels = {
            "audio_and_text": "Audio + text",
            "audio_only": "Audio only",
            "text_only": "Text only",
        }
        return setting_card(
            "🔊",
            "Voice",
            current=f"<b>{html.escape(mode.upper())}</b>",
            facts=[
                f"<b>Voice</b> · {html.escape(str(profile_label))}",
                f"<b>Reply</b> · {html.escape(reply_labels[native['reply_content']])}",
            ],
            action="Choose a mode and voice below. Advanced typed commands remain available.",
        )

    def _load(self) -> dict:
        if not self.state_path.exists():
            return copy.deepcopy(self.DEFAULT_STATE)
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            merged = copy.deepcopy(self.DEFAULT_STATE)
            merged.update(data if isinstance(data, dict) else {})
            if not isinstance(merged.get("provider_options"), dict):
                merged["provider_options"] = {}
            voice_profile = str(merged.get("voice_profile") or "").strip().casefold()
            merged["voice_profile"] = (
                voice_profile if voice_profile in self.VOICE_PROFILES else None
            )
            raw_native = data.get("native") if isinstance(data, dict) else None
            native = copy.deepcopy(self.DEFAULT_STATE["native"])
            if isinstance(raw_native, dict):
                native.update(raw_native)
            merged["native"] = self._normalise_native_policy(native)
            return merged
        except Exception:
            return copy.deepcopy(self.DEFAULT_STATE)

    def _save(self, payload: dict):
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

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
        policy["mode"] = mode if mode in {"off", "tts", "native", "auto"} else "off"
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
        if policy["mode"] not in {"native", "auto"}:
            resolved["mode"] = policy["mode"]
        return resolved

    def native_audio_enabled(self, terminal: str | None = None) -> bool:
        return self.native_policy_for_terminal(terminal)["mode"] in {
            "native",
            "auto",
        }

    def set_native_mode(self, mode: str) -> str:
        return self.set_reply_mode(mode)

    def set_native_target(self, provider: str | None, model: str | None) -> str:
        state = self._load()
        native = self._native_policy_from_state(state)
        native["provider"] = str(provider or "").strip() or None
        native["model"] = str(model or "").strip() or None
        if bool(native["provider"]) != bool(native["model"]):
            raise RuntimeError("Native provider and model must be configured together.")
        profile_id = str(state.get("voice_profile") or "").strip().casefold()
        if profile_id:
            native["voice"] = self._native_voice_for_profile(profile_id, native)
        state["native"] = native
        self._save(state)
        return f"Native target set to {self._native_target_label(native)}."

    def set_native_voice(self, voice: str | None) -> str:
        state = self._load()
        native = self._native_policy_from_state(state)
        native["voice"] = str(voice or "").strip() or None
        state["native"] = native
        state["voice_profile"] = None
        self._save(state)
        return f"Native voice set to {native['voice'] or 'configured default'}."

    def set_native_format(self, audio_format: str | None) -> str:
        state = self._load()
        native = self._native_policy_from_state(state)
        native["format"] = str(audio_format or "").strip().casefold() or None
        state["native"] = native
        self._save(state)
        return f"Native format set to {native['format'] or 'capability-selected default'}."

    def set_native_reply_content(self, content: str) -> str:
        aliases = {
            "both": "audio_and_text",
            "audio": "audio_only",
            "text": "text_only",
        }
        normalized = aliases.get(str(content or "").strip().casefold(), str(content or "").strip().casefold())
        if normalized not in {"audio_and_text", "audio_only", "text_only"}:
            raise RuntimeError("Reply content must be both, audio, or text.")
        state = self._load()
        native = self._native_policy_from_state(state)
        native["reply_content"] = normalized
        state["native"] = native
        self._save(state)
        return f"Native reply content set to {normalized}."

    def set_native_fallback(self, fallback: str) -> str:
        aliases = {"local": "local_chain", "none": "native_only"}
        normalized = aliases.get(str(fallback or "").strip().casefold(), str(fallback or "").strip().casefold())
        if normalized not in {"local_chain", "native_only"}:
            raise RuntimeError("Fallback must be local_chain or native_only.")
        state = self._load()
        native = self._native_policy_from_state(state)
        native["fallback"] = normalized
        state["native"] = native
        self._save(state)
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
        state = self._load()
        native = self._native_policy_from_state(state)
        native["retention_seconds"] = retention
        state["native"] = native
        self._save(state)
        return f"Native audio retention set to {self._retention_label(retention)}."

    def set_output_transcript_echo(self, enabled: bool) -> str:
        state = self._load()
        native = self._native_policy_from_state(state)
        native["output_transcript_echo"] = bool(enabled)
        native["reply_content"] = (
            "audio_and_text" if enabled else "audio_only"
        )
        state["native"] = native
        self._save(state)
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
        state = self._load()
        state["enabled"] = bool(enabled)
        self._save(state)
        return f"Voice replies are now {'ON' if enabled else 'OFF'}."

    def set_provider(self, provider_name: str) -> str:
        name = (provider_name or "").strip().lower()
        if name not in self.list_providers():
            raise RuntimeError(f"Unknown voice provider: {provider_name}. Available: {', '.join(self.list_providers())}")
        state = self._load()
        state["provider"] = name
        state["voice_profile"] = None
        self._save(state)
        return f"Voice provider set to {name}."

    def set_voice_name(self, voice_name: str) -> str:
        preset = self._preset_payload(voice_name)
        if preset:
            return self.apply_voice_preset(voice_name)
        state = self._load()
        state["voice_name"] = voice_name.strip() or None
        state["voice_profile"] = None
        self._save(state)
        return f"Voice name set to {state['voice_name'] or 'default'}."

    def set_rate(self, rate: int) -> str:
        state = self._load()
        state["rate"] = int(rate)
        self._save(state)
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
        state = self._load()
        state["provider"] = preset["provider"]
        state["voice_name"] = preset["voice_name"]
        state["provider_options"] = dict(preset.get("provider_options") or {})
        state["voice_profile"] = None
        self._save(state)
        return f"Voice preset set to {alias}: {preset['label']}."

    async def synthesize_reply(
        self,
        agent_name: str,
        request_id: str,
        text: str,
        max_retries: int = 2,
        force: bool = False,
        max_chars_override: int | None = None,
    ) -> VoiceAsset | None:
        import asyncio
        import logging
        logger = logging.getLogger(f"Runtime.{agent_name}.voice")

        state = self._load()
        if not force and not state.get("enabled"):
            return None

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                stem = f"{agent_name}_{request_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{attempt}"
                provider_name = state.get("provider", "windows")
                provider = build_provider(provider_name, ffmpeg_cmd=self.ffmpeg_cmd)
                voice_name = state.get("voice_name")
                profile_id = str(state.get("voice_profile") or "").strip().casefold()
                if provider_name == "edge" and profile_id in self.VOICE_PROFILES:
                    voice_name = self._tts_voice_for_profile(profile_id, text)
                return await provider.synthesize(
                    text=text,
                    output_dir=self.output_dir,
                    stem=stem,
                    voice_name=voice_name,
                    rate=int(state.get("rate", 0)),
                    max_chars=(
                        max(1, int(max_chars_override))
                        if max_chars_override is not None
                        else int(state.get("max_chars", 1200))
                    ),
                    provider_options=state.get("provider_options") or {},
                )
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    logger.warning(f"Voice synthesis attempt {attempt + 1}/{max_retries + 1} failed: {e}. Retrying...")
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise last_err
