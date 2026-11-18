"""Versioned, one-invocation Office sidecar. No import by Core or Functions."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
import time
from pathlib import Path

PROTOCOL = 1
LIMIT = 256 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    opts = parser.parse_args()
    job = opts.job.resolve(strict=True)
    if not opts.job.is_absolute() or not job.is_dir() or opts.job.is_symlink():
        raise ValueError("Invalid invocation directory")
    raw = sys.stdin.buffer.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Oversized request")
    request = json.loads(raw)
    if request.get("transport") == "file-v1":
        request_id = str(request.get("request_id") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", request_id):
            raise ValueError("Invalid request identity")
        name = f"request-{request_id}.json"
        source = job / name
        if (request.get("file") != name or source.is_symlink() or not source.is_file()
                or source.resolve().parent != job):
            raise ValueError("Invalid request file")
        payload = source.read_bytes()
        if (len(payload) != request.get("size")
                or hashlib.sha256(payload).hexdigest() != request.get("sha256")):
            raise ValueError("Request file failed integrity check")
        request = json.loads(payload)
        if request.get("request_id") != request_id:
            raise ValueError("Request identity mismatch")
    if request.get("protocol") != PROTOCOL or request.get("deadline", 0) < time.time():
        raise ValueError("Invalid protocol or expired request")
    # Only the operator-qualified release directory is an application import root.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from engine import perform, OfficeError, configure_budgets
    try:
        configure_budgets(request.get("resource_budgets"))
        for name, version in request["pins"].items():
            if importlib.metadata.version(name) != version:
                raise OfficeError("RUNTIME_VERSION_MISMATCH", "Office Runtime dependencies changed.")
        if request["operation"] == "health":
            import openpyxl
            import docx
            import pptx
            import pymupdf
            from PIL import Image
            result = {"status": "ok", "versions": request["pins"],
                      "formats": ["xlsx", "docx", "pptx", "pdf", "csv"],
                      "converter": bool(request.get("converter")),
                      "python": sys.version.split()[0]}
        else:
            result = {"status": "ok", **perform(request["operation"], request["arguments"],
                       job, converter=request.get("converter"))}
    except OfficeError as exc:
        result = {"status": "error", "code": exc.code, "summary": str(exc)}
    except Exception as exc:
        result = {"status": "error", "code": "INVALID_DOCUMENT",
                  "summary": "Operation rejected: " + type(exc).__name__ + ". Original retained."}
    response = {"protocol": PROTOCOL, "request_id": request["request_id"],
                "result": result, "sha256": hashlib.sha256(canonical(result)).hexdigest()}
    encoded = canonical(response)
    if len(encoded) > LIMIT:
        if request.get("response_transport") == "file-v1":
            name = f"response-{request['request_id']}.json"
            with (job / name).open("xb") as output:
                output.write(encoded)
                output.flush()
            response = {"protocol": PROTOCOL, "request_id": request["request_id"],
                        "transport": "file-v1", "file": name, "size": len(encoded),
                        "sha256": hashlib.sha256(encoded).hexdigest()}
        else:
            result = {"status": "error", "code": "RESULT_LIMIT",
                      "summary": "Result exceeds the Office IPC limit. Requalify the Office runtime."}
            response.update(result=result, sha256=hashlib.sha256(canonical(result)).hexdigest())
    sys.stdout.buffer.write(canonical(response))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
# HASHI-Fingerprint: hfp_1f6531d27fb24dc592c7da07f517631b
