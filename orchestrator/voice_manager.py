from __future__ import annotations
import json
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

from orchestrator.tts_providers import build_provider, list_provider_names
from orchestrator.voice_synthesizer import VoiceAsset


class VoiceManager:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    PIPER_MODEL_DIR = PROJECT_ROOT / "voice_models" / "piper"
    DEFAULT_STATE = {
        "enabled": False,
        "mode": "text_and_voice",
        "provider": "windows",
        "voice_name": None,
        "rate": 0,
        "max_chars": 1200,
        "provider_options": {},
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
    }

    def __init__(self, workspace_dir: Path, media_dir: Path, ffmpeg_cmd: str = "ffmpeg"):
        self.workspace_dir = workspace_dir
        self.media_dir = media_dir
        self.state_path = workspace_dir / "voice_state.json"
        self.output_dir = media_dir / "voice"
        self.ffmpeg_cmd = ffmpeg_cmd

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

    def voice_menu_text(self) -> str:
        state = self._load()
        enabled = "ON" if state.get("enabled") else "OFF"
        active_alias = self.get_active_preset_alias()
        if active_alias:
            preset = self.VOICE_PRESETS[active_alias]
            current = f"{active_alias} - {preset['label']}"
        else:
            current = state.get("voice_name") or "custom"
        return (
            "Voice Replies\n"
            f"Status: {enabled}\n"
            f"Current: {current}\n"
            f"Provider: {state.get('provider', 'windows')}\n"
            "Tap a preset below or use typed commands for advanced options."
        )

    def _load(self) -> dict:
        if not self.state_path.exists():
            return dict(self.DEFAULT_STATE)
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            merged = dict(self.DEFAULT_STATE)
            merged.update(data if isinstance(data, dict) else {})
            if not isinstance(merged.get("provider_options"), dict):
                merged["provider_options"] = {}
            return merged
        except Exception:
            return dict(self.DEFAULT_STATE)

    def _save(self, payload: dict):
        self.state_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    def get_state(self) -> dict:
        return self._load()

    def is_enabled(self) -> bool:
        return bool(self._load().get("enabled"))

    def get_provider_name(self) -> str:
        return str(self._load().get("provider") or "windows")

    def list_providers(self) -> list[str]:
        return list_provider_names()

    def describe(self) -> str:
        state = self._load()
        status = "ON" if state.get("enabled") else "OFF"
        voice_name = state.get("voice_name") or "default"
        preset = self.get_active_preset_alias()
        preset_line = f"Preset: {preset}\n" if preset else ""
        return (
            f"Voice: {status}\n"
            f"Mode: {state.get('mode', 'text_and_voice')}\n"
            f"Provider: {state.get('provider', 'windows')}\n"
            f"{preset_line}"
            f"Voice Name: {voice_name}\n"
            f"Rate: {state.get('rate', 0)}\n"
            f"Max Chars: {state.get('max_chars', 1200)}"
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
        self._save(state)
        return f"Voice provider set to {name}."

    def set_voice_name(self, voice_name: str) -> str:
        preset = self._preset_payload(voice_name)
        if preset:
            return self.apply_voice_preset(voice_name)
        state = self._load()
        state["voice_name"] = voice_name.strip() or None
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
            f"- coqui: Coqui TTS Python package [{self._provider_status('coqui')}]"
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
        self._save(state)
        return f"Voice preset set to {alias}: {preset['label']}."

    async def synthesize_reply(self, agent_name: str, request_id: str, text: str, max_retries: int = 2) -> VoiceAsset | None:
        import asyncio
        import logging
        logger = logging.getLogger(f"Runtime.{agent_name}.voice")

        state = self._load()
        if not state.get("enabled"):
            return None

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                stem = f"{agent_name}_{request_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{attempt}"
                provider = build_provider(state.get("provider", "windows"), ffmpeg_cmd=self.ffmpeg_cmd)
                return await provider.synthesize(
                    text=text,
                    output_dir=self.output_dir,
                    stem=stem,
                    voice_name=state.get("voice_name"),
                    rate=int(state.get("rate", 0)),
                    max_chars=int(state.get("max_chars", 1200)),
                    provider_options=state.get("provider_options") or {},
                )
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    logger.warning(f"Voice synthesis attempt {attempt + 1}/{max_retries + 1} failed: {e}. Retrying...")
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise last_err
