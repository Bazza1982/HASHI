#!/usr/bin/env python3
"""Local PII detector smoke/benchmark probe.

This tool uses synthetic fixtures only. It intentionally does not make any
online model API call. Model downloads, when needed, are handled by the
selected local model library before inference starts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable


DEFAULT_GLINER_MODEL = "urchade/gliner_multi_pii-v1"
DEFAULT_LABELS = [
    "person",
    "address",
    "email",
    "phone number",
    "tax file number",
    "medicare number",
    "Australian business number",
    "Australian company number",
    "bank account number",
    "credit card number",
    "date of birth",
    "driver licence",
    "passport number",
    "password",
    "API key",
    "username",
    "IP address",
]


def _rss_mb() -> float | None:
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return None


def _normalize(value: str) -> str:
    return "".join(str(value).lower().split())


def _non_overlapping_entities(
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the strongest span when detectors return nested/duplicate hits."""
    preferred_prefixes = (
        "AU_",
        "EMAIL",
        "PHONE",
        "CREDIT_CARD",
        "BANK",
        "API",
        "PASSWORD",
        "PASSPORT",
        "PERSON",
        "ADDRESS",
    )

    def priority(item: dict[str, Any]) -> tuple[int, float, int]:
        label = str(item["label"]).upper()
        preferred = int(any(label.startswith(prefix) for prefix in preferred_prefixes))
        length = int(item["end"]) - int(item["start"])
        return preferred, float(item.get("score", 0.0)), length

    selected: list[dict[str, Any]] = []
    for entity in sorted(entities, key=priority, reverse=True):
        start = int(entity["start"])
        end = int(entity["end"])
        if any(
            start < int(existing["end"]) and end > int(existing["start"])
            for existing in selected
        ):
            continue
        selected.append(entity)
    return sorted(selected, key=lambda item: int(item["start"]))


def _redact(text: str, entities: list[dict[str, Any]]) -> str:
    redacted = text
    for index, entity in enumerate(
        reversed(_non_overlapping_entities(entities)),
        start=1,
    ):
        start = int(entity["start"])
        end = int(entity["end"])
        label = str(entity["label"]).upper().replace(" ", "_")
        redacted = redacted[:start] + f"[{label}_{index}]" + redacted[end:]
    return redacted


def _gliner_detector(model_name: str, threshold: float):
    from gliner import GLiNER

    model = GLiNER.from_pretrained(model_name)

    def detect(text: str) -> list[dict[str, Any]]:
        found = model.predict_entities(text, DEFAULT_LABELS, threshold=threshold)
        return [
            {
                "start": int(item["start"]),
                "end": int(item["end"]),
                "text": str(item["text"]),
                "label": str(item["label"]),
                "score": round(float(item.get("score", 0.0)), 6),
            }
            for item in found
        ]

    return detect


def _presidio_detector(_model_name: str, threshold: float):
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import (
        AuAbnRecognizer,
        AuAcnRecognizer,
        AuMedicareRecognizer,
        AuTfnRecognizer,
    )

    configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    }
    nlp_engine = NlpEngineProvider(
        nlp_configuration=configuration
    ).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    for recognizer_type in (
        AuAbnRecognizer,
        AuAcnRecognizer,
        AuMedicareRecognizer,
        AuTfnRecognizer,
    ):
        analyzer.registry.add_recognizer(recognizer_type())

    def detect(text: str) -> list[dict[str, Any]]:
        found = analyzer.analyze(
            text=text,
            language="en",
            score_threshold=threshold,
        )
        return [
            {
                "start": int(item.start),
                "end": int(item.end),
                "text": text[item.start : item.end],
                "label": str(item.entity_type),
                "score": round(float(item.score), 6),
            }
            for item in found
        ]

    return detect


def _load_cases(path: Path) -> list[dict[str, Any]]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list) or not loaded:
        raise ValueError("fixture must be a non-empty JSON list")
    return loaded


def _score_case(
    case: dict[str, Any],
    entities: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = list(case.get("expected") or [])
    unexpected = list(case.get("unexpected") or [])
    detected_text = [_normalize(item["text"]) for item in entities]

    hits = []
    misses = []
    for item in expected:
        value = _normalize(item["value"])
        matched = any(value in candidate or candidate in value for candidate in detected_text)
        (hits if matched else misses).append(item)

    false_hits = []
    for value in unexpected:
        normalized = _normalize(value)
        if any(normalized in candidate or candidate in normalized for candidate in detected_text):
            false_hits.append(value)

    return {
        "id": case["id"],
        "expected_count": len(expected),
        "hit_count": len(hits),
        "misses": misses,
        "known_false_hits": false_hits,
    }


def _run(
    detector_factory: Callable[[str, float], Callable[[str], list[dict[str, Any]]]],
    *,
    engine: str,
    model_name: str,
    threshold: float,
    repeats: int,
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    rss_before = _rss_mb()
    load_started = time.perf_counter()
    detect = detector_factory(model_name, threshold)
    load_seconds = time.perf_counter() - load_started
    rss_after_load = _rss_mb()

    results = []
    total_expected = 0
    total_hits = 0
    total_false_hits = 0
    all_latencies = []

    for case in cases:
        timings = []
        entities = []
        for _ in range(repeats):
            started = time.perf_counter()
            entities = detect(case["text"])
            timings.append((time.perf_counter() - started) * 1000)
        all_latencies.extend(timings)
        score = _score_case(case, entities)
        total_expected += score["expected_count"]
        total_hits += score["hit_count"]
        total_false_hits += len(score["known_false_hits"])
        results.append(
            {
                **score,
                "latency_ms": {
                    "first": round(timings[0], 2),
                    "median": round(statistics.median(timings), 2),
                },
                "entities": entities,
                "redacted": _redact(case["text"], entities),
            }
        )

    return {
        "engine": engine,
        "model": model_name if engine == "gliner" else "presidio+en_core_web_sm+AU",
        "threshold": threshold,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
        },
        "load_seconds": round(load_seconds, 2),
        "latency_ms": {
            "first": round(all_latencies[0], 2),
            "median_all": round(statistics.median(all_latencies), 2),
        },
        "memory_mb": {
            "before": rss_before,
            "after_load": rss_after_load,
            "after_run": _rss_mb(),
        },
        "summary": {
            "expected": total_expected,
            "detected": total_hits,
            "recall": round(total_hits / total_expected, 4) if total_expected else None,
            "known_false_hits": total_false_hits,
        },
        "cases": results,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=("presidio", "gliner"))
    parser.add_argument("--model", default=DEFAULT_GLINER_MODEL)
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "pii_probe_cases.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    factories = {
        "gliner": _gliner_detector,
        "presidio": _presidio_detector,
    }
    try:
        report = _run(
            factories[args.engine],
            engine=args.engine,
            model_name=args.model,
            threshold=args.threshold,
            repeats=max(1, args.repeats),
            cases=_load_cases(args.fixture),
        )
    except Exception as exc:
        report = {
            "engine": args.engine,
            "model": args.model,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
            },
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        return 1

    report["status"] = "passed"
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
