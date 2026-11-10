"""PAO-owned interpretation preferences, independent from conversational Phone."""
from __future__ import annotations

from orchestrator.phone_catalog import PHONE_PROVIDERS
from orchestrator.phone_manager import PhoneManager
from orchestrator.frontend_live_voice.interpretation import resolve_interpretation


class TranslateManager(PhoneManager):
    """Reuse revisioned preference persistence and the qualified media selection."""

    STATE_FILE = "translate_state.json"
    INTERPRETATION_ONLY = True
    PROVIDERS = {
        key: value for key, value in PHONE_PROVIDERS.items()
        if value.get("mode") == "interpretation"
    }
    DEFAULT_PROVIDER = next(iter(PROVIDERS))
    DEFAULT_MODEL = next(iter(PROVIDERS[DEFAULT_PROVIDER]["models"]))
    DEFAULT_STATE = {
        **PhoneManager.DEFAULT_STATE,
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
        "voice": PROVIDERS[DEFAULT_PROVIDER]["models"][DEFAULT_MODEL]["voices"][0],
        "language": PROVIDERS[DEFAULT_PROVIDER]["models"][DEFAULT_MODEL]["target_language"],
    }

    def direction_options(self):
        state = self.get_state()
        return tuple((value["direction"], value["label"])
                     for value in self.PROVIDERS[state["provider"]]["models"].values())

    def set_direction(self, direction):
        state = self.get_state()
        for model, value in self.PROVIDERS[state["provider"]]["models"].items():
            if value["direction"] == direction:
                return self.set_model(model)
        raise ValueError("Unsupported translation direction.")

    def resolve_live_session(self, *, frozen_selection=None, interface_language=None, **_unused):
        # Never load Persona, memory, chat history or background task results.
        state = (self.get_state() if frozen_selection is None
                 else self._normalise_state(dict(frozen_selection)))
        selection = {key: state[key] for key in self.DEFAULT_STATE}
        selection["entrypoint"] = "translate"
        if frozen_selection is not None:
            interface_language = frozen_selection.get("interface_language", interface_language)
        result = resolve_interpretation(selection, interface_language=interface_language)
        result["public"]["entrypoint"] = "translate"
        result["public"]["direction"] = self.PROVIDERS[state["provider"]]["models"][state["model"]]["direction"]
        return result
