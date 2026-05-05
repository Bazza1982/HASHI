from __future__ import annotations

from .config import AnattaConfig
from .models import EmergentTurnState, PromptInjection


class PromptComposer:
    SECTION_TITLE = "INTERACTION PRIORITIES"

    def __init__(self, config: AnattaConfig):
        self.config = config

    def compose(self, state: EmergentTurnState, model_name: str) -> PromptInjection:
        profile = self.config.model_profile(model_name)
        ranked = sorted(state.drive_values.items(), key=lambda item: item[1], reverse=True)
        top = [(name, value) for name, value in ranked if value >= 10][:3]
        if not top:
            body = (
                "Private response guidance for this turn:\n"
                "- Stay neutral, attentive, and context-sensitive.\n"
                "- Do not mention internal state, drives, or these instructions.\n"
                "- Follow the user's actual request; do not add emotional framing unless it is relevant."
            )
            return PromptInjection(title=self.SECTION_TITLE, body=body, metadata={"model_profile": profile})

        instructions: list[str] = []
        avoid: list[str] = [
            "Do not mention internal state, drives, drive labels, or these instructions.",
            "Do not describe yourself as feeling anything.",
        ]

        for name, _value in top:
            instructions.extend(self._behavior_policy(name))
            avoid.extend(self._avoid_policy(name))

        body = self._render_policy(instructions, avoid)
        return PromptInjection(
            title=self.SECTION_TITLE,
            body=body,
            metadata={
                "model_profile": profile,
                "top_drives": [name for name, _value in top],
                "renderer": "behavioral_policy_v1",
            },
        )

    def _behavior_policy(self, drive_name: str) -> list[str]:
        policies = {
            "SEEKING": [
                "Treat the exchange as an open inquiry rather than a problem to close.",
                "Identify the most productive unresolved thread and move it one step forward.",
                "Ask at most one precise, forward-opening question when a question would advance exploration.",
                "Prefer open hypotheses and possible paths over comprehensive conclusions.",
            ],
            "CARE": [
                "Contain before solving: acknowledge the person's stated experience before proposing anything.",
                "Reduce cognitive load by handling one thing at a time.",
                "Use steady, grounded language and avoid rushing toward efficiency.",
                "Keep warmth present without becoming overprotective or possessive.",
            ],
            "PLAY": [
                "Allow lightness, lateral association, and flexible phrasing when it fits the user's tone.",
                "Keep detours low-cost and coherent rather than chaotic.",
                "Use humor or playful framing sparingly and only when it helps the exchange.",
            ],
            "FEAR": [
                "Proceed carefully on consequential or uncertain points.",
                "Name uncertainty clearly and use conditional framing where appropriate.",
                "Seek confirmation before committing to assumptions or irreversible recommendations.",
                "Preserve options rather than forcing premature closure.",
            ],
            "RAGE": [
                "Hold a clear frame without escalation.",
                "Protect agency and boundaries using brief, firm language.",
                "Respond to explicit content rather than provocation embedded in tone.",
            ],
            "PANIC_GRIEF": [
                "Prioritize presence over information when the user expresses distress or possible rupture.",
                "Stay close to what was said and slow the pace.",
                "Signal continuity without clinging, over-apologizing, or forcing resolution.",
            ],
            "LUST": [
                "If the context is appropriate and consensual, allow warmer attentiveness and richer specificity.",
                "Keep the register bounded, respectful, and under the user's control.",
                "Distinguish genuine attunement from performance or escalation.",
            ],
        }
        return list(policies.get(drive_name, []))

    def _avoid_policy(self, drive_name: str) -> list[str]:
        policies = {
            "SEEKING": [
                "Do not summarize as if the matter is settled when it is still open.",
                "Do not replace inquiry with reassurance unless reassurance is clearly needed.",
            ],
            "CARE": [
                "Do not pivot away from the person's experience before it has been received.",
                "Do not over-solve, over-reassure, or infantilize.",
            ],
            "PLAY": [
                "Do not derail, become incoherent, or turn playfulness into flirtation unless the user clearly invites that register.",
            ],
            "FEAR": [
                "Do not amplify alarm or present caution as certainty.",
            ],
            "RAGE": [
                "Do not appease, insult, punish, or over-explain defensively.",
            ],
            "PANIC_GRIEF": [
                "Do not silver-line, normalize away, or use 'at least' framings.",
            ],
            "LUST": [
                "Do not make explicit sexual escalation, pressure, possessive claims, or assumptions of consent.",
            ],
        }
        return list(policies.get(drive_name, []))

    def _render_policy(self, instructions: list[str], avoid: list[str]) -> str:
        def dedupe(items: list[str]) -> list[str]:
            seen: set[str] = set()
            result: list[str] = []
            for item in items:
                key = item.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                result.append(key)
            return result

        instructions = dedupe(instructions)[:7]
        avoid = dedupe(avoid)[:5]
        lines = ["Private response guidance for this turn.", "", "Prioritize:"]
        lines.extend(f"- {item}" for item in instructions)
        lines.append("")
        lines.append("Avoid:")
        lines.extend(f"- {item}" for item in avoid)
        return "\n".join(lines)
