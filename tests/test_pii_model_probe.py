from __future__ import annotations

from tools.pii_model_probe import _non_overlapping_entities, _redact


def test_redaction_resolves_nested_detector_spans_without_corrupting_text() -> None:
    text = "Email amelia@example.com now"
    entities = [
        {
            "start": 6,
            "end": 24,
            "text": "amelia@example.com",
            "label": "EMAIL_ADDRESS",
            "score": 1.0,
        },
        {
            "start": 13,
            "end": 24,
            "text": "example.com",
            "label": "URL",
            "score": 0.5,
        },
    ]

    selected = _non_overlapping_entities(entities)

    assert [item["label"] for item in selected] == ["EMAIL_ADDRESS"]
    assert _redact(text, entities) == "Email [EMAIL_ADDRESS_1] now"


def test_specific_structured_entity_wins_over_generic_nlp_span() -> None:
    text = "TFN 123 456 782"
    entities = [
        {
            "start": 4,
            "end": 15,
            "text": "123 456 782",
            "label": "DATE_TIME",
            "score": 0.85,
        },
        {
            "start": 4,
            "end": 15,
            "text": "123 456 782",
            "label": "AU_TFN",
            "score": 1.0,
        },
    ]

    assert _redact(text, entities) == "TFN [AU_TFN_1]"
