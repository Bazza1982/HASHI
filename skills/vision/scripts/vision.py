# -*- coding: utf-8 -*-
"""Configurable OpenAI-compatible visual inspection CLI.

Provider URL/model and project-specific prompts/regions are supplied explicitly
through CLI flags, environment variables or a private configuration file.
Outputs preserve model evidence and uncertainty; heuristic framing checks do not
establish ground truth. Uses the standard library, with optional Pillow.
Python 3.8 or newer.
"""
import argparse
import base64
import io
import http.client
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:  # optional dependency: enables auto-resize / re-encode / crop verification
    from PIL import Image

    HAS_PIL = True
except Exception:  # pragma: no cover
    HAS_PIL = False

VERSION = "3.1.1"

DEFAULT_QUESTION = (
    "Describe the visible image content objectively: image type, subjects or "
    "objects, their spatial relationships, background, framing, colors and "
    "lighting, and any legible text. Distinguish observation from inference. "
    "State uncertainty and do not invent details or identities."
)

MODE_QUESTIONS = {
    "ocr": (
        "Transcribe every text in the image verbatim, in reading order (top to "
        "bottom, left to right). Do not translate, do not explain, do not add "
        "anything else. If the image contains no text at all, answer exactly: "
        "NO TEXT"
    ),
    "framing": (
        "Framing is handled by the region-visibility pipeline (see "
        "FRAMING_QUESTIONS_DEFAULT); this entry only registers the mode name."
    ),
    "describe": DEFAULT_QUESTION,
}

# Open prompts feed configurable, heuristic evidence parsing and crop checks.
FRAMING_QUESTIONS_DEFAULT = {
    "listing": (
        "List exactly what is actually visible in this image, from top to "
        "bottom. List only items that are drawn or photographed inside the "
        "frame; do not list items that are cut off by the frame edge; do not "
        "guess, do not infer, do not comment."
    ),
    "composition": (
        "Briefly answer three points, each on one short line:\n"
        "1) The main subject of this image;\n"
        "2) Where the bottom edge of the frame cuts the subject, if at all "
        "(for example: 'at the label', 'below the object', 'does not cut');\n"
        "3) Which parts of the subject are cut off by a frame edge or are "
        "outside the frame."
    ),
    "crop": (
        "This is a zoomed-in crop from a larger image. List exactly what is "
        "actually visible in this crop, from top to bottom. List only what is "
        "drawn in the image; do not guess or infer."
    ),
}

# Words that flip a matched keyword clause to a No verdict. ASCII words are
# matched on word boundaries ("no" never matches inside "nose"); non-ASCII
# words are matched as substrings. Overridable per config / language.
DEFAULT_NEGATIONS = [
    "not", "no", "none", "absent", "missing", "without",
    "cannot see", "can't see", "cut off", "cropped", "out of frame",
    "not visible", "unclear", "cannot tell",
]

# Verbs that introduce a cut-position statement in the composition answer.
DEFAULT_CUT_VERBS = ["cuts", "cut", "cropped", "ends at"]

# Words that say the cut is *below* a marker (so the region may still be
# visible and no auto-correction should fire).
DEFAULT_BELOW_WORDS = ["below", "under", "beneath", "lower than"]

# Regions are project-specific and must be configured for framing mode.
DEFAULT_REGIONS = []


MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

DEFAULTS = {
    "api_url": "",
    "model": "",
    "max_tokens": 512,
    "temperature": 0.2,
    "timeout_s": 300,
    "retries": 2,
    "max_side": 1280,
    "max_mb": 1.5,
}


MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class VisionError(Exception):
    """User-facing error; message is printed as one clean line, never a traceback."""


class HTTP413Error(Exception):
    pass


def eprint(msg):
    print(msg, file=sys.stderr)



def load_config(skill_root, cli_path=None, env_path=None):
    """Load one requested/private config; an absent default is unconfigured."""
    explicit = cli_path or env_path
    path = Path(explicit) if explicit else Path(skill_root) / "config.json"
    if not path.is_file():
        if explicit or path.exists():
            raise VisionError("config file not found or not a regular file")
        return {}, path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise VisionError("config file is not readable UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise VisionError("config must contain a JSON object")
    return data, path



def completion_url(api_url):
    """Accept a base, a /v1 base, or an exact completion endpoint."""
    if not isinstance(api_url, str) or not api_url.strip():
        raise VisionError("configure an API URL before sending images")
    url = api_url.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and not any(c.isspace() for c in url)
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise VisionError("API URL must be HTTP(S), without credentials, query or fragment")
    if url.endswith("/chat/completions"):
        return url
    return url + ("/chat/completions" if parsed.path.rstrip("/").endswith("/v1")
                  else "/v1/chat/completions")


def _pack(name, mime, data, orig_bytes, sent_bytes, resized):
    return {
        "name": name,
        "mime": mime,
        "data": data,
        "orig_bytes": orig_bytes,
        "sent_bytes": sent_bytes,
        "resized": resized,
    }



def _to_rgb(frame):
    if frame.mode == "RGB":
        return frame.copy()
    rgba = frame.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, (255, 255, 255))
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def prepare_image(path, max_side, max_mb, no_resize):
    """Read a bounded image; re-encode and shrink until the byte budget fits."""
    p = Path(path)
    if p.suffix.lower() not in MIME_BY_SUFFIX:
        raise VisionError("unsupported image extension")
    try:
        with p.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise VisionError("cannot read image") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise VisionError("image exceeds the input size limit")
    orig = len(raw)
    mime = MIME_BY_SUFFIX[p.suffix.lower()]
    limit = int(max_mb * 1024 * 1024)
    if no_resize:
        return _pack(p.name, mime, raw, orig, orig, False)
    if not HAS_PIL:
        if orig > limit:
            eprint("warning: Pillow unavailable; image sent unchanged, byte budget not enforced")
        return _pack(p.name, mime, raw, orig, orig, False)
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            longest = max(im.size)
            if longest <= max_side and orig <= limit:
                return _pack(p.name, mime, raw, orig, orig, False)
            frame = _to_rgb(im)
            if longest > max_side:
                scale = max_side / longest
                frame = frame.resize((max(1, round(frame.width * scale)),
                                      max(1, round(frame.height * scale))), Image.LANCZOS)
            for _ in range(24):
                for quality in (90, 80, 70, 60):
                    buf = io.BytesIO()
                    frame.save(buf, "JPEG", quality=quality)
                    data = buf.getvalue()
                    if len(data) <= limit:
                        return _pack(p.name, "image/jpeg", data, orig, len(data), True)
                size = (max(1, int(frame.width * 0.75)),
                        max(1, int(frame.height * 0.75)))
                if size == frame.size:
                    break
                frame = frame.resize(size, Image.LANCZOS)
        raise VisionError("image cannot fit the configured byte budget")
    except VisionError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError):
        raise VisionError("cannot decode or encode image") from None


def data_uri_from_prep(prep):
    return f"data:{prep['mime']};base64,{base64.b64encode(prep['data']).decode('ascii')}"



def _safe_read(http_error):
    """Read only enough to identify a known protocol code; never echo the body."""
    try:
        return http_error.read(8192).decode("utf-8", "replace")
    except Exception:
        return ""
    finally:
        http_error.close()






class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_with_retry(url, body, api_key, timeout_s, retries):
    """Bounded POST with redirects disabled and sanitized failures."""
    opener = urllib.request.build_opener(_NoRedirect())
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if api_key:
            req.add_header("Authorization", "Bearer " + api_key)
        try:
            with opener.open(req, timeout=timeout_s) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise VisionError("backend response exceeds the size limit")
                return data
        except urllib.error.HTTPError as exc:
            code = exc.code
            error_body = _safe_read(exc)
            if code == 413:
                raise HTTP413Error("HTTP 413") from None
            if code == 400 and "one_image_and_text_required" in error_body:
                raise VisionError("backend accepts one image per request (one_image_and_text_required)") from None
            if code == 400 and "max_tokens_invalid" in error_body:
                raise VisionError("backend rejected max_tokens; configure a supported value (max_tokens_invalid)") from None
            if code >= 500 and attempt < retries:
                eprint("warning: backend unavailable; retry {}/{}".format(attempt + 1, retries))
                time.sleep(delay)
                delay *= 2
                continue
            raise VisionError("HTTP {} from backend".format(code)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError, http.client.HTTPException):
            if attempt < retries:
                eprint("warning: connection failed; retry {}/{}".format(attempt + 1, retries))
                time.sleep(delay)
                delay *= 2
                continue
            raise VisionError("cannot reach backend") from None
    raise VisionError("request retry budget exhausted")



def extract_answer(data):
    if not isinstance(data, dict):
        raise VisionError("backend response must be a JSON object")
    if data.get("error"):
        raise VisionError("backend returned an error object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise VisionError("backend response has no valid choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise VisionError("backend response has no valid message")
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(part["text"] for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                            and isinstance(part.get("text"), str)).strip()
    if not isinstance(content, str) or not content.strip():
        raise VisionError("backend returned an empty answer")
    try:
        content.encode("utf-8")
    except UnicodeError:
        raise VisionError("backend returned invalid Unicode text") from None
    return content.strip()


def ask(api_url, model, api_key, images, question, max_tokens, temperature,
        timeout_s, retries, max_side, max_mb, no_resize):
    """One request: send all images + the question, return a result dict."""
    url = completion_url(api_url)
    t0 = time.monotonic()
    side = max_side
    budget_mb = max_mb
    for attempt in range(3):  # original send + up to two 413 shrink retries
        preps = [prepare_image(p, side, budget_mb, no_resize) for p in images]
        content = []
        if question:
            content.append({"type": "text", "text": question})
        for pr in preps:
            content.append({"type": "image_url", "image_url": {"url": data_uri_from_prep(pr)}})
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            raise VisionError("request text is not valid Unicode") from None
        try:
            raw = post_with_retry(url, body, api_key, timeout_s, retries)
            data = json.loads(raw.decode("utf-8"))
        except HTTP413Error:
            if no_resize or not HAS_PIL or attempt == 2:
                raise VisionError(
                    "backend rejected the image: payload too large (HTTP 413). "
                    "Let auto-resize run (install Pillow) or pre-compress the image."
                )
            side = max(1, int(side * 0.8))
            # Lower the actual largest image budget, even below the side cap.
            largest = max(prep["sent_bytes"] for prep in preps)
            budget_mb = min(budget_mb, max(1, int(largest * 0.6)) / 1048576)
            eprint("warning: backend returned 413; retrying with a smaller image budget")
            continue
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise VisionError("backend response is not JSON")
        answer = extract_answer(data)
        return {
            "answer": answer,
            "usage": data.get("usage"),
            "resized": any(pr["resized"] for pr in preps),
            "orig_bytes": sum(pr["orig_bytes"] for pr in preps),
            "sent_bytes": sum(pr["sent_bytes"] for pr in preps),
            "seconds": round(time.monotonic() - t0, 2),
        }
    raise VisionError("failed to deliver image to backend after resizing")



def _has_term(text, term):
    """Case-insensitive tokens for ASCII; substrings for non-ASCII terms."""
    text, term = text.casefold(), term.casefold()
    if not term:
        return False
    if term.isascii():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text) is not None
    return term in text


def _parse_verdict(answer, keywords, negations):
    """Derive a deterministic Yes/No verdict from an open listing answer.

    Splits the answer into clauses, finds the last clause that mentions any
    positive keyword for the region, and returns No when that clause is also
    negated. Returns (verdict, evidence) where evidence is that clause.
    """
    verdict = "No"
    evidence = ""
    for clause in re.split(r"[,;，；。.\n\r]+", answer):
        for kw in keywords:
            if _has_term(clause, kw):
                negated = any(_has_term(clause, n) for n in negations)
                verdict = "No" if negated else "Yes"
                evidence = clause.strip()
                break
    return verdict, evidence


def _is_yes(verdict):
    return str(verdict).strip().lower().rstrip(".") in ("yes", "true")



def _cut_phrases(comp_text, cut_verbs, negations=None):
    """Extract bounded cut-position phrases without matching word fragments."""
    text = comp_text.casefold()
    phrases = []
    for verb in cut_verbs:
        word = verb.casefold()
        pattern = re.escape(word)
        if word.isascii():
            pattern = r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])"
        for match in re.finditer(pattern, text):
            before = text[max(0, match.start() - 12):match.start()]
            if any(_has_term(before, word) for word in (DEFAULT_NEGATIONS if negations is None else negations)):
                continue
            phrase = re.split(r"[;,\n)）.。]", text[match.start():match.start() + 80], maxsplit=1)[0]
            if phrase and phrase not in phrases:
                phrases.append(phrase)
    return phrases



def _cut_above_region(comp_text, markers, cut_verbs, below_words, negations=None):
    if not markers:
        return False
    for phrase in _cut_phrases(comp_text, cut_verbs, negations):
        for marker in markers:
            marker = marker.casefold()
            if not _has_term(phrase, marker):
                continue
            # In "cuts below the label", the direction precedes the marker.
            prefix = phrase[:phrase.find(marker)]
            if any(_has_term(prefix, word) for word in below_words):
                continue
            return True
    return False


def _apply_consistency(region, verdict, comp_text, cut_verbs, below_words, negations=None):
    """Deterministic self-consistency fix for framing verdicts.

    If the composition answer says the frame edge sits above the region the
    verdict claims to see, the verdict contradicts itself and is corrected to
    No, with a marker. Returns (verdict, note).
    """
    if not _is_yes(verdict):
        return verdict, ""
    if _cut_above_region(comp_text, region.get("cut_markers") or [],
                         cut_verbs, below_words, negations):
        return "No", f" [auto-corrected: frame cuts above region '{region['id']}']"
    return verdict, ""



def _crop_band(img_path, y0, y1, scale=2):
    """Create one temporary crop, cleaning it even when encoding fails."""
    if not HAS_PIL:
        raise VisionError("crop verification needs Pillow")
    tmp = None
    try:
        with Image.open(img_path) as image:
            image.load()
            top, bottom = int(image.height * y0), int(image.height * y1)
            if bottom - top < 8:
                raise VisionError("image too small to crop")
            crop = _to_rgb(image).crop((0, top, image.width, bottom))
            if scale != 1:
                crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
            fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="vision_crop_")
            os.close(fd)
            crop.save(tmp, "JPEG", quality=92)
            return tmp
    except (OSError, ValueError, VisionError, Image.DecompressionBombError):
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise VisionError("cannot create crop image") from None


def _validate_region(item):
    """Normalize a configured region, warning and skipping unusable identities.

    Invalid crop geometry raises VisionError rather than using a fallback."""
    rid = item.get("id")
    if not isinstance(rid, str) or not rid.strip():
        eprint("warning: skipping region entry without a string 'id'")
        return None
    rid = rid.strip()
    keywords = item.get("keywords")
    if (not isinstance(keywords, list)
            or not all(isinstance(k, str) and k.strip() for k in keywords)
            or not keywords):
        eprint(f"warning: region {rid!r}: 'keywords' must be a non-empty "
               "list of strings; skipping")
        return None
    markers = item.get("cut_markers", [])
    if not isinstance(markers, list) or not all(isinstance(marker, str) and marker.strip() for marker in markers):
        raise VisionError("cut_markers must be a list of non-empty strings")
    region = {
        "id": rid,
        "label": str(item.get("label") or rid),
        "subject": str(item.get("subject") or ""),
        "keywords": keywords,
        "cut_markers": markers,
    }
    bands = item.get("verify_bands")
    if bands is None:
        bands = [[0.0, 1.0]]
    if not isinstance(bands, list) or not bands:
        raise VisionError("verify_bands must be a non-empty list of [top, bottom] pairs")
    ok_bands = []
    for b in bands:
        if (isinstance(b, (list, tuple)) and len(b) == 2
                and type(b[0]) in (int, float) and type(b[1]) in (int, float)
                and 0.0 <= b[0] < b[1] <= 1.0):
            ok_bands.append([float(b[0]), float(b[1])])
    if len(ok_bands) != len(bands):
        raise VisionError("verify_bands must satisfy 0 <= top < bottom <= 1")
    region["verify_bands"] = ok_bands
    return region


def build_regions(cfg):
    """Build the validated region list for framing mode.

    No regions are selected by default. Unusable entries are skipped with
    warnings; invalid crop geometry raises VisionError."""
    src = cfg.get("regions")
    if src is None:
        src = DEFAULT_REGIONS
    if not isinstance(src, list):
        eprint("warning: 'regions' in config must be a list; no regions selected")
        src = DEFAULT_REGIONS
    regions = []
    for item in src:
        if not isinstance(item, dict):
            eprint("warning: skipping non-object region entry")
            continue
        region = _validate_region(item)
        if region:
            regions.append(region)
    return regions


def _verify_positive(cfg, img_path, band, region, tmp_files):
    """Ask the open listing question on an upscaled crop band and parse the
    verdict. Returns (is_yes, full_evidence, usage). Appends temp files to tmp_files
    for cleanup by the caller."""
    tmp = _crop_band(img_path, band[0], band[1])
    tmp_files.append(tmp)
    cres = ask(
        cfg["api_url"], cfg["model"], cfg["api_key"], [tmp],
        cfg["framing_q"]["crop"],
        cfg["max_tokens"], cfg["temperature"], cfg["timeout_s"], cfg["retries"],
        cfg["max_side"], cfg["max_mb"], False,
    )
    verdict, _ = _parse_verdict(cres["answer"], region["keywords"],
                                       cfg["negations"])
    return verdict == "Yes", cres["answer"], cres.get("usage")


def _add_usage(total, usage):
    """Sum observed standard integer token counts; missing usage is unknown."""
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                total[key] = total.get(key, 0) + value


def ask_framing(cfg, img_path, fast=False):
    """framing mode: open listing + composition questions, per-region
    deterministic verdicts, cut-position consistency cross-check, and crop
    verification of every parsed "Yes" (plus a second chance for "No"
    verdicts when crop_check_negative is enabled)."""
    t0 = time.monotonic()
    parts = []
    resized = False
    usage = {}
    fq = cfg["framing_q"]
    crop_check_negative = cfg.get("crop_check_negative", False)
    for key, q in (("listing", fq["listing"]), ("composition", fq["composition"])):
        res = ask(
            cfg["api_url"], cfg["model"], cfg["api_key"], [str(img_path)], q,
            cfg["max_tokens"], cfg["temperature"], cfg["timeout_s"], cfg["retries"],
            cfg["max_side"], cfg["max_mb"], cfg["no_resize"],
        )
        resized = resized or res["resized"]
        _add_usage(usage, res.get("usage"))
        parts.append({"key": key, "answer": res["answer"]})

    listing_text = next(p["answer"] for p in parts if p["key"] == "listing")
    comp_text = next(p["answer"] for p in parts if p["key"] == "composition")

    # Derive one verdict per configured region from the listing.
    derived = []
    for region in cfg["regions"]:
        verdict, evidence = _parse_verdict(listing_text, region["keywords"],
                                           cfg["negations"])
        item = {
            "key": region["id"],
            "answer": listing_text,
            "verdict": verdict,
            "evidence": evidence,
            "derived": True,
            "verified": False,
            "crop_overridden": False,
            "crop_checks": [],
        }
        verdict, note = _apply_consistency(region, verdict, comp_text,
                                           cfg["cut_verbs"], cfg["below_words"], cfg["negations"])
        item["verdict"] = verdict
        item["auto_corrected"] = bool(note)
        item["note"] = note or (" [not established: no matching listing evidence]" if not evidence else "")
        derived.append(item)

    # Crop pass: verify every parsed "Yes", and -- when crop_check_negative
    # is enabled -- give "No" verdicts a second chance on the same bands.
    tmp_files = []
    try:
        for region, item in zip(cfg["regions"], derived):
            is_yes = _is_yes(item["verdict"])
            if not is_yes and not crop_check_negative:
                continue
            if fast:
                item["note"] = (item.get("note") or "") + " [crop check skipped: --fast]"
                continue
            if not HAS_PIL:
                item["note"] = (item.get("note") or "") + " [crop check skipped: Pillow missing]"
                continue
            verified = False
            crop_checked = False
            crop_evidence = ""
            for band in region["verify_bands"]:
                try:
                    band_yes, evidence, crop_usage = _verify_positive(
                        cfg, img_path, band, region, tmp_files)
                    _add_usage(usage, crop_usage)
                    item["crop_evidence"] = evidence
                    item["crop_checks"].append({"band": band, "evidence": evidence,
                                                "verdict": "Yes" if band_yes else "No"})
                    crop_evidence = evidence
                    crop_checked = True
                    if band_yes:
                        verified = True
                        break
                except VisionError as exc:
                    item["note"] = (item.get("note") or "") + f" [crop check failed: {exc}]"
                    crop_checked = False
                    break
            if crop_checked and verified:
                if is_yes:
                    item["verified"] = True
                    item["note"] = (item.get("note") or "") + " [crop-verified Yes]"
                else:
                    flipped, flip_note = _apply_consistency(
                        region, "Yes", comp_text, cfg["cut_verbs"], cfg["below_words"], cfg["negations"]
                    )
                    if _is_yes(flipped):
                        item["verdict"] = "Yes"
                        item["verified"] = True
                        detail = f" (evidence: {crop_evidence[:40]})" if crop_evidence else ""
                        item["note"] = (item.get("note") or "") + f" [crop-found Yes{detail}]"
                    else:
                        item["note"] = (item.get("note") or "") + flip_note
            elif crop_checked and not verified:
                if is_yes:
                    item["verdict"] = "No"
                    item["crop_overridden"] = True
                    detail = f" (evidence: {crop_evidence[:40]})" if crop_evidence else ""
                    item["note"] = (item.get("note") or "") + f" [crop check says No{detail}]"
                else:
                    item["note"] = (item.get("note") or "") + " [crop-confirmed No]"
    finally:
        for t in tmp_files:
            try:
                os.unlink(t)
            except OSError:
                pass

    lines = []
    for region, item in zip(cfg["regions"], derived):
        note = item.get("note") or ""
        evidence = item.get("evidence") or ""
        if evidence:
            evidence = evidence if len(evidence) <= 60 else evidence[:60] + "..."
            note += f" (evidence: {evidence})"
        lines.append(f"{region['label']}: {item['verdict']}{note}")
    lines.append("Composition: " + " ".join(comp_text.split()))

    return {
        "answer": "\n".join(lines),
        "usage": usage or None,
        "resized": resized,
        "orig_bytes": None,
        "sent_bytes": None,
        "seconds": round(time.monotonic() - t0, 2),
        "parts": parts + derived,
    }


def collect_images(dirpath, limit):
    p = Path(dirpath)
    if not p.is_dir():
        raise VisionError(f"directory not found: {dirpath}")
    files = [
        f
        for f in sorted(p.iterdir())
        if f.is_file() and f.suffix.lower() in MIME_BY_SUFFIX
    ]
    if limit is not None:
        if limit <= 0:
            raise VisionError("limit must be positive")
        files = files[:limit]
    return files


def run_one(cfg, img_path, question):
    t0 = time.monotonic()
    try:
        if cfg.get("mode") == "framing":
            res = ask_framing(cfg, img_path, fast=cfg.get("fast", False))
        else:
            res = ask(
                cfg["api_url"], cfg["model"], cfg["api_key"], [str(img_path)], question,
                cfg["max_tokens"], cfg["temperature"], cfg["timeout_s"], cfg["retries"],
                cfg["max_side"], cfg["max_mb"], cfg["no_resize"],
            )
        return {"file": str(img_path), "ok": True, **res}
    except VisionError as exc:
        return {
            "file": str(img_path),
            "ok": False,
            "error": str(exc),
            "seconds": round(time.monotonic() - t0, 2),
        }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Ask an OpenAI-compatible VLM about local images "
                    "(HASHI vision skill).",
        epilog="Exit codes: 0 success | 1 batch finished with failures | "
               "2 usage error, file error or API failure.",
    )
    parser.add_argument(
        "images", nargs="*",
        help="one or more image paths (omit when using --dir; order is preserved)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--question", default=None, help="specific visual question")
    group.add_argument(
        "--mode", choices=sorted(MODE_QUESTIONS), default=None,
        help="preset question: ocr / framing / describe",
    )
    parser.add_argument(
        "--dir", default=None,
        help="batch: scan a directory of images and ask the same question of each",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="with --dir: process at most N images (default: all)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="with --dir: parallel workers (1-32; default 1); "
                             "respect provider concurrency limits")
    parser.add_argument("--api-url", default=None, help="API base URL (OpenAI-compatible)")
    parser.add_argument("--model", default=None, help="model id")
    parser.add_argument("--api-key", default=None, help="Bearer token if required")
    parser.add_argument("--config", default=None,
                        help="JSON config file instead of the skill-root config.json "
                             "(provider settings, mode_questions, framing regions)")
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=None, help="seconds per request")
    parser.add_argument("--retries", type=int, default=None,
                        help="retries for 5xx / connection errors (default 2)")
    parser.add_argument("--max-side", type=int, default=None,
                        help="auto-resize longest side in px (default 1280)")
    parser.add_argument("--max-mb", type=float, default=None,
                        help="auto-resize/encode byte budget in MB (default 1.5)")
    parser.add_argument("--no-resize", action="store_true",
                        help="send files byte-for-byte (disables auto-resize "
                             "and the 413 shrink retry)")
    parser.add_argument("--fast", action="store_true",
                        help="with --mode framing: skip the crop-zoom "
                             "verification pass (fewer requests)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output (single image: one JSON "
                             "object; --dir: JSON array)")
    parser.add_argument("--show-config", action="store_true",
                        help="print the resolved provider + capabilities and exit")
    parser.add_argument("--version", action="version",
                        version=f"vision skill {VERSION}")
    return parser


def _word_list(cfg, key, default):
    value = cfg.get(key, default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(word, str) and word.strip() for word in value):
        raise VisionError(key + " must be a list of non-empty strings")
    return value


def _resolve_settings(args, cfg):
    def pick(cli, env_name, key, cast):
        value = cli
        if value is None and env_name:
            value = os.environ.get(env_name)
        if value is None:
            value = cfg.get(key, DEFAULTS[key])
        try:
            if isinstance(value, bool):
                raise ValueError()
            if cast is str:
                if not isinstance(value, str):
                    raise ValueError()
                return value.strip()
            if cast is int and isinstance(value, float) and not value.is_integer():
                raise ValueError()
            result = cast(value)
            if not math.isfinite(result):
                raise ValueError()
            return result
        except (ValueError, TypeError, OverflowError):
            raise VisionError("invalid value for " + key) from None
    values = {
        "api_url": pick(args.api_url, "VISION_API_URL", "api_url", str),
        "model": pick(args.model, "VISION_MODEL", "model", str),
        "max_tokens": pick(args.max_tokens, None, "max_tokens", int),
        "temperature": pick(args.temperature, None, "temperature", float),
        "timeout_s": pick(args.timeout, None, "timeout_s", float),
        "retries": pick(args.retries, None, "retries", int),
        "max_side": pick(args.max_side, "VISION_MAX_SIDE", "max_side", int),
        "max_mb": pick(args.max_mb, "VISION_MAX_MB", "max_mb", float),
    }
    if values["api_url"]:
        completion_url(values["api_url"])
    if not args.show_config and (not values["api_url"] or not values["model"]):
        raise VisionError("configure api_url and model using CLI, environment or private config")
    key = args.api_key if args.api_key is not None else os.environ.get("VISION_API_KEY", cfg.get("api_key"))
    key = "" if key is None else key
    if not isinstance(key, str) or "\r" in key or "\n" in key:
        raise VisionError("invalid value for api_key")
    if (values["max_tokens"] <= 0 or not 0 <= values["temperature"] <= 2
            or not 0 < values["timeout_s"] <= 3600 or not 0 <= values["retries"] <= 5
            or not 1 <= values["max_side"] <= 16384
            or values["max_mb"] * 1024 * 1024 < 1 or values["max_mb"] > 128
            or not 1 <= args.jobs <= 32 or (args.limit is not None and args.limit <= 0)):
        raise VisionError("numeric options are outside supported bounds")
    for name in ("mode_questions", "framing_questions"):
        if name in cfg and (not isinstance(cfg[name], dict)
                            or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                       for k, v in cfg[name].items())):
            raise VisionError(name + " must contain non-empty strings")
    if "crop_check_negative" in cfg and not isinstance(cfg["crop_check_negative"], bool):
        raise VisionError("crop_check_negative must be a boolean")
    regions = build_regions(cfg)
    if len({region["id"] for region in regions}) != len(regions):
        raise VisionError("region ids must be unique")
    framing_q = dict(FRAMING_QUESTIONS_DEFAULT)
    framing_q.update({k: v for k, v in cfg.get("framing_questions", {}).items() if k in framing_q})
    values.update(api_key=key, no_resize=bool(args.no_resize), mode=args.mode, fast=bool(args.fast),
                  regions=regions, framing_q=framing_q,
                  negations=_word_list(cfg, "negation_keywords", DEFAULT_NEGATIONS),
                  cut_verbs=_word_list(cfg, "cut_verbs", DEFAULT_CUT_VERBS),
                  below_words=_word_list(cfg, "below_words", DEFAULT_BELOW_WORDS),
                  crop_check_negative=cfg.get("crop_check_negative", False))
    return values


def main(argv=None):
    args = build_parser().parse_args(argv)

    skill_root = Path(__file__).resolve().parent.parent
    try:
        cfg, config_path = load_config(
            skill_root, args.config, os.environ.get("VISION_CONFIG")
        )
    except VisionError as exc:
        eprint(f"error: {exc}")
        return 2
    try:
        cfg_ctx = _resolve_settings(args, cfg)
    except VisionError as exc:
        eprint(f"error: {exc}")
        return 2
    api_url, model, api_key = (cfg_ctx[key] for key in ("api_url", "model", "api_key"))
    max_tokens, temperature, timeout_s = (cfg_ctx[key] for key in ("max_tokens", "temperature", "timeout_s"))
    retries, max_side, max_mb = (cfg_ctx[key] for key in ("retries", "max_side", "max_mb"))
    no_resize, regions = cfg_ctx["no_resize"], cfg_ctx["regions"]

    if args.show_config:
        print(json.dumps({
            "vision_skill_version": VERSION,
            "api_url": api_url,
            "model": model,
            "api_key_set": bool(api_key),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout_s": timeout_s,
            "retries": retries,
            "max_side": max_side,
            "max_mb": max_mb,
            "no_resize": no_resize,
            "fast": bool(args.fast),
            "pillow_available": HAS_PIL,
            "crop_check_negative": cfg_ctx["crop_check_negative"],
            "config_file": str(config_path) if config_path.is_file() else None,
            "framing_regions": [r["id"] for r in regions],
        }, ensure_ascii=False, indent=2))
        return 0

    modes = dict(MODE_QUESTIONS)
    if isinstance(cfg.get("mode_questions"), dict):
        modes.update({k: v for k, v in cfg["mode_questions"].items()
                      if isinstance(v, str)})
    question = None
    if args.question:
        question = args.question
    elif args.mode == "framing":
        pass  # framing dispatches to ask_framing with its own questions
    elif args.mode:
        question = modes.get(args.mode)
        if not question:
            eprint(f"error: no question defined for mode {args.mode!r}")
            return 2
    else:
        question = DEFAULT_QUESTION

    if args.mode == "framing" and not args.dir and len(args.images) > 1:
        eprint("error: framing mode accepts exactly one image")
        return 2

    if args.mode == "framing" and not regions:
        eprint(
            "error: framing mode needs at least one valid region; add a "
            "'regions' list to the config file (see config.example.json)"
        )
        return 2

    if args.dir and args.images:
        eprint("error: give either --dir or image paths, not both")
        return 2
    if not args.dir and not args.images:
        eprint("error: nothing to do: pass image paths or --dir")
        return 2

    if args.dir:
        try:
            files = collect_images(args.dir, args.limit)
        except VisionError as exc:
            eprint(f"error: {exc}")
            return 2
        if not files:
            eprint(
                f"error: no supported images found in {args.dir} "
                f"(looking for {', '.join(sorted(MIME_BY_SUFFIX))})"
            )
            return 2
        jobs = max(1, args.jobs)
        if jobs > 1:
            with ThreadPoolExecutor(max_workers=jobs) as ex:
                results = list(ex.map(lambda f: run_one(cfg_ctx, f, question), files))
        else:
            results = [run_one(cfg_ctx, f, question) for f in files]
        ok = sum(1 for r in results if r["ok"])
        failed = len(results) - ok
        total_s = sum(r.get("seconds") or 0 for r in results)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for r in results:
                print(f"===== {Path(r['file']).name} =====")
                if r["ok"]:
                    print(r["answer"])
                else:
                    print(f"error: {r['error']}")
                print()
        eprint(
            f"vision: batch done: ok={ok}/{len(results)}, failed={failed}, "
            f"{total_s:.1f}s total (excluding retry sleeps)"
        )
        return 0 if failed == 0 else 1

    try:
        if args.mode == "framing":
            res = ask_framing(cfg_ctx, args.images[0], fast=bool(args.fast))
        else:
            res = ask(api_url, model, api_key, [str(p) for p in args.images], question,
                      max_tokens, temperature, timeout_s, retries,
                      max_side, max_mb, no_resize)
    except VisionError as exc:
        if args.json:
            print(json.dumps({"file": str(Path(args.images[0]).resolve()), "ok": False,
                              "error": str(exc)}, ensure_ascii=False))
        else:
            eprint(f"error: {exc}")
        return 2
    if args.json:
        print(json.dumps(
            {"file": str(Path(args.images[0]).resolve()), "ok": True, **res},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(res["answer"])
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
