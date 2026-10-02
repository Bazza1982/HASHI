#!/usr/bin/env python3
"""One-shot, local Presidio/spaCy detector for the HERV3 privacy pilot.

Input and output are JSON over local pipes. Output contains offsets and labels,
never the supplied text. This process requires an isolated Python environment.
"""

from __future__ import annotations

import json
import sys


ALLOWED_LABELS = frozenset(
    {
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "AU_TFN", "AU_MEDICARE",
        "AU_ABN", "AU_ACN", "CREDIT_CARD", "US_BANK_NUMBER", "IBAN_CODE",
        "US_SSN", "IP_ADDRESS",
    }
)


def main() -> int:
    try:
        from pii_model_probe import _presidio_detector

        request = json.load(sys.stdin)
        texts = request.get("texts")
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            raise ValueError("invalid input")
        detect = _presidio_detector("", 0.35)
        matches = [
            [
                {"start": item["start"], "end": item["end"],
                 "label": item["label"], "score": item["score"]}
                for item in detect(text) if item["label"] in ALLOWED_LABELS
            ]
            for text in texts
        ]
        json.dump({"ok": True, "matches": matches}, sys.stdout)
        return 0
    except Exception:
        # The parent treats every failure as a block. Never print the input or
        # exception because libraries can embed source text in error messages.
        json.dump({"ok": False}, sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
