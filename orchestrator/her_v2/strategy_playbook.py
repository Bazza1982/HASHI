"""Versioned, data-driven Strategy Card Playbook support for HER v2."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence


class StrategyPlaybookError(RuntimeError):
    """Raised when the external Strategy Card catalogue is invalid."""


PLAYBOOK_ASSET = (
    Path(__file__).with_name("playbook_assets") / "strategy_playbook.json"
)


@dataclass(frozen=True)
class StrategyPlaybookSnapshot:
    """One immutable-by-convention Playbook snapshot used for a turn."""

    playbook_version: str
    cards: tuple[Mapping[str, Any], ...]
    sha256: str

    @property
    def card_ids(self) -> tuple[str, ...]:
        return tuple(str(card["id"]) for card in self.cards)

    def prompt_payload(self) -> Mapping[str, Any]:
        return {
            "playbook_version": self.playbook_version,
            "sha256": self.sha256,
            "cards": [copy.deepcopy(dict(card)) for card in self.cards],
        }

    def render(self) -> str:
        return json.dumps(
            self.prompt_payload(), ensure_ascii=False, indent=2, sort_keys=True
        )

    def resolve_cards(
        self, card_ids: Sequence[str]
    ) -> tuple[Mapping[str, Any], ...]:
        by_id = {str(card["id"]): card for card in self.cards}
        resolved: list[Mapping[str, Any]] = []
        for raw_card_id in card_ids:
            card_id = str(raw_card_id or "").strip()
            card = by_id.get(card_id)
            if card is None:
                raise StrategyPlaybookError(
                    f"unknown Strategy Card ID {card_id!r} for Playbook "
                    f"{self.playbook_version!r}"
                )
            resolved.append(copy.deepcopy(dict(card)))
        return tuple(resolved)


def _require_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StrategyPlaybookError(f"Strategy Playbook {field} must be non-empty")
    return text


@lru_cache(maxsize=1)
def load_strategy_playbook() -> StrategyPlaybookSnapshot:
    """Load, validate, freeze, and hash the external Strategy Card catalogue."""

    try:
        raw_text = PLAYBOOK_ASSET.read_text(encoding="utf-8")
    except OSError as exc:
        raise StrategyPlaybookError(
            f"cannot read Strategy Playbook asset: {exc}"
        ) from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StrategyPlaybookError(
            f"Strategy Playbook is not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise StrategyPlaybookError("Strategy Playbook root must be an object")

    playbook_version = _require_text(
        raw.get("playbook_version"), field="playbook_version"
    )
    raw_cards = raw.get("cards")
    if not isinstance(raw_cards, list) or not raw_cards:
        raise StrategyPlaybookError("Strategy Playbook cards must be a non-empty list")

    cards: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, raw_card in enumerate(raw_cards):
        if not isinstance(raw_card, Mapping):
            raise StrategyPlaybookError(
                f"Strategy Playbook card {index} must be an object"
            )
        card = copy.deepcopy(dict(raw_card))
        card_id = _require_text(card.get("id"), field=f"cards[{index}].id")
        _require_text(card.get("version"), field=f"cards[{index}].version")
        _require_text(card.get("title"), field=f"cards[{index}].title")
        _require_text(card.get("content"), field=f"cards[{index}].content")
        for field_name in (
            "use_when",
            "avoid_when",
            "strategy",
            "validation",
            "failure_modes",
        ):
            values = card.get(field_name)
            if not isinstance(values, list) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise StrategyPlaybookError(
                    f"Strategy Playbook cards[{index}].{field_name} must be a "
                    "list of non-empty strings"
                )
        for field_name in ("topology", "composition"):
            if not isinstance(card.get(field_name), Mapping):
                raise StrategyPlaybookError(
                    f"Strategy Playbook cards[{index}].{field_name} must be an object"
                )
        if card_id in seen:
            raise StrategyPlaybookError(
                f"duplicate Strategy Card ID {card_id!r}"
            )
        seen.add(card_id)
        cards.append(card)

    canonical = json.dumps(
        {
            "playbook_version": playbook_version,
            "cards": cards,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return StrategyPlaybookSnapshot(
        playbook_version=playbook_version,
        cards=tuple(cards),
        sha256="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )
