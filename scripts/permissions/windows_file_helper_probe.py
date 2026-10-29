"""Fixed disposable-account qualification of the product held-handle helper."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from orchestrator.permissions.models import normalize_scope
from orchestrator.permissions.windows_file_binding import open_bound_read, open_bound_write
from orchestrator.permissions.windows_ipc import current_process_user_sid
from orchestrator.permissions.windows_process import inheritable_handle_copy, launch_suspended


def probe(expected_sid):
    root = Path(__file__).parent
    scratch = Path(os.environ["TEMP"])
    executable = Path(sys.executable)
    entry = root / "orchestrator" / "permissions" / "file_worker.py"
    if current_process_user_sid() != expected_sid:
        raise RuntimeError("unexpected qualification account")
    try:
        with entry.open("r+b"):
            pass
    except PermissionError:
        sealed = True
    else:
        raise RuntimeError("qualified helper is writable by the supervisor account")
    source = scratch / "fixture.txt"
    source.write_bytes(b"long original fixture content")
    outputs = []
    for number, (action, data) in enumerate((("write", b"short"), ("read", b""), ("create", b"new"))):
        target = source if action != "create" else scratch / "new.txt"
        scope = normalize_scope("file", str(target), ("read",) if action == "read" else ("create", "write"), cwd=scratch)
        bound = open_bound_read(scope) if action == "read" else open_bound_write(scope)
        kernel, handle = inheritable_handle_copy(bound._handles[-1])
        result = scratch / f"helper-result-{number}.json"
        plan = {"format": "hashi-file-helper-v1", "action": action, "handles": [handle],
                "directory": getattr(bound, "_directory", ""), "name": getattr(bound, "_name", ""),
                "data": base64.b64encode(data).decode("ascii"), "result": str(result)}
        plan_path = scratch / f"helper-plan-{number}.json"
        encoded = json.dumps(plan).encode("utf-8")
        plan_path.write_bytes(encoded)
        owned = None
        try:
            owned = launch_suspended(
                executable=executable, arguments=["-I", "-S", str(entry), str(plan_path), hashlib.sha256(encoded).hexdigest()],
                cwd=scratch, environment=dict(os.environ), expected_user_sid=expected_sid,
                executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
                inherited_handles=(handle,),
            )
            owned.start()
            deadline = time.monotonic() + 5
            while not owned.poll() and time.monotonic() < deadline:
                time.sleep(.01)
            if not owned.poll() or owned.exit_code() != 0:
                raise RuntimeError("fixed file helper failed")
            owned.stop(timeout_ms=4500)
            output = json.loads(result.read_bytes())
            if output.get("outcome") != "succeeded":
                raise RuntimeError("fixed file outcome is unverified")
            if action == "read":
                assert base64.b64decode(output["data"], validate=True) == b"short"
            else:
                assert target.read_bytes() == data
            outputs.append({"action": action, "verified": True, "atomic_job": owned.observation.atomic_job,
                            "not_elevated": not owned.observation.elevated, "job_stopped": True})
        finally:
            if owned is not None:
                owned.stop(timeout_ms=4500)
            bound.close()
            if not kernel.CloseHandle(handle):
                raise RuntimeError("transfer handle cleanup failed")
    return {"qualified_image_readonly": sealed, "operations": outputs}
