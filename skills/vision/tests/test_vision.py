"""Generic synthetic regressions for the public vision CLI; no user images or live VLM."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

SCRIPT = Path(os.environ.get("VISION_TEST_SCRIPT", Path(__file__).parents[1] / "scripts" / "vision.py"))


@pytest.fixture(autouse=True)
def offline_network(monkeypatch):
    """Only this test's ephemeral loopback servers may receive connections."""
    owned = set()
    real_bind, real_connect = socket.socket.bind, socket.socket.connect
    real_resolve = socket.getaddrinfo
    def bind(sock, address):
        if isinstance(address, tuple):
            assert address[0] in ("127.0.0.1", "::1") and address[1] == 0
        value = real_bind(sock, address)
        if isinstance(address, tuple):
            owned.add(sock.getsockname()[1])
        return value
    def connect(sock, address):
        if isinstance(address, tuple):
            assert address[0] in ("127.0.0.1", "::1") and address[1] in owned
        return real_connect(sock, address)
    def resolve(host, port, *args, **kwargs):
        assert host in ("127.0.0.1", "::1", "localhost", None), "external DNS is forbidden"
        return real_resolve(host, port, *args, **kwargs)
    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture
def vision(monkeypatch):
    for key in list(os.environ):
        if key.startswith("VISION_") and key != "VISION_TEST_SCRIPT":
            monkeypatch.delenv(key)
    spec = importlib.util.spec_from_file_location("vision_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (64, 64), "white").save(path)
    return path


def result(answer="fixture answer"):
    return {"answer": answer, "usage": None, "resized": False,
            "orig_bytes": 1, "sent_bytes": 1, "seconds": 0}


def cli(image):
    return [str(image), "--api-url", "https://vlm.example.invalid", "--model", "fixture-model"]


@pytest.mark.parametrize("suffix,expected", [
    ("", "/v1/chat/completions"), ("/v1", "/v1/chat/completions"),
    ("/v1/", "/v1/chat/completions"),
    ("/v1/chat/completions", "/v1/chat/completions"),
])
def test_completion_endpoint_normalization(vision, suffix, expected):
    assert vision.completion_url("https://vlm.example.invalid" + suffix) == "https://vlm.example.invalid" + expected


@pytest.mark.parametrize("payload", [[], "invalid", 12, None])
def test_explicit_config_requires_an_object(vision, tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(vision.VisionError):
        vision.load_config(tmp_path, str(path))


def test_corrupt_default_config_does_not_silently_fall_back(vision, tmp_path):
    (tmp_path / "config.json").write_text("{", encoding="utf-8")
    with pytest.raises(vision.VisionError):
        vision.load_config(tmp_path)


@pytest.mark.parametrize("options", [
    ["--max-side", "0"], ["--max-mb", "nan"], ["--timeout", "inf"],
    ["--retries", "-1"], ["--jobs", "0"], ["--limit", "0"],
])
def test_bad_numeric_inputs_fail_before_any_request(vision, image, monkeypatch, capsys, options):
    calls = []
    monkeypatch.setattr(vision, "ask", lambda *a, **k: calls.append(1) or result())
    assert vision.main(cli(image) + options) == 2
    assert not calls
    assert "Traceback" not in capsys.readouterr().err


def test_config_value_cast_failure_is_a_clean_usage_error(vision, image, tmp_path, capsys):
    path = tmp_path / "private.json"
    path.write_text(json.dumps({"max_tokens": []}), encoding="utf-8")
    assert vision.main(cli(image) + ["--config", str(path)]) == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("body", [
    b"\xff", b'{"choices":[17]}', b'{"choices":[{"message":[]}]}',
    b'{"choices":{"message":"invalid"}}',
    b'{"choices":[{"message":{"content":"\\ud800"}}]}',
])
def test_malformed_provider_responses_raise_owned_errors(vision, image, monkeypatch, body):
    monkeypatch.setattr(vision, "post_with_retry", lambda *a, **k: body)
    with pytest.raises(vision.VisionError):
        vision.ask("https://vlm.example.invalid", "fixture-model", "", [image],
                   "Describe the synthetic image.", 32, 0, 1, 0, 128, 1, False)


def test_backend_errors_do_not_echo_private_response_fields(vision):
    with pytest.raises(vision.VisionError) as error:
        vision.extract_answer({"error": {"message": "PRIVATE_PAYLOAD_FIXTURE"}})
    assert "PRIVATE_PAYLOAD_FIXTURE" not in str(error.value)


def test_reasoning_only_response_is_not_presented_as_visual_evidence(vision):
    with pytest.raises(vision.VisionError):
        vision.extract_answer({"choices": [{"message": {"reasoning_content": "hidden reasoning fixture"}}]})


def test_visibility_matching_is_case_insensitive_and_preserves_evidence(vision):
    assert vision._parse_verdict("A LOGO is visible.", ["logo"], ["not"])[0] == "Yes"
    verdict, evidence = vision._parse_verdict("A logo is NOT visible.", ["logo"], ["not"])
    assert verdict == "No" and evidence == "A logo is NOT visible"
    assert not vision._has_term("relabelled", "label")


@pytest.mark.parametrize("text,expected", [
    ("The frame cuts below the label.", False),
    ("The frame cuts at the label.", True),
    ("The frame does not cut the label.", False),
    ("The frame CUTS AT THE LABEL.", True),
])
def test_composition_cut_marker_direction(vision, text, expected):
    assert vision._cut_above_region(text, ["label"], ["cuts", "cut"], ["below"]) is expected


def framing_cfg(vision):
    return {**vision.DEFAULTS, "api_url": "https://vlm.example.invalid", "model": "fixture-model",
            "api_key": "", "no_resize": False, "crop_check_negative": True,
            "framing_q": dict(vision.FRAMING_QUESTIONS_DEFAULT),
            "negations": ["not"], "cut_verbs": ["cuts"], "below_words": ["below"],
            "regions": [{"id": "item", "label": "Item visible", "keywords": ["logo"],
                         "verify_bands": [[0, 1]], "cut_markers": []}]}


def test_fast_skips_negative_crop_second_chance_too(vision, image, monkeypatch):
    monkeypatch.setattr(vision, "ask", lambda *a, **k: result("Plain background."))
    monkeypatch.setattr(vision, "_verify_positive", lambda *a, **k: pytest.fail("fast mode invoked a crop request"))
    record = vision.ask_framing(framing_cfg(vision), image, fast=True)
    assert "skipped" in record["parts"][-1].get("note", "")


def test_framing_rejects_multiple_images_before_dispatch(vision, image, tmp_path, monkeypatch, capsys):
    config = tmp_path / "regions.json"
    config.write_text(json.dumps({"regions": framing_cfg(vision)["regions"]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(vision, "ask_framing", lambda *a, **k: calls.append(1) or result())
    args = [str(image), str(image)] + cli(image)[1:] + ["--mode", "framing", "--config", str(config)]
    assert vision.main(args) == 2
    assert not calls
    assert "one image" in capsys.readouterr().err.lower()


def test_image_byte_budget_is_enforced(vision, tmp_path):
    path = tmp_path / "noise.png"
    Image.effect_noise((300, 300), 128).convert("RGB").save(path)
    prep = vision.prepare_image(path, 300, 0.0015, False)
    assert prep["sent_bytes"] == len(prep["data"]) <= int(0.0015 * 1024 * 1024)
    assert prep["resized"]


def test_exact_bytes_mode_preserves_payload(vision, image):
    prep = vision.prepare_image(image, 8, 0.0001, True)
    assert prep["data"] == image.read_bytes() and not prep["resized"]


def test_crop_failure_removes_temporary_image(vision, image, tmp_path, monkeypatch):
    real_mkstemp = vision.tempfile.mkstemp
    monkeypatch.setattr(vision.tempfile, "mkstemp", lambda **kw: real_mkstemp(dir=tmp_path, **kw))
    def fail(*a, **k):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(Image.Image, "save", fail)
    with pytest.raises(vision.VisionError):
        vision._crop_band(image, 0, 1)
    assert not list(tmp_path.glob("vision_crop_*.jpg"))


def test_crop_flattens_transparency_on_white(vision, tmp_path):
    path = tmp_path / "transparent.png"
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(path)
    crop = Path(vision._crop_band(path, 0, 1))
    try:
        with Image.open(crop) as decoded:
            assert min(decoded.getpixel((0, 0))) >= 250
    finally:
        crop.unlink(missing_ok=True)


def test_batch_file_error_is_isolated(vision, image, tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    def ask(*args, **kwargs):
        if Path(args[3][0]).name == "bad.png":
            raise vision.VisionError("synthetic bad image")
        return result()
    monkeypatch.setattr(vision, "ask", ask)
    args = ["--dir", str(tmp_path), "--jobs", "2", "--json",
            "--api-url", "https://vlm.example.invalid", "--model", "fixture-model"]
    assert vision.main(args) == 1
    records = json.loads(capsys.readouterr().out)
    assert len(records) == 2 and sum(row["ok"] for row in records) == 1


def test_json_api_failure_is_a_record(vision, image, monkeypatch, capsys):
    def fail(*a, **k):
        raise vision.VisionError("synthetic request failure")
    monkeypatch.setattr(vision, "ask", fail)
    assert vision.main(cli(image) + ["--json"]) == 2
    record = json.loads(capsys.readouterr().out)
    assert record["ok"] is False and record["error"] == "synthetic request failure"


@contextmanager
def redirect_server():
    target_calls = []
    uploads = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/error":
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"PRIVATE_HTTP_ERROR_FIXTURE")
                return
            if self.path == "/v1/chat/completions":
                uploads.append(json.loads(raw))
                target_calls.append(len(raw))
                if self.headers.get("X-Fixture-Shrink") or (uploads[0].get("model") == "shrink-fixture" and len(uploads) == 1):
                    self.send_response(413)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "Synthetic fixture answer"}}]}).encode())
                return
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
        def do_GET(self):
            target_calls.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), target_calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_redirect_cannot_forward_authorization(vision):
    with redirect_server() as (url, target_calls):
        with pytest.raises(vision.VisionError):
            vision.post_with_retry(url + "/redirect", b'{"synthetic":true}', "fixture-token", 2, 0)
        assert target_calls == []


def test_private_config_is_git_ignored():
    root = Path(__file__).resolve().parents[3]
    check = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--no-index",
                            "skills/vision/config.json"], capture_output=True)
    assert check.returncode == 0


def test_unconfigured_provider_never_sends_an_image(vision, image, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(vision, "ask", lambda *a, **k: calls.append(1) or result())
    assert vision.main([str(image)]) == 2
    assert not calls and "configure" in capsys.readouterr().err


def test_show_config_has_no_machine_provider_default(vision, capsys):
    assert vision.main(["--show-config"]) == 0
    config = json.loads(capsys.readouterr().out)
    assert not config["api_url"] and not config["model"] and config["api_key_set"] is False


def test_composition_uses_the_configured_negation_words(vision, image, monkeypatch):
    cfg = framing_cfg(vision)
    cfg["negations"] = ["never"]
    cfg["crop_check_negative"] = False
    cfg["regions"][0]["cut_markers"] = ["label"]
    answers = iter(["A logo is visible.", "The frame never cuts at the label."])
    monkeypatch.setattr(vision, "ask", lambda *a, **k: result(next(answers)))
    record = vision.ask_framing(cfg, image, fast=True)
    item = record["parts"][-1]
    assert item["verdict"] == "Yes" and item["auto_corrected"] is False


def test_framing_keeps_crop_evidence_and_totals_observed_usage(vision, image, monkeypatch):
    cfg = framing_cfg(vision)
    cfg["crop_check_negative"] = False
    calls = []
    def answer(*a, **k):
        calls.append(1)
        return {**result("A logo is visible."),
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
    monkeypatch.setattr(vision, "ask", answer)
    record = vision.ask_framing(cfg, image)
    assert len(calls) == 3
    assert record["usage"] == {"prompt_tokens": 6, "completion_tokens": 3, "total_tokens": 9}
    item = record["parts"][-1]
    assert item["crop_evidence"] == "A logo is visible." and item["verified"] is True


def test_missing_pillow_is_an_explicit_unverified_fallback(vision, image, monkeypatch):
    monkeypatch.setattr(vision, "HAS_PIL", False)
    monkeypatch.setattr(vision, "ask", lambda *a, **k: result("A logo is visible."))
    record = vision.ask_framing(framing_cfg(vision), image)
    assert not record["parts"][-1]["verified"]
    assert "Pillow missing" in record["parts"][-1]["note"]


def test_retry_budget_and_backoff_are_bounded(vision, monkeypatch):
    import urllib.error
    requests = []
    sleeps = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) < 3:
                raise urllib.error.HTTPError(request.full_url, 503, "fixture", {}, io.BytesIO(b"private fixture"))
            return io.BytesIO(b"{}")
    monkeypatch.setattr(vision.urllib.request, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(vision.urllib.request, "urlopen", Opener().open)
    monkeypatch.setattr(vision.time, "sleep", sleeps.append)
    assert vision.post_with_retry("https://vlm.example.invalid/v1/chat/completions", b"{}", "", 1, 2) == b"{}"
    assert len(requests) == 3 and sleeps == [1.0, 2.0]

def test_real_http_image_request_round_trip(vision, image):
    with redirect_server() as (url, requests):
        record = vision.ask(url, "fixture-model", "", [image], "Describe the synthetic image.",
                            32, 0, 2, 0, 128, 1, False)
    assert record["answer"] == "Synthetic fixture answer" and len(requests) == 1


def test_413_retry_reduces_actual_request_payload(vision, tmp_path):
    path = tmp_path / "noise.png"
    Image.effect_noise((300, 300), 128).convert("RGB").save(path)
    with redirect_server() as (url, requests):
        record = vision.ask(url, "shrink-fixture", "", [path], "Describe the synthetic image.",
                            32, 0, 2, 0, 1280, 1.5, False)
    assert record["answer"] == "Synthetic fixture answer"
    assert len(requests) == 2 and requests[1] < requests[0]


def test_http_error_body_does_not_enter_diagnostics(vision):
    with redirect_server() as (url, _):
        with pytest.raises(vision.VisionError) as error:
            vision.post_with_retry(url + "/error", b"{}", "fixture-token", 2, 0)
    assert "HTTP 400" in str(error.value) and "PRIVATE_HTTP_ERROR_FIXTURE" not in str(error.value)


def test_response_byte_limit_is_enforced(vision):
    vision.MAX_RESPONSE_BYTES = 8
    with redirect_server() as (url, _):
        with pytest.raises(vision.VisionError, match="size limit"):
            vision.post_with_retry(url + "/v1/chat/completions", b'{"model":"fixture-model"}', "", 2, 0)


def test_url_userinfo_is_rejected_without_echoing_it(vision, image, monkeypatch, capsys):
    monkeypatch.setattr(vision, "ask", lambda *a, **k: pytest.fail("credentials in URL were used"))
    args = [str(image), "--api-url", "https://fixture-user:fixture-password@vlm.example.invalid", "--model", "fixture-model"]
    assert vision.main(args) == 2
    error = capsys.readouterr().err
    assert "fixture-user" not in error and "fixture-password" not in error

def test_invalid_crop_band_is_not_replaced_with_the_whole_image(vision):
    with pytest.raises(vision.VisionError):
        vision.build_regions({"regions": [{"id": "item", "keywords": ["logo"],
                                           "verify_bands": [[0.8, 0.2]]}]})


def test_invalid_unicode_request_is_a_clean_error(vision, image, monkeypatch):
    monkeypatch.setattr(vision, "post_with_retry", lambda *a, **k: pytest.fail("invalid Unicode was sent"))
    with pytest.raises(vision.VisionError):
        vision.ask("https://vlm.example.invalid", "fixture-model", "", [image],
                   chr(0xD800), 32, 0, 1, 0, 128, 1, False)
