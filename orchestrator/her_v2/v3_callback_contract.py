from __future__ import annotations

import re

HER_V3_CALLBACK_MODEL = "herv3_model"
HER_V3_CALLBACK_PROVIDER = "herv3_provider"
HER_V3_CALLBACK_PROVIDER_LOCKED = "herv3_provider_locked"
HER_V3_CALLBACK_PROVIDER_MENU = "herv3_provider_menu"

HER_V3_MODEL_CALLBACK_NAMES = (
    HER_V3_CALLBACK_MODEL,
    HER_V3_CALLBACK_PROVIDER,
    HER_V3_CALLBACK_PROVIDER_LOCKED,
    HER_V3_CALLBACK_PROVIDER_MENU,
)
HER_V3_MODEL_CALLBACK_PATTERN = (
    r"^(?:"
    + "|".join(re.escape(name) for name in HER_V3_MODEL_CALLBACK_NAMES)
    + r")(?::|$)"
)


def her_v3_callback_data(name: str, *parts: object) -> str:
    if name not in HER_V3_MODEL_CALLBACK_NAMES:
        raise ValueError(f"unknown HERV3 callback name: {name}")
    values = (name, *(str(part) for part in parts))
    return ":".join(values)
