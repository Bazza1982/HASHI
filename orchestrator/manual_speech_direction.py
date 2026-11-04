"""Bounded manual direction, shared by voice and frozen call profiles."""
from orchestrator import ui_language

def validate(value):
    if not isinstance(value, dict) or set(value) - {"enabled", "instructions"}:
        raise ValueError(ui_language.tr("voice.manual.invalid"))
    enabled=value.get("enabled", False)
    text=value.get("instructions", "")
    if type(enabled) is not bool or not isinstance(text, str) or len(text)>500:
        raise ValueError(ui_language.tr("voice.manual.invalid"))
    text=text.strip()
    if enabled and not text:
        raise ValueError(ui_language.tr("voice.manual.empty"))
    return {"enabled":enabled, "instructions":text}

def input_button(callback, value):
    from telegram import InlineKeyboardButton
    return InlineKeyboardButton(ui_language.tr("voice.manual.save"), callback_data=callback,
        api_kwargs={"hashi_text_input":{"label":ui_language.tr("voice.manual.label"),
        "value":value.get("instructions",""),"max_length":500}})

def plan(value, model):
    value=validate(value)
    import hashlib
    plan_id=hashlib.sha256((model+"\0"+value["instructions"]).encode()).hexdigest()[:32]
    return {"version":1, "model":model, "plan_id":plan_id, "reason":None, "applied":True, "instructions":value["instructions"],
            "source":"manual", "planning_ms":0, "sampled":False}
