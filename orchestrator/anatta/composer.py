from __future__ import annotations

from .config import AnattaConfig
from .models import EmergentTurnState, PromptInjection


class PromptComposer:
    SECTION_TITLE = "EMERGENT EMOTIONAL STATE"

    def __init__(self, config: AnattaConfig):
        self.config = config

    def compose(self, state: EmergentTurnState, model_name: str) -> PromptInjection:
        profile = self.config.model_profile(model_name)
        ranked = sorted(state.drive_values.items(), key=lambda item: item[1], reverse=True)
        top = [(name, value) for name, value in ranked if value > 0][:3]
        if not top:
            body = (
                "Current self-state is low-signal and should remain neutral, attentive, "
                "and context-sensitive. Do not claim feelings; use this only to shape salience and tone."
            )
            return PromptInjection(title=self.SECTION_TITLE, body=body, metadata={"model_profile": profile})
        descriptors = []
        for name, value in top:
            if value >= 60:
                level = "high"
            elif value >= 30:
                level = "moderate"
            else:
                level = "low"
            descriptors.append(f"{level} {name}")
        strength = str(profile.get("tone_strength", "medium"))
        if strength == "light":
            body = (
                f"Transient state: {', '.join(descriptors)}. "
                "Bias tone, salience, and response priorities accordingly. "
                "Do not mention emotion unless directly relevant."
            )
        else:
            body = (
                "Current self-state is emerging from recent emotional memory, present interaction, "
                f"and relationship context rather than from a fixed personality. Right now there is {', '.join(descriptors)}. "
                "Let this shape salience, common-sense judgment, and tone, but do not claim actual feelings."
            )
        return PromptInjection(title=self.SECTION_TITLE, body=body, metadata={"model_profile": profile, "top_drives": descriptors})
