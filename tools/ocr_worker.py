"""Isolated PaddleOCR worker used by :mod:`tools.ocr`.

Paddle's native runtime is intentionally kept outside the long-lived HASHI
process.  The parent captures this process's logs and consumes only the final
sentinel-prefixed JSON record.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


RESULT_PREFIX = "HASHI_OCR_RESULT="
MIN_RECOGNITION_SCORE = 0.55

ROUTE_MODELS = {
    "universal": "PP-OCRv6_medium_rec",
    "korean": "korean_PP-OCRv5_mobile_rec",
    "eslav": "eslav_PP-OCRv5_mobile_rec",
    "arabic": "arabic_PP-OCRv5_mobile_rec",
}
DETECTION_MODEL = "PP-OCRv6_medium_det"


@dataclass(frozen=True)
class _Candidate:
    route: str
    text: str
    score: float


def _contains_range(text: str, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start <= ord(char) <= end for char in text for start, end in ranges)


def _text_script(text: str) -> str | None:
    if _contains_range(text, ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF))):
        return "arabic"
    if _contains_range(text, ((0x0400, 0x052F),)):
        return "eslav"
    if _contains_range(text, ((0xAC00, 0xD7AF), (0x1100, 0x11FF))):
        return "korean"
    if _contains_range(
        text,
        (
            (0x3040, 0x30FF),
            (0x3400, 0x4DBF),
            (0x4E00, 0x9FFF),
            (0xF900, 0xFAFF),
        ),
    ):
        return "universal"
    return None


def _candidate_rank(candidate: _Candidate) -> tuple[float, int]:
    script = _text_script(candidate.text)
    script_bonus = 0.16 if script == candidate.route else 0.0
    universal_latin_bonus = 0.04 if script is None and candidate.route == "universal" else 0.0
    route_order = {"universal": 3, "korean": 2, "eslav": 1, "arabic": 0}
    return candidate.score + script_bonus + universal_latin_bonus, route_order[candidate.route]


def _choose_candidate(candidates: Iterable[_Candidate]) -> _Candidate | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.text.strip() and candidate.score >= MIN_RECOGNITION_SCORE
    ]
    return max(eligible, key=_candidate_rank) if eligible else None


def _result_payload(value: Any) -> dict[str, Any]:
    serialized = value.json if hasattr(value, "json") else value
    if callable(serialized):
        serialized = serialized()
    if isinstance(serialized, dict) and isinstance(serialized.get("res"), dict):
        return dict(serialized["res"])
    if isinstance(serialized, dict):
        return dict(serialized)
    raise TypeError("PaddleOCR returned an unsupported result shape")


def _recognition_value(value: Any, key: str, default: Any) -> Any:
    try:
        return value[key]
    except (KeyError, TypeError):
        payload = _result_payload(value)
        return payload.get(key, default)


def _normalize_recognition_text(value: Any) -> str:
    if isinstance(value, (tuple, list)):
        value = value[0] if value else ""
    return str(value or "").replace("\x00", "").strip()


def _load_recognizer(model_root: Path, route: str):
    from paddlex import create_model

    model_name = ROUTE_MODELS[route]
    return create_model(
        model_name,
        model_dir=str(model_root / model_name),
        device="cpu",
        batch_size=8,
        engine_config={"enable_mkldnn": False},
    )


def run(image_path: Path, model_root: Path, routes: tuple[str, ...]) -> dict[str, Any]:
    import cv2
    from paddleocr import PaddleOCR

    universal_name = ROUTE_MODELS["universal"]
    base = PaddleOCR(
        text_detection_model_name=DETECTION_MODEL,
        text_detection_model_dir=str(model_root / DETECTION_MODEL),
        text_recognition_model_name=universal_name,
        text_recognition_model_dir=str(model_root / universal_name),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
        device="cpu",
    )
    try:
        base_results = base.predict(str(image_path), text_rec_score_thresh=0.0)
        if not base_results:
            return {"status": "empty", "text": "", "detected_script": None}
        payload = _result_payload(base_results[0])
        polygons = list(payload.get("dt_polys") or [])
        universal_texts = list(payload.get("rec_texts") or [])
        universal_scores = list(payload.get("rec_scores") or [])
        if len(universal_texts) != len(polygons) or len(universal_scores) != len(polygons):
            raise RuntimeError("PaddleOCR detection and recognition counts differ")

        candidates: list[list[_Candidate]] = [[] for _ in polygons]
        if "universal" in routes:
            for index, (text, score) in enumerate(zip(universal_texts, universal_scores)):
                candidates[index].append(
                    _Candidate("universal", _normalize_recognition_text(text), float(score))
                )

        extra_routes = tuple(route for route in routes if route != "universal")
        if extra_routes and polygons:
            image = cv2.imread(str(image_path))
            if image is None:
                raise ValueError("OpenCV could not decode the OCR image")
            crops = list(base.paddlex_pipeline._crop_by_polys(image, polygons))
            if len(crops) != len(polygons):
                raise RuntimeError("PaddleOCR crop count differs from detection count")
            for route in extra_routes:
                recognizer = _load_recognizer(model_root, route)
                results = list(
                    recognizer.predict(crops, batch_size=8, return_word_box=False)
                )
                if len(results) != len(polygons):
                    raise RuntimeError(f"{route} recognition count differs from detection count")
                for index, result in enumerate(results):
                    candidates[index].append(
                        _Candidate(
                            route,
                            _normalize_recognition_text(
                                _recognition_value(result, "rec_text", "")
                            ),
                            float(_recognition_value(result, "rec_score", 0.0)),
                        )
                    )

        chosen = [candidate for group in candidates if (candidate := _choose_candidate(group))]
        scripts = {_text_script(candidate.text) for candidate in chosen}
        scripts.discard(None)
        script_names = {
            "universal": "Han/Japanese",
            "korean": "Korean",
            "eslav": "Cyrillic",
            "arabic": "Arabic",
        }
        detected_script = (
            script_names[next(iter(scripts))]
            if len(scripts) == 1
            else ("Mixed" if scripts else None)
        )
        text = "\n".join(candidate.text for candidate in chosen)
        return {
            "status": "ok" if text else "empty",
            "text": text,
            "detected_script": detected_script,
        }
    finally:
        base.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--routes", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    routes = tuple(item.strip() for item in args.routes.split(",") if item.strip())
    unknown = sorted(set(routes).difference(ROUTE_MODELS))
    if not routes or unknown:
        payload = {"status": "error", "error": "invalid OCR model route"}
        print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False))
        return 2
    try:
        payload = run(args.image.resolve(), args.model_root.resolve(), routes)
        return_code = 0
    except Exception as exc:
        payload = {
            "status": "error",
            "error": f"PaddleOCR worker failed: {type(exc).__name__}",
        }
        return_code = 1
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False))
    return return_code


if __name__ == "__main__":
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    raise SystemExit(main())
