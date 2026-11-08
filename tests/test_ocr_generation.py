"""The native OCR child must travel with the qualified Function artifact."""

import shutil
import io
import json
import subprocess
import sys
from pathlib import Path

from orchestrator.function_generation import build_source_manifest


def test_qualified_ocr_artifact_can_launch_its_isolated_worker(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manifest = build_source_manifest(["tools.ocr"], code_root=root)
    artifact = tmp_path / "qualified"
    for item in (*manifest.entries, *manifest.assets):
        target = artifact / item.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / item.relative_path, target)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(artifact / "tools" / "ocr_worker.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--image" in result.stdout and "--model-root" in result.stdout


def test_worker_result_preserves_multilingual_text_with_an_ascii_stdout(
    tmp_path, monkeypatch
):
    from tools import ocr_worker

    text = "繁體中文 日本語 العربية"
    monkeypatch.setattr(ocr_worker, "run", lambda *args: {"status": "ok", "text": text})
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii", write_through=True)
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", stream)
        result = ocr_worker.main(
            [
                "--image",
                str(tmp_path / "image.png"),
                "--model-root",
                str(tmp_path / "models"),
                "--routes",
                "universal",
            ]
        )
        stream.flush()
        raw = buffer.getvalue().decode("ascii")
    assert result == 0
    assert json.loads(raw.removeprefix(ocr_worker.RESULT_PREFIX))["text"] == text
# HASHI-Fingerprint: hfp_2fed380b67ee47ad84ab2a4c3a12919c
