"""Dependency-free client and operator qualification for the Office sidecar.

No package discovery through PATH, automatic pip, model-supplied interpreter,
or imports of Office libraries in a Functions/Core interpreter.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path

PROTOCOL = 1
MAX_IPC_BYTES = 256 * 1024
CODE_FILES = ("worker.py", "engine.py", "package_probe.py", "formatting.py")
LOCK = Path(__file__).resolve().parents[2] / "requirements" / "office-runtime-windows-py312.lock"


class RuntimeUnavailable(ValueError):
    pass


def digest(path):
    with Path(path).open("rb") as stream:
        result = hashlib.file_digest(stream, "sha256")
    return result.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_file(path):
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise RuntimeUnavailable("Runtime paths must be absolute regular files.")
    return path


def qualify_runtime(python: Path, destination: Path, *, converter: Path | None = None) -> Path:
    """Operator-only prepare/qualification. Does not install or activate anything.

    Copies the reviewed worker implementation into a new immutable generation;
    verifies every installed distribution's RECORD against the approved wheels.
    Activation is an explicit instance tool_options.office configuration change.
    """
    python = _safe_file(Path(python))
    destination = Path(destination)
    if not destination.is_absolute() or destination.exists():
        raise RuntimeUnavailable("Prepare requires a new absolute generation directory.")
    pins = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        match = re.match(r"([^#\s=]+)==([^\s]+) --hash=sha256:([a-f0-9]{64})$", line)
        if match:
            pins[match[1]] = match[2]
    if len(pins) < 7:
        raise RuntimeUnavailable("Office closure lock is incomplete.")
    probe = """import sys, json, platform, importlib.metadata as m, hashlib, pathlib
pins=json.loads(sys.argv[1])
files={}
for name, version in pins.items():
 d=m.distribution(name)
 if d.version != version: raise ValueError('version mismatch: '+name)
 for entry in d.files or ():
  p=pathlib.Path(d.locate_file(entry)).resolve()
  if p.suffix in ('.pyc','.pyo') or not p.is_file(): continue
  data=p.read_bytes()
  if entry.hash and entry.hash.mode=='sha256':
   import base64
   if base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=') != entry.hash.value:
    raise ValueError('installed file differs from wheel: '+str(p))
  files[str(p)]=hashlib.sha256(data).hexdigest()
print(json.dumps({'version':list(sys.version_info[:3]),'platform':sys.platform,'machine':platform.machine(),'prefix':sys.prefix,'files':files}))
"""
    result = subprocess.run([str(python), "-I", "-B", "-c", probe, json.dumps(pins)],
                            capture_output=True, timeout=60, check=True)
    inventory = json.loads(result.stdout)
    if inventory["version"][:2] != [3, 12] or inventory["platform"] != "win32" or inventory["machine"].lower() not in {"amd64", "x86_64"}:
        raise RuntimeUnavailable("This lock qualifies Windows x64 / CPython 3.12 only.")
    destination.mkdir(parents=True)
    files = dict(inventory["files"])
    files[str(python)] = digest(python)
    for name in CODE_FILES:
        output = destination / name
        shutil.copyfile(Path(__file__).parent / name, output)
        files[str(output)] = digest(output)
    manifest = {
        "protocol": PROTOCOL, "generation": "office-v1",
        "python": str(python), "worker": str(destination / "worker.py"),
        "python_version": inventory["version"], "platform": inventory["platform"],
        "machine": inventory["machine"], "pins": pins,
        "lock_sha256": digest(LOCK), "files": files,
        "converter": str(_safe_file(Path(converter))) if converter else None,
    }
    if converter:
        converter_root = Path(converter).resolve().parent.parent
        for member in converter_root.rglob("*"):
            if member.is_file():
                manifest["files"][str(_safe_file(member))] = digest(member)
    path = destination / "manifest.json"
    path.write_bytes(canonical(manifest))
    runtime = OfficeRuntime({"manifest": str(path), "state_root": str(destination / "qualification-state"),
                             "work_root": str(Path(os.environ.get("TEMP") or destination) / "office-qualification")})
    # Runtime health is a real isolated process; a version list alone is insufficient.
    health = asyncio.run(runtime.invoke("health", {}, destination / "health"))
    if health.get("status") != "ok":
        raise RuntimeUnavailable("Isolated Office health failed: " + str(health))
    health["manifest_sha256"] = digest(path)
    (destination / "health.json").write_bytes(canonical(health))
    return path


class OfficeRuntime:
    def __init__(self, options):
        self.options = dict(options or {})
        self.configuration_error = None
        shared = os.environ.get("HASHI_OFFICE_RUNTIME_CONFIG", "").strip()
        if not self.options and shared:
            from orchestrator.config_json import read_config_json
            source = Path(shared)
            try:
                if not source.is_absolute():
                    raise RuntimeUnavailable("HASHI_OFFICE_RUNTIME_CONFIG must be absolute.")
                self.options = dict(read_config_json(source))
            except (OSError, ValueError) as exc:
                self.configuration_error = type(exc).__name__
        self.manifest_path = Path(str(self.options.get("manifest") or ""))
        self.state_root = Path(str(self.options.get("state_root") or ""))
        self.timeout = min(120.0, max(1.0, float(self.options.get("timeout_seconds", 60))))
        self._manifest = None

    def manifest(self, *, verify=True):
        if self.configuration_error:
            raise RuntimeUnavailable("Shared Office configuration is unavailable: " + self.configuration_error)
        if not self.options.get("manifest") or not self.options.get("state_root") or not self.state_root.is_absolute():
            raise RuntimeUnavailable("Configure absolute tool_options.office.manifest and state_root.")
        path = _safe_file(self.manifest_path)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise RuntimeUnavailable("Office qualification manifest exceeds its limit.")
        manifest = json.loads(path.read_bytes())
        if manifest.get("protocol") != PROTOCOL or not manifest.get("files") or not manifest.get("pins"):
            raise RuntimeUnavailable("Unsupported Office qualification manifest.")
        if manifest.get("platform") != sys.platform:
            raise RuntimeUnavailable("Office runtime is qualified for a different OS.")
        for key in ("python", "worker"):
            configured = _safe_file(Path(manifest.get(key, "")))
            if str(configured) not in manifest["files"]:
                raise RuntimeUnavailable("Unqualified Office " + key)
        if verify:
            for raw, expected in manifest["files"].items():
                if digest(_safe_file(Path(raw))) != expected:
                    raise RuntimeUnavailable("Office runtime integrity check failed; operator must re-qualify.")
        self._manifest = manifest
        return manifest

    def availability(self):
        try:
            self.manifest(verify=False)
            health = json.loads((self.manifest_path.parent / "health.json").read_bytes())
            if health.get("status") != "ok" or health.get("manifest_sha256") != digest(self.manifest_path):
                raise RuntimeUnavailable("Office health qualification does not match this generation.")
            return {"available": True, "source": "qualified_office_runtime"}
        except (OSError, ValueError, TypeError, KeyError):
            return {"available": False, "code": "capability_unavailable",
                    "reason": "office_runtime_unqualified", "capability_kind": "office",
                    "action": "execute",
                    "next_step": "Prepare and qualify an isolated Office Runtime; configure tool_options.office."}

    async def invoke(self, operation, arguments, job):
        """Use a short private transport directory; durable versions stay in state."""
        job = Path(job)
        job.mkdir(parents=True, exist_ok=True)
        work_root = Path(str(self.options.get("work_root") or ""))
        if not self.options.get("work_root") or not work_root.is_absolute():
            raise RuntimeUnavailable("Configure an absolute short Office work_root for native libraries.")
        if os.name == "nt" and len(str(work_root)) > 100:
            raise RuntimeUnavailable("Office work_root must be at most 100 characters on Windows.")
        work_root.mkdir(parents=True, exist_ok=True)
        if work_root.is_symlink() or (hasattr(work_root,"is_junction") and work_root.is_junction()):
            raise RuntimeUnavailable("Office work_root cannot be a link.")
        with tempfile.TemporaryDirectory(prefix="office-", dir=work_root) as temporary:
            transport = Path(temporary)
            for source in job.iterdir():
                if source.is_file():
                    if source.is_symlink() or source.stat().st_size > 32*1024*1024:
                        raise RuntimeUnavailable("Invalid staging input.")
                    shutil.copyfile(source, transport/source.name)
            value = await self._invoke_staged(operation, arguments, transport)
            names = {"receipt.json"}
            if value.get("output"):
                names.add(value["output"])
            for key in ("path","image"):
                if value.get("preview",{}).get(key):
                    names.add(value["preview"][key])
            for name in names:
                if Path(name).name != name or name in {"",".",".."}:
                    raise RuntimeUnavailable("Worker returned an invalid output name.")
                source, destination = transport/name, job/name
                if not source.is_file() or source.is_symlink() or destination.is_symlink() or source.stat().st_size>32*1024*1024:
                    raise RuntimeUnavailable("Worker returned an invalid output file.")
                shutil.copyfile(source,destination)
            return value

    async def _invoke_staged(self, operation, arguments, job):
        manifest = await asyncio.to_thread(self.manifest)
        job = Path(job)
        job.mkdir(parents=True, exist_ok=True)
        if job.is_symlink() or (hasattr(job, "is_junction") and job.is_junction()):
            raise RuntimeUnavailable("Office invocation directory is linked.")
        request_id = hashlib.sha256(canonical([str(job), operation, arguments])).hexdigest()
        request = {"protocol": PROTOCOL, "request_id": request_id, "operation": operation,
                   "deadline": time.time() + self.timeout, "arguments": arguments,
                   "pins": manifest["pins"], "converter": manifest.get("converter")}
        encoded = canonical(request)
        if len(encoded) > MAX_IPC_BYTES:
            raise RuntimeUnavailable("Office request exceeds IPC byte limit.")
        # Do not pass provider, Telegram, database or other inherited credentials.
        env = {k: v for k, v in os.environ.items()
               if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "NUMBER_OF_PROCESSORS",
                                "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL"}}
        profile = job / "profile"
        profile.mkdir(exist_ok=True)
        env.update(TEMP=str(job), TMP=str(job), TMPDIR=str(job),
                   USERPROFILE=str(profile), HOME=str(profile),
                   APPDATA=str(profile / "AppData/Roaming"), LOCALAPPDATA=str(profile / "AppData/Local"),
                   HF_HOME=str(job / "cache/huggingface"), TORCH_HOME=str(job / "cache/torch"),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        from tools.builtins import (_bash_process_kwargs, _bash_process_group_id,
                                    _cleanup_bash_process, _shield_bash_cleanup)
        proc = await asyncio.create_subprocess_exec(
            manifest["python"], "-I", "-B", manifest["worker"], "--job", str(job),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(job), env=env,
            **_bash_process_kwargs())
        pgid = _bash_process_group_id(proc)
        # Read bounded streams concurrently. Overflow closes the invocation.
        async def communicate():
            proc.stdin.write(encoded)
            await proc.stdin.drain()
            proc.stdin.close()
            async def bounded(stream):
                chunks, size = [], 0
                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        return b"".join(chunks)
                    size += len(chunk)
                    if size > MAX_IPC_BYTES:
                        raise RuntimeUnavailable("Office worker output exceeded IPC limit.")
                    chunks.append(chunk)
            stdout, stderr = await asyncio.gather(bounded(proc.stdout), bounded(proc.stderr))
            await proc.wait()
            return stdout, stderr
        task = asyncio.create_task(communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except BaseException:
            await _shield_bash_cleanup(_cleanup_bash_process(proc, task, pgid=pgid))
            if task.done() and not task.cancelled():
                task.exception()
            raise
        if proc.returncode:
            raise RuntimeUnavailable("Office worker exited without a valid receipt.")
        try:
            response = json.loads(stdout)
            result = response["result"]
            if (response["protocol"] != PROTOCOL or response["request_id"] != request_id or
                    response["sha256"] != hashlib.sha256(canonical(result)).hexdigest()):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise RuntimeUnavailable("Office worker receipt is missing or has an invalid checksum.") from None
        (job / "receipt.json").write_bytes(canonical(response))
        return result
