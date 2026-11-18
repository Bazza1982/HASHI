"""Office large-payload transport with real Windows worker processes."""

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.office.runtime import OfficeRuntime, RuntimeUnavailable


def runtime_for(tmp_path, monkeypatch, worker, *, supports=True, work_root=None):
    runtime = OfficeRuntime({"work_root": str(work_root or (tmp_path / "work")), "timeout_seconds": 30})
    manifest = {"python": sys.executable, "worker": str(worker), "pins": {}, "converter": None,
                "transport_capabilities": ["resource-budgets-v1"]}
    if supports:
        manifest["transport_capabilities"].append("file-v1")
    monkeypatch.setattr(runtime, "manifest", lambda: manifest)
    return runtime


def test_large_unicode_request_and_response_over_long_windows_path(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    worker = release / "worker.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "tools" / "office" / "worker.py", worker)
    (release / "engine.py").write_text(
        "class OfficeError(Exception):\n    pass\n"
        "def configure_budgets(value):\n    pass\n"
        "def perform(operation, arguments, job, converter=None):\n"
        "    return {'echo': arguments['payload']}\n",
        encoding="utf-8",
    )
    long_work_root = tmp_path / ("long-work-directory-" * 3)
    assert len(str(long_work_root)) > 100
    runtime = runtime_for(tmp_path, monkeypatch, worker, work_root=long_work_root)
    payload = "雪🔭-document-" * 35_000
    assert len(payload.encode()) > 256 * 1024
    value = asyncio.run(runtime.invoke("echo", {"payload": payload}, tmp_path / "job"))
    assert value["status"] == "ok" and value["echo"] == payload
    assert hashlib.sha256(value["echo"].encode()).digest() == hashlib.sha256(payload.encode()).digest()
    receipt = json.loads((tmp_path / "job" / "receipt.json").read_bytes())
    assert receipt["result"]["echo"] == payload


@pytest.mark.parametrize("failure", ["digest", "path", "identity", "truncated"])
def test_large_response_reference_tampering_is_rejected(tmp_path, monkeypatch, failure):
    worker = tmp_path / "fake-worker.py"
    worker.write_text(
        "import json,sys,pathlib\n"
        "r=json.load(sys.stdin); job=pathlib.Path(sys.argv[-1]); rid=r['request_id']\n"
        "name='response-'+rid+'.json'; (job/name).write_bytes(b'broken')\n"
        "case=" + repr(failure) + "\n"
        "v={'protocol':1,'request_id':rid,'transport':'file-v1','file':name,'size':6,'sha256':'0'*64}\n"
        "if case=='path': v['file']='../../outside'\n"
        "if case=='identity': v['request_id']='f'*64\n"
        "if case=='truncated': v['size']=99\n"
        "print(json.dumps(v))\n",
        encoding="utf-8",
    )
    runtime = runtime_for(tmp_path, monkeypatch, worker)
    with pytest.raises(RuntimeUnavailable, match="invalid checksum"):
        asyncio.run(runtime._invoke_staged("echo", {}, tmp_path / "job"))
    assert not (tmp_path / "job" / "receipt.json").exists()


def test_worker_rejects_tampered_large_request_before_processing(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    worker = release / "worker.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "tools" / "office" / "worker.py", worker)
    job = tmp_path / "job"
    job.mkdir()
    request_id = "a" * 64
    name = f"request-{request_id}.json"
    payload = b'{"protocol":1}'
    (job / name).write_bytes(payload)
    frame = {"protocol": 1, "transport": "file-v1", "request_id": request_id,
             "file": name, "size": len(payload), "sha256": "0" * 64}
    result = subprocess.run([sys.executable, "-I", "-B", str(worker), "--job", str(job)],
                            input=json.dumps(frame).encode(), capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b"integrity check" in result.stderr


def test_legacy_runtime_reports_large_request_needs_requalification(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path, monkeypatch, tmp_path / "legacy.py", supports=False)
    with pytest.raises(RuntimeUnavailable, match="large request transport"):
        asyncio.run(runtime._invoke_staged("echo", {"payload": "x" * 300_000}, tmp_path / "job"))


def test_legacy_runtime_reports_configured_budgets_need_requalification(tmp_path, monkeypatch):
    from tools.office.limits import DEFAULT_BUDGETS

    runtime = runtime_for(tmp_path, monkeypatch, tmp_path / "legacy.py")
    runtime.budgets = {**DEFAULT_BUDGETS, "file_bytes": DEFAULT_BUDGETS["file_bytes"] + 1}
    monkeypatch.setattr(runtime, "manifest", lambda: {
        "python": sys.executable, "worker": str(tmp_path / "legacy.py"),
        "pins": {}, "converter": None, "transport_capabilities": ["file-v1"],
    })
    with pytest.raises(RuntimeUnavailable, match="configurable resource budgets"):
        asyncio.run(runtime._invoke_staged("echo", {}, tmp_path / "job"))
# HASHI-Fingerprint: hfp_a98e771a45a64f0bbc83a1a4cd5348e2
