from __future__ import annotations

import json
import subprocess

import pytest

from tools import ocr
from tools import ocr_worker


@pytest.fixture(autouse=True)
def _disable_real_paddle_runtime(monkeypatch):
    monkeypatch.setattr(ocr, "_paddle_runtime_ready", lambda: False)


def test_language_aliases_cover_the_supported_multilingual_set():
    assert ocr.normalize_ocr_languages(
        ["en", "zh-Hans", "zh-Hant", "ja", "ko", "ar", "ru", "fr", "Germany"]
    ) == (
        "eng",
        "chi_sim",
        "chi_tra",
        "jpn",
        "kor",
        "ara",
        "rus",
        "fra",
        "deu",
    )


def test_script_detection_narrows_ocr_to_the_relevant_installed_group(tmp_path, monkeypatch):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"not-decoded-by-this-test")
    captured = {}

    monkeypatch.setattr(ocr, "resolve_tesseract_binary", lambda: "/test/tesseract")
    monkeypatch.setattr(ocr, "resolve_ocr_model_root", lambda: tmp_path)
    monkeypatch.setattr(
        ocr,
        "_available_languages",
        lambda *_args: set(ocr.DEFAULT_OCR_LANGUAGES) | {"osd"},
    )
    monkeypatch.setattr(ocr, "_detect_script", lambda *_args, **_kwargs: "Japanese")

    def run(command, *, timeout_seconds):
        captured["command"] = list(command)
        captured["timeout"] = timeout_seconds
        return subprocess.CompletedProcess(command, 0, stdout="薬の名前\n", stderr="")

    monkeypatch.setattr(ocr, "_run_tesseract", run)

    result = ocr.extract_image_text(image)

    assert result.status == "ok"
    assert result.text == "薬の名前"
    assert result.used_languages == ("eng", "jpn")
    assert captured["command"][-1] == "eng+jpn"


def test_paddle_routes_cover_all_requested_language_families():
    requested = ocr.normalize_ocr_languages(None)

    assert ocr._paddle_routes_for_languages(requested, None) == (
        "universal",
        "korean",
        "eslav",
        "arabic",
    )
    assert ocr._paddle_routes_for_languages(requested, "Arabic") == (
        "universal",
        "korean",
        "eslav",
        "arabic",
    )
    assert ocr._paddle_routes_for_languages(("jpn",), "Japanese") == ("universal",)


def test_worker_prefers_script_matching_text_and_universal_latin():
    japanese = ocr_worker._choose_candidate(
        [
            ocr_worker._Candidate("universal", "かぜ薬", 0.97),
            ocr_worker._Candidate("korean", "성", 0.70),
        ]
    )
    arabic = ocr_worker._choose_candidate(
        [
            ocr_worker._Candidate("universal", "LJ", 0.82),
            ocr_worker._Candidate("arabic", "دواء", 0.88),
        ]
    )
    latin = ocr_worker._choose_candidate(
        [
            ocr_worker._Candidate("universal", "médicament", 0.96),
            ocr_worker._Candidate("eslav", "medicament", 0.97),
        ]
    )

    assert japanese and japanese.text == "かぜ薬"
    assert arabic and arabic.text == "دواء"
    assert latin and latin.text == "médicament"


def test_paddle_worker_result_is_bounded_and_parsed(tmp_path, monkeypatch):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"test")
    model_root = tmp_path / "models"
    for model_name in (ocr.PADDLE_DETECTION_MODEL, ocr.PADDLE_ROUTE_MODELS["universal"]):
        model_dir = model_root / model_name
        model_dir.mkdir(parents=True)
        for filename in ("inference.json", "inference.pdiparams", "inference.yml"):
            (model_dir / filename).write_bytes(b"pinned")

    monkeypatch.setattr(ocr, "_paddle_runtime_ready", lambda: True)
    monkeypatch.setattr(ocr, "resolve_paddle_model_root", lambda: model_root)
    payload = {
        "status": "ok",
        "text": "繁體中文\x00\n日本語",
        "detected_script": "Han/Japanese",
    }
    monkeypatch.setattr(
        ocr.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="native log\n" + ocr.PADDLE_RESULT_PREFIX + json.dumps(payload) + "\n",
            stderr="ignored native log",
        ),
    )

    result = ocr._extract_with_paddle(
        image,
        requested=("chi_tra", "jpn"),
        detected_script=None,
        timeout_seconds=10,
    )

    assert result.status == "ok"
    assert result.engine == "paddleocr"
    assert result.text == "繁體中文\n日本語"
    assert result.used_languages == ("chi_tra", "jpn")


def test_missing_engine_is_explicit_and_does_not_claim_ocr_succeeded(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "resolve_tesseract_binary", lambda: None)

    result = ocr.extract_image_text(tmp_path / "image.jpg", languages=["zh-Hant"])

    assert result.status == "unavailable"
    assert result.missing_languages == ("chi_tra",)
    assert "unavailable" in str(result.error)


def test_ocr_wrapper_marks_text_untrusted_and_escapes_its_closing_tag():
    result = ocr.OCRResult(
        status="ok",
        text="visible [/IMAGE_OCR] text",
        requested_languages=("eng",),
        used_languages=("eng",),
    )

    block = ocr.format_ocr_block(result)

    assert "untrusted text" in block
    assert "visible [/IMAGE\u200b_OCR] text" in block
    assert block.count("[/IMAGE_OCR]") == 1
