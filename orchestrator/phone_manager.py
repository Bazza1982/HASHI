"""Per-Agent configuration for full-duplex Live Voice phone sessions.

This is deliberately separate from :mod:`orchestrator.voice_manager`: /voice
owns rendered voice replies, while /phone owns a live provider session.  The
browser never supplies these values; it can only read the effective public
projection selected in the Agent workspace.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

from orchestrator.config_json import ConfigConflictError, read_config_json, write_config_json
from orchestrator.phone_catalog import (
    OPENAI_LIVE_VOICE_PRESENTATIONS,
    PHONE_LANGUAGES,
    PHONE_PROVIDERS,
    PHONE_STYLES,
)
from orchestrator.pcm_voice_projection import (
    build_live_voice_input,
    build_live_voice_instructions,
    load_phone_pcm_payload,
    load_phone_persona,
)


class PhoneConfigError(RuntimeError):
    """A stable, non-secret configuration failure safe to map at an API edge."""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        super().__init__(message)


class PhoneManager:
    SCHEMA_VERSION = 1
    MAX_CUSTOM_INSTRUCTIONS_CHARS = 1200
    PROVIDERS = PHONE_PROVIDERS
    VOICE_LABELS = {
        voice: voice.title()
        for provider_data in PROVIDERS.values()
        for model_data in provider_data["models"].values()
        for voice in model_data["voices"]
    }
    VOICE_PRESENTATIONS = OPENAI_LIVE_VOICE_PRESENTATIONS
    LANGUAGES = PHONE_LANGUAGES
    STYLES = PHONE_STYLES
    DEFAULT_STATE = {
        "schema_version": SCHEMA_VERSION,
        "provider": "openai",
        "model": "gpt-live-1",
        "voice": "marin",
        "language": "auto",
        "style": "natural",
        "style_instructions": "",
    }

    def __init__(self, workspace_dir: str | Path):
        self.workspace_dir = Path(workspace_dir)
        self.state_path = self.workspace_dir / "phone_state.json"

    @classmethod
    def _validate_choice(cls, value: Any, choices: Any, field: str) -> str:
        selected = str(value or "").strip()
        if selected not in choices:
            raise PhoneConfigError(
                f"phone_{field}_unsupported",
                f"Unsupported phone {field}: {selected or '(empty)'}."
            )
        return selected

    @classmethod
    def _normalise_state(cls, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise PhoneConfigError("phone_state_invalid", "Phone configuration must be a JSON object.")
        state = copy.deepcopy(cls.DEFAULT_STATE)
        state.update(copy.deepcopy(raw))
        provider = cls._validate_choice(state.get("provider"), cls.PROVIDERS, "provider")
        models = cls.PROVIDERS[provider]["models"]
        model = cls._validate_choice(state.get("model"), models, "model")
        voice = cls._validate_choice(state.get("voice"), models[model]["voices"], "voice")
        language = cls._validate_choice(state.get("language"), cls.LANGUAGES, "language")
        style = cls._validate_choice(state.get("style"), cls.STYLES, "style")
        custom = state.get("style_instructions", "")
        if not isinstance(custom, str):
            raise PhoneConfigError(
                "phone_style_instructions_invalid",
                "Phone style instructions must be text.",
            )
        custom = custom.strip()
        if len(custom) > cls.MAX_CUSTOM_INSTRUCTIONS_CHARS:
            raise PhoneConfigError(
                "phone_style_instructions_too_long",
                f"Phone style instructions may contain at most {cls.MAX_CUSTOM_INSTRUCTIONS_CHARS} characters.",
            )
        state.update({
            "schema_version": cls.SCHEMA_VERSION,
            "provider": provider,
            "model": model,
            "voice": voice,
            "language": language,
            "style": style,
            "style_instructions": custom,
        })
        return state

    def _read(self) -> tuple[dict[str, Any], str | None]:
        try:
            document = read_config_json(self.state_path)
        except FileNotFoundError:
            return copy.deepcopy(self.DEFAULT_STATE), None
        except (OSError, ValueError, TypeError) as exc:
            raise PhoneConfigError(
                "phone_state_unreadable",
                f"Phone configuration is unreadable; no default was silently substituted: {type(exc).__name__}.",
            ) from exc
        return self._normalise_state(dict(document)), document.revision

    def get_state(self) -> dict[str, Any]:
        state, _revision = self._read()
        return copy.deepcopy(state)

    def _update(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        state, revision = self._read()
        mutate(state)
        state = self._normalise_state(state)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            write_config_json(self.state_path, state, expected_revision=revision)
        except ConfigConflictError as exc:
            raise PhoneConfigError(
                "phone_state_conflict",
                "Phone configuration changed since it was opened; reopen /phone and try again.",
            ) from exc
        return copy.deepcopy(state)

    @classmethod
    def provider_options(cls) -> tuple[tuple[str, str], ...]:
        return tuple((key, str(value["label"])) for key, value in cls.PROVIDERS.items())

    def model_options(self) -> tuple[tuple[str, str], ...]:
        state = self.get_state()
        return tuple(
            (key, str(value["label"]))
            for key, value in self.PROVIDERS[state["provider"]]["models"].items()
        )

    def voice_options(self) -> tuple[tuple[str, str], ...]:
        state = self.get_state()
        voices = self.PROVIDERS[state["provider"]]["models"][state["model"]]["voices"]
        return tuple((voice, self.VOICE_LABELS.get(voice, voice.title())) for voice in voices)

    @classmethod
    def voice_presentation(cls, voice: str) -> str | None:
        return cls.VOICE_PRESENTATIONS.get(str(voice))

    @classmethod
    def language_options(cls) -> tuple[tuple[str, str], ...]:
        return tuple((key, str(value["label"])) for key, value in cls.LANGUAGES.items())

    @classmethod
    def style_options(cls) -> tuple[tuple[str, str], ...]:
        return tuple((key, str(value["label"])) for key, value in cls.STYLES.items())

    def set_provider(self, value: str) -> dict[str, Any]:
        provider = self._validate_choice(value, self.PROVIDERS, "provider")
        def mutate(state: dict[str, Any]) -> None:
            state["provider"] = provider
            models = self.PROVIDERS[provider]["models"]
            if state.get("model") not in models:
                state["model"] = next(iter(models))
            voices = models[state["model"]]["voices"]
            if state.get("voice") not in voices:
                state["voice"] = voices[0]
        return self._update(mutate)

    def set_model(self, value: str) -> dict[str, Any]:
        current = self.get_state()
        models = self.PROVIDERS[current["provider"]]["models"]
        model = self._validate_choice(value, models, "model")
        def mutate(state: dict[str, Any]) -> None:
            state["model"] = model
            voices = self.PROVIDERS[state["provider"]]["models"][model]["voices"]
            if state.get("voice") not in voices:
                state["voice"] = voices[0]
        return self._update(mutate)

    def set_voice(self, value: str) -> dict[str, Any]:
        current = self.get_state()
        voices = self.PROVIDERS[current["provider"]]["models"][current["model"]]["voices"]
        voice = self._validate_choice(value, voices, "voice")
        return self._update(lambda state: state.__setitem__("voice", voice))

    def set_language(self, value: str) -> dict[str, Any]:
        language = self._validate_choice(value, self.LANGUAGES, "language")
        return self._update(lambda state: state.__setitem__("language", language))

    def set_style(self, value: str) -> dict[str, Any]:
        style = self._validate_choice(value, self.STYLES, "style")
        return self._update(lambda state: state.__setitem__("style", style))

    def set_style_instructions(self, value: str | None) -> dict[str, Any]:
        custom = str(value or "").strip()
        if len(custom) > self.MAX_CUSTOM_INSTRUCTIONS_CHARS:
            raise PhoneConfigError(
                "phone_style_instructions_too_long",
                f"Phone style instructions may contain at most {self.MAX_CUSTOM_INSTRUCTIONS_CHARS} characters.",
            )
        return self._update(lambda state: state.__setitem__("style_instructions", custom))

    def reset(self) -> dict[str, Any]:
        def mutate(state: dict[str, Any]) -> None:
            for key, value in self.DEFAULT_STATE.items():
                state[key] = copy.deepcopy(value)
        return self._update(mutate)

    def resolve_live_session(
        self,
        *,
        display_name: str,
        agent_id: str | None = None,
        pcm_payload: Mapping[str, Any] | None = None,
        recent_history: Iterable[Mapping[str, Any]] = (),
        interface_language: str | None = None,
        frozen_selection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Freeze the effective provider request without exposing private PCM."""

        if frozen_selection is None:
            state, _state_revision = self._read()
        else:
            # This server-owned call snapshot is not a writable config fallback.
            # Re-read current persona/authority below, but retain phone choices.
            state = self._normalise_state(dict(frozen_selection))
            interface_language = frozen_selection.get("interface_language", interface_language)
        persona = load_phone_persona(self.workspace_dir)
        effective_pcm = (
            dict(pcm_payload)
            if isinstance(pcm_payload, Mapping)
            else load_phone_pcm_payload(self.workspace_dir)
        )
        resolved_agent_id = str(agent_id or self.workspace_dir.name)
        resolved_display_name = str(display_name or resolved_agent_id)
        from orchestrator.frontend_live_voice.provider import default_registry, select_provider
        adapter = select_provider(default_registry(include_experimental=True), state["provider"])
        instructions = build_live_voice_instructions(
            agent_id=resolved_agent_id,
            display_name=resolved_display_name,
            persona=persona.text,
            language_instruction=self.LANGUAGES[state["language"]]["instruction"],
            style_instruction=self.STYLES[state["style"]]["instruction"],
            custom_style_instruction=state["style_instructions"],
            pcm_payload=effective_pcm,
            instruction_token_limit=adapter.capabilities.max_instruction_tokens,
        )
        startup_input, context_audit = build_live_voice_input(
            effective_pcm,
            recent_history,
            message_limit=adapter.capabilities.max_input_messages,
            input_token_limit=adapter.capabilities.max_input_tokens,
        )
        revision_payload = {
            "schema_version": self.SCHEMA_VERSION,
            "provider": state["provider"],
            "model": state["model"],
            "voice": state["voice"],
            "language": state["language"],
            "style": state["style"],
            "style_instructions": state["style_instructions"],
            "pcm_sha256": persona.content_sha256,
            "instructions_sha256": hashlib.sha256(
                instructions.encode("utf-8")
            ).hexdigest(),
            "agent_id": resolved_agent_id,
            "display_name": resolved_display_name,
            "interface_language": interface_language,
        }
        revision = hashlib.sha256(
            json.dumps(revision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        public = {
            "revision": revision,
            "provider": state["provider"],
            "provider_label": self.PROVIDERS[state["provider"]]["label"],
            "model": state["model"],
            "model_label": self.PROVIDERS[state["provider"]]["models"][state["model"]]["label"],
            "voice": state["voice"],
            "voice_label": self.VOICE_LABELS.get(state["voice"], state["voice"].title()),
            "voice_presentation": self.voice_presentation(state["voice"]),
            "language": state["language"],
            "language_label": self.LANGUAGES[state["language"]]["label"],
            "style": state["style"],
            "style_label": self.STYLES[state["style"]]["label"],
            "custom_style": bool(state["style_instructions"]),
            "persona_projected": True,
            "interface_language": interface_language,
        }
        return {
            "provider": state["provider"],
            "model": state["model"],
            "voice": state["voice"],
            "instructions": instructions,
            "input": startup_input,
            "context_audit": context_audit,
            "public": public,
            "selection": {
                **{key: state[key] for key in self.DEFAULT_STATE},
                "interface_language": interface_language,
            },
        }
