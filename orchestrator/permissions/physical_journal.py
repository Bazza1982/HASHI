"""Private physical file-helper journal, independent of permission grants.

A durable record is written while the fixed helper is still suspended. Its
preassigned claim identity closes the prepare/journal crash window. Unknown
business outcomes remain unknown; recovery never reruns a helper or a write.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import base64
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from orchestrator.permissions.descriptors import ToolDescriptor, _arguments_digest
from orchestrator.permissions.models import ExecutionContext, Scope
from orchestrator.permissions.windows_file_supervisor import _render_read
from orchestrator.permissions.windows_process import ProcessObservation, observe_original_process_stopped


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


class PhysicalFileJournal:
    def __init__(self, path, *, artifact_root, authority, results, issuer,
                 observe_stopped=observe_original_process_stopped):
        self.path, self.root = Path(path), Path(artifact_root).resolve()
        self.authority, self.results, self.issuer = authority, results, issuer
        self._observe_stopped = observe_stopped
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS physical_files(
                claim_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','stopped','unknown','finished')),
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

    def record(self, *, context, operation_id, descriptor, arguments, cwd, claim_id,
               observation, plan_path, plan_digest, max_processes):
        if (not isinstance(observation, ProcessObservation) or observation.creation_time_100ns <= 0
                or not observation.atomic_job or max_processes != 1
                or not isinstance(claim_id, str) or len(claim_id) != 32):
            raise ValueError("a fixed single-process helper and observed creation identity are required")
        plan = Path(plan_path).resolve()
        if not plan.is_relative_to(self.root) or plan.name != "plan.json":
            raise ValueError("the helper plan is outside its protected artifact root")
        payload = {
            "version": 1, "context": asdict(context), "operation_id": operation_id,
            "descriptor": asdict(descriptor), "arguments": arguments, "cwd": str(cwd),
            "claim_id": claim_id, "observation": asdict(observation), "max_processes": 1,
            "plan": str(plan), "plan_digest": plan_digest,
        }
        data = _encoded(payload)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO physical_files VALUES(?,?,'open',?,?)",
                (claim_id, self.authority._service_generation, data, sha256(data).hexdigest()),
            )

    def stopped(self, claim_id):
        with self._connection() as connection:
            connection.execute("UPDATE physical_files SET state='stopped' WHERE claim_id=? AND state='open'",
                               (claim_id,))

    def finished(self, claim_id):
        self._state(claim_id, "finished")

    def _state(self, claim_id, state):
        with self._connection() as connection:
            connection.execute("UPDATE physical_files SET state=? WHERE claim_id=?", (state, claim_id))

    def pending(self):
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM physical_files WHERE (state='open' AND generation!=?) OR state='stopped'",
                (self.authority._service_generation,),
            ).fetchall()
            result = []
            for row in rows:
                if sha256(row["payload"]).hexdigest() != row["digest"]:
                    raise ValueError("physical helper evidence is damaged")
                payload = json.loads(row["payload"])
                if payload["claim_id"] != row["claim_id"] or payload["max_processes"] != 1:
                    raise ValueError("physical helper binding changed")
                result.append((row["state"], payload, row["digest"]))
            return result

    def reconcile(self):
        """Read original PID identity and fixed result artifacts; never execute."""
        for state, value, evidence_digest in self.pending():
            observation = ProcessObservation(**value["observation"])
            if state != "stopped" and self._observe_stopped(observation) is not True:
                continue
            self._reconcile_stopped(value, evidence_digest)

    def _reconcile_stopped(self, value, evidence_digest):
        context = ExecutionContext(**value["context"])
        raw = value["descriptor"]
        descriptor = ToolDescriptor(
            raw["tool_name"], tuple(Scope(**scope) for scope in raw["scopes"]),
            raw["arguments_digest"], raw["coverage"], tuple(raw["limitations"]),
        )
        if (descriptor.tool_name not in {"file_read", "file_write"}
                or _arguments_digest(descriptor.tool_name, value["arguments"], Path(value["cwd"]))
                != descriptor.arguments_digest or len(descriptor.scopes) != 1):
            raise ValueError("physical helper arguments changed")
        claim = self.authority.resource_claim_for_operation(
            context, value["operation_id"], descriptor.arguments_digest, claim_id=value["claim_id"],
        )
        if claim is None:
            # This helper was durably recorded before prepare, and its exact
            # preassigned claim was never committed. It cannot have been resumed.
            self._state(value["claim_id"], "finished")
            return
        status = self.authority.operation_status_for_task(context, value["operation_id"])
        plan_path = Path(value["plan"]).resolve()
        if not plan_path.is_relative_to(self.root) or plan_path.name != "plan.json":
            raise ValueError("physical artifact path changed")
        plan_bytes = plan_path.read_bytes()
        if sha256(plan_bytes).hexdigest() != value["plan_digest"]:
            raise ValueError("physical helper plan changed")
        plan = json.loads(plan_bytes)
        result_path = Path(plan["result"]).resolve()
        if result_path != plan_path.parent / "result.json":
            raise ValueError("physical result destination changed")
        result = None
        if result_path.is_file():
            if result_path.stat().st_size > 24 * 1024 * 1024:
                raise ValueError("physical helper result is oversized")
            candidate = json.loads(result_path.read_bytes())
            if isinstance(candidate, dict) and set(candidate) == {"outcome", "data"} and candidate["outcome"] == "succeeded":
                if descriptor.tool_name == "file_read":
                    args = value["arguments"]
                    payload = _render_read(
                        descriptor.scopes[0].resource, base64.b64decode(candidate["data"], validate=True),
                        max(1, args.get("offset", 1)), max(1, min(500, args.get("limit", 500))),
                    )
                else:
                    payload = f"OK: wrote {len(value['arguments']['content'])} characters to {descriptor.scopes[0].resource}"
                result = self.results.put(context, value["operation_id"], descriptor, outcome="succeeded", payload=payload)
        if result is not None:
            if status.state == "unknown":
                self.authority.reconcile_outcome(
                    context, claim.claim_id,
                    receipt_reference=self.issuer.issue_outcome(context, claim.claim_id, result),
                )
            elif status.state == "started":
                self.authority.finish(context, claim.claim_id, outcome=result.outcome, result_digest=result.result_digest)
            elif status.state != "succeeded" or status.result_digest != result.result_digest:
                raise ValueError("physical result conflicts with the permission ledger")
        elif status.state == "started":
            self.authority.finish(context, claim.claim_id, outcome="unknown", result_digest=None)
        elif status.state == "prepared":
            self.authority.cancel_prepared(context, claim.claim_id)
        if self.authority.resource_state(claim.claim_id) != "stopped":
            self.authority.confirm_resource_stopped(
                context, claim.claim_id, receipt_reference=self.issuer.issue_stop(context, claim.claim_id, {
                    "original_process_stopped": True, "pid": value["observation"]["process_id"],
                    "creation_time_100ns": value["observation"]["creation_time_100ns"],
                    "single_process_job": True, "journal_digest": evidence_digest,
                    "plan_digest": value["plan_digest"],
                }),
            )
        final = self.authority.operation_status_for_task(context, value["operation_id"])
        self._state(value["claim_id"], "unknown" if final.state == "unknown" else "finished")
