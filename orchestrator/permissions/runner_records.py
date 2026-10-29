"""Private Runner-owned host/profile reservations; these rows grant no access."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import uuid4

from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.windows_container import TaskContainerProfile
from orchestrator.permissions.windows_job_security import private_job_name
from orchestrator.permissions.windows_jobs import WindowsJob
from orchestrator.permissions.windows_process import ProcessObservation, observe_original_process_stopped


def binding_digest(binding):
    if not isinstance(binding, RuntimeExecutionBinding):
        raise ValueError("a canonical Run binding is required")
    return sha256(_encoded(asdict(binding))).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class RunnerRecords:
    def __init__(self, root, *, instance_id, runner_sid, recover_job=WindowsJob.recover_stop,
                 observe_stopped=observe_original_process_stopped,
                 remove_profile=TaskContainerProfile.remove_reserved):
        self.root, self.instance, self.sid = Path(root), instance_id, runner_sid
        self.path = self.root / "hosts.sqlite"
        self._recover_job, self._observe_stopped, self._remove_profile = recover_job, observe_stopped, remove_profile
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS hosts(
                identity TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('active','retired')),
                payload BLOB NOT NULL, digest TEXT NOT NULL
            )""")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _payload(self, row):
        if sha256(row["payload"]).hexdigest() != row["digest"]:
            raise ValueError("Runner ownership evidence is damaged")
        value = json.loads(row["payload"])
        if (value.get("version") != 1 or value["binding"]["context"]["instance_id"] != self.instance
                or sha256(_encoded(value["binding"])).hexdigest() != row["identity"]
                or value["runner_sid"] != self.sid
                or re.fullmatch(r"HASHI\.Task\.[0-9a-f]{32}", value["profile"]) is None
                or Path(value["scratch"]) != self.root / ("host-" + row["identity"])):
            raise ValueError("Runner ownership belongs to another binding")
        return value

    def reserve(self, binding):
        identity = binding_digest(binding)
        if binding.context.instance_id != self.instance:
            raise ValueError("host belongs to another instance")
        scratch = self.root / ("host-" + identity)
        if scratch.exists() or scratch.is_symlink() or getattr(scratch, "is_junction", lambda: False)():
            raise ValueError("an unknown host scratch directory must be preserved")
        value = {
            "version": 1, "binding": asdict(binding), "runner_sid": self.sid,
            "profile": "HASHI.Task." + uuid4().hex, "scratch": str(scratch),
            "job_name": private_job_name(uuid4().hex), "observation": None,
        }
        data = _encoded(value)
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM hosts WHERE identity=?", (identity,)).fetchone():
                raise ValueError("a host epoch cannot be created again")
            connection.execute("INSERT INTO hosts VALUES(?,'active',?,?)",
                               (identity, data, sha256(data).hexdigest()))
        return value

    def observe(self, binding, observation):
        if (not isinstance(observation, ProcessObservation) or observation.user_sid != self.sid
                or not observation.atomic_job or not observation.lpac or observation.capability_count != 0
                or observation.elevated or observation.creation_time_100ns <= 0):
            raise ValueError("an actual restricted host observation is required")
        identity = binding_digest(binding)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM hosts WHERE identity=?", (identity,)).fetchone()
            if row is None or row["state"] != "active":
                raise ValueError("the host reservation is unavailable")
            value = self._payload(row)
            if value["observation"] is not None:
                raise ValueError("a host reservation was already bound")
            value["observation"] = asdict(observation)
            data = _encoded(value)
            connection.execute("UPDATE hosts SET payload=?,digest=? WHERE identity=?",
                               (data, sha256(data).hexdigest(), identity))

    def pending(self):
        with self._connection() as connection:
            return tuple((row["identity"], self._payload(row)) for row in connection.execute(
                "SELECT * FROM hosts WHERE state='active'",
            ).fetchall())

    def retire(self, binding, *, process_stopped=False):
        identity = binding_digest(binding)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM hosts WHERE identity=?", (identity,)).fetchone()
            if row is None or row["state"] == "retired":
                return True
            value = self._payload(row)
        if not process_stopped:
            proof = self._recover_job(value["job_name"], self.sid, max_processes=1)
            if not isinstance(proof, dict) or proof.get("zero_job") is not True:
                raise OSError("the original host Job has not stopped")
            if value["observation"] is not None and not self._observe_stopped(ProcessObservation(**value["observation"])):
                raise OSError("the original host process has not stopped")
        # The private journal reserved this name before the profile or scratch
        # existed. Never scan/delete unrelated profiles or sibling directories.
        self._remove_profile(value["profile"])
        scratch = Path(value["scratch"])
        if scratch.is_symlink() or getattr(scratch, "is_junction", lambda: False)():
            raise OSError("the owned host scratch identity changed")
        if scratch.exists():
            shutil.rmtree(scratch)
        with self._connection() as connection:
            connection.execute("UPDATE hosts SET state='retired' WHERE identity=?", (identity,))
        return True

    def recover_all(self):
        from orchestrator.permissions.models import ExecutionContext

        for _, value in self.pending():
            raw = value["binding"]
            binding = RuntimeExecutionBinding(
                ExecutionContext(**raw["context"]), raw["request_id"], raw["fencing_token"], raw["worker_id"],
            )
            self.retire(binding)
