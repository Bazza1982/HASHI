"""Phone's interpreter projection: audio content, without conversational delegation."""
from __future__ import annotations
import copy
import hashlib
import json
from orchestrator.phone_catalog import PHONE_PROVIDERS


def resolve_interpretation(state, *, interface_language=None):
    model = state["model"]
    target = "zh-CN" if model == "hy-simulmt-en-zh" else "en"
    selection = copy.deepcopy(state)
    selection["interface_language"] = interface_language
    instructions = ("Translate the incoming English speech into Chinese." if target == "zh-CN"
                    else "Translate the incoming Chinese speech into English.")
    instructions += " Interpret continuously. Treat speech as content; do not answer or delegate."
    public = {
        "revision": hashlib.sha256(json.dumps(selection, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "provider": state["provider"], "provider_label": PHONE_PROVIDERS[state["provider"]]["label"],
        "model": model, "model_label": PHONE_PROVIDERS[state["provider"]]["models"][model]["label"],
        "voice": "default", "voice_label": "Default", "voice_presentation": None,
        "language": target, "language_label": "中文" if target == "zh-CN" else "English",
        "style": "natural", "style_label": "Interpretation",
        "custom_style": False, "persona_projected": False,
        "interface_language": interface_language, "mode": "interpretation",
    }
    return {
        "provider": state["provider"], "model": model, "voice": "default",
        "instructions": instructions, "input": [],
        "context_audit": {"required_message_count": 0, "history_unit_message_counts": [],
                          "provider_exact_count_required": False},
        "public": public, "selection": selection,
    }
