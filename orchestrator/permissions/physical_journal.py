"""Private physical file-helper journal, independent of permission grants.

A durable record is written while the fixed helper is still suspended. Its
preassigned claim identity closes the prepare/journal crash window. Unknown
business outcomes remain unknown; recovery never reruns a helper or a write.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from orchestrator.permissions.descriptors import ToolDescriptor, _arguments_digest
from orchestrator.permissions.models import ExecutionContext, Scope
from orchestrator.permissions.file_operations import FILE_TOOLS
from orchestrator.permissions.network_worker import NETWORK_TOOLS
from orchestrator.permissions.file_results import render_helper_result
from orchestrator.permissions.windows_process import ProcessObservation, observe_original_process_stopped
from orchestrator.permissions.windows_jobs import WindowsJob
from orchestrator.permissions.windows_job_security import private_job_name


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


class PhysicalFileJournal:
    def __init__(self, path, *, artifact_root, authority, results, issuer,
                 observe_stopped=observe_original_process_stopped, recover_job=WindowsJob.recover_stop, retire_script=None):
        self.path, self.root = Path(path), Path(artifact_root).resolve()
        self.authority, self.results, self.issuer = authority, results, issuer
        self._observe_stopped, self._recover_job = observe_stopped, recover_job
        self._retire_script = retire_script
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS physical_files(
                claim_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('open','stopped','unknown','finished')),
                payload BLOB NOT NULL, digest TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS safety_parent_jobs(
                name TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('active','retired')),
                payload BLOB NOT NULL, digest TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS execution_owners(
                epoch TEXT PRIMARY KEY, binding BLOB NOT NULL, digest TEXT NOT NULL,
                native_host INTEGER NOT NULL CHECK(native_host IN (0,1)),
                state TEXT NOT NULL CHECK(state IN ('active','retired'))
            )""")

    def reserve_parent(self, name, owner_sid):
        value = {"name": name, "owner_sid": owner_sid, "generation": self.authority._service_generation}
        data = _encoded(value)
        with self._connection() as connection:
            connection.execute("INSERT INTO safety_parent_jobs VALUES(?,'active',?,?)",
                               (name, data, sha256(data).hexdigest()))

    def retire_parent(self, name):
        with self._connection() as connection:
            connection.execute("UPDATE safety_parent_jobs SET state='retired' WHERE name=?", (name,))

    def _recover_parents(self):
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM safety_parent_jobs WHERE state='active'").fetchall()
        for row in rows:
            if sha256(row["payload"]).hexdigest() != row["digest"]:
                raise ValueError("Safety parent Job evidence is damaged")
            value = json.loads(row["payload"])
            if value["name"] != row["name"]:
                raise ValueError("Safety parent Job reservation changed")
            if value["generation"] == self.authority._service_generation:
                continue
            proof = self._recover_job(value["name"], value["owner_sid"], max_processes=None)
            if not isinstance(proof, dict) or proof.get("zero_job") is not True:
                raise OSError("the previous Safety process still owns native resources")
            self.retire_parent(value["name"])

    def record_execution(self, binding, *, native_host):
        data = _encoded(asdict(binding))
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM execution_owners WHERE epoch=?",
                                     (binding.context.execution_epoch,)).fetchone()
            if row is not None:
                if (row["digest"] != sha256(data).hexdigest() or row["binding"] != data
                        or bool(row["native_host"]) != native_host or row["state"] != "active"):
                    raise ValueError("physical execution ownership changed")
                return
            connection.execute("INSERT INTO execution_owners VALUES(?,?,?,?,'active')",
                               (binding.context.execution_epoch, data, sha256(data).hexdigest(), int(native_host)))

    def execution_mode(self, binding):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM execution_owners WHERE epoch=?",
                                     (binding.context.execution_epoch,)).fetchone()
            if row is None:
                return None
            data = _encoded(asdict(binding))
            if row["digest"] != sha256(data).hexdigest() or row["binding"] != data:
                raise ValueError("physical execution binding changed")
            return bool(row["native_host"])

    def prepare_native_host(self, binding):
        # A monotonic physical intention, not a permission or a level switch.
        self.execution_mode(binding)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM execution_owners WHERE epoch=?",
                                     (binding.context.execution_epoch,)).fetchone()
            if row is None or row["state"] != "active":
                raise ValueError("native host ownership is unavailable")
            connection.execute("UPDATE execution_owners SET native_host=1 WHERE epoch=? AND state='active'",
                               (binding.context.execution_epoch,))

    def execution_retired(self, binding):
        self.execution_mode(binding)
        with self._connection() as connection:
            row = connection.execute("SELECT state FROM execution_owners WHERE epoch=?",
                                     (binding.context.execution_epoch,)).fetchone()
            return row is not None and row["state"] == "retired"

    def retire_execution(self, binding):
        self.execution_mode(binding)
        with self._connection() as connection:
            connection.execute("UPDATE execution_owners SET state='retired' WHERE epoch=?",
                               (binding.context.execution_epoch,))

    def outstanding_executions(self):
        from orchestrator.permissions.models import RuntimeExecutionBinding

        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM execution_owners WHERE state='active'").fetchall()
            result = []
            for row in rows:
                if sha256(row["binding"]).hexdigest() != row["digest"]:
                    raise ValueError("physical execution history is damaged")
                raw = json.loads(row["binding"])
                binding = RuntimeExecutionBinding(
                    ExecutionContext(**raw["context"]), raw["request_id"], raw["fencing_token"], raw["worker_id"],
                )
                if binding.context.execution_epoch != row["epoch"]:
                    raise ValueError("physical execution epoch changed")
                result.append(binding)
            return tuple(result)

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

    def reserve(self, *, context, operation_id, descriptor, arguments, cwd, claim_id,
                owner_sid, plan_path, plan_digest, max_processes=1):
        if max_processes != (2 if descriptor.tool_name == "run_script" else 1):
            raise ValueError("the helper process limit differs from its fixed operation")
        job_name = private_job_name(claim_id)
        plan = Path(plan_path).resolve()
        if not plan.is_relative_to(self.root) or plan.name != "plan.json":
            raise ValueError("the helper plan is outside its protected artifact root")
        payload = {
            "version": 2, "context": asdict(context), "operation_id": operation_id,
            "descriptor": asdict(descriptor), "arguments": arguments, "cwd": str(cwd),
            "claim_id": claim_id, "observation": None, "max_processes": max_processes,
            "job_name": job_name, "owner_sid": owner_sid,
            "plan": str(plan), "plan_digest": plan_digest,
        }
        data = _encoded(payload)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO physical_files VALUES(?,?,'open',?,?)",
                (claim_id, self.authority._service_generation, data, sha256(data).hexdigest()),
            )

        return job_name

    def observe_process(self, claim_id, observation, *, max_processes):
        if (not isinstance(observation, ProcessObservation) or observation.creation_time_100ns <= 0
                or not observation.atomic_job or max_processes not in {1, 2}):
            raise ValueError("a fixed single-process helper and observed creation identity are required")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM physical_files WHERE claim_id=?", (claim_id,)).fetchone()
            if row is None or row["state"] != "open" or sha256(row["payload"]).hexdigest() != row["digest"]:
                raise ValueError("physical reservation changed")
            payload = json.loads(row["payload"])
            if (payload["observation"] is not None or payload["owner_sid"] != observation.user_sid
                    or payload["max_processes"] != max_processes):
                raise ValueError("physical process was already bound or belongs to another account")
            payload["observation"] = asdict(observation)
            data = _encoded(payload)
            connection.execute("UPDATE physical_files SET payload=?,digest=? WHERE claim_id=?",
                               (data, sha256(data).hexdigest(), claim_id))

    def record(self, *, observation, max_processes, **arguments):
        self.reserve(owner_sid=observation.user_sid, max_processes=max_processes, **arguments)
        self.observe_process(arguments["claim_id"], observation, max_processes=max_processes)

    def stopped(self, claim_id, *, observed_stop=False):
        if not observed_stop:
            with self._connection() as connection:
                row = connection.execute("SELECT * FROM physical_files WHERE claim_id=?", (claim_id,)).fetchone()
                if row is None or sha256(row["payload"]).hexdigest() != row["digest"]:
                    raise ValueError("physical reservation changed")
                value = json.loads(row["payload"])
            proof = self._recover_job(value["job_name"], value["owner_sid"], max_processes=value["max_processes"])
            if not isinstance(proof, dict) or proof.get("zero_job") is not True:
                raise OSError("reserved Job cleanup remains unverified")
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
                if (payload.get("version") != 2 or payload["claim_id"] != row["claim_id"]
                        or payload["max_processes"] != (2 if payload["descriptor"]["tool_name"] == "run_script" else 1)
                        or payload["job_name"] != private_job_name(row["claim_id"])):
                    raise ValueError("physical helper binding changed")
                result.append((row["state"], payload, row["digest"]))
            return result

    def reconcile(self, *, binding=None):
        """Settle every independent owner; one failed row cannot skip the others."""
        failures = []
        if binding is None:
            try:
                self._recover_parents()
            except Exception as error:
                failures.append(error)
        for state, value, evidence_digest in self.pending():
            if binding is not None and value["context"] != asdict(binding.context):
                continue
            try:
                observation = ProcessObservation(**value["observation"]) if value["observation"] is not None else None
                if state != "stopped":
                    proof = self._recover_job(value["job_name"], value["owner_sid"], max_processes=value["max_processes"])
                    if not isinstance(proof, dict) or proof.get("zero_job") is not True:
                        continue
                    if observation is not None and self._observe_stopped(observation) is not True:
                        continue
                self._reconcile_stopped(value, evidence_digest)
            except Exception as error:
                failures.append(error)
        if failures:
            raise OSError("some physical owners remain unverified") from failures[0]

    def _already_settled(self, context, operation_id, descriptor, claim_id):
        status = self.authority.operation_status_for_task(context, operation_id)
        if status.state not in {"succeeded", "failed"} or self.authority.resource_state(claim_id) != "stopped":
            return False
        result = self.results.get(context, operation_id, descriptor)
        if result.outcome != status.state or result.result_digest != status.result_digest:
            raise ValueError("the settled result differs from the permission ledger")
        self._state(claim_id, "finished")
        return True

    def _reconcile_stopped(self, value, evidence_digest):
        context = ExecutionContext(**value["context"])
        raw = value["descriptor"]
        descriptor = ToolDescriptor(
            raw["tool_name"], tuple(Scope(**scope) for scope in raw["scopes"]),
            raw["arguments_digest"], raw["coverage"], tuple(raw["limitations"]),
        )
        if (descriptor.tool_name not in FILE_TOOLS | NETWORK_TOOLS | {"run_script"}
                or _arguments_digest(descriptor.tool_name, value["arguments"], Path(value["cwd"]))
                != descriptor.arguments_digest or not descriptor.scopes
                or (descriptor.tool_name not in ({"file_search", "run_script"} | NETWORK_TOOLS) and len(descriptor.scopes) != 1)):
            raise ValueError("physical helper arguments changed")
        claim = self.authority.resource_claim_for_operation(
            context, value["operation_id"], descriptor.arguments_digest, claim_id=value["claim_id"],
        )
        if claim is None:
            # This helper was durably recorded before prepare, and its exact
            # preassigned claim was never committed. It cannot have been resumed.
            self._state(value["claim_id"], "finished")
            return
        if value["observation"] is None:
            raise ValueError("a committed claim has no physical process observation")
        status = self.authority.operation_status_for_task(context, value["operation_id"])
        plan_path = Path(value["plan"]).resolve()
        if not plan_path.is_relative_to(self.root) or plan_path.name != "plan.json":
            raise ValueError("physical artifact path changed")
        if self._already_settled(context, value["operation_id"], descriptor, claim.claim_id):
            return
        try:
            plan_bytes = plan_path.read_bytes()
        except FileNotFoundError:
            if self._already_settled(context, value["operation_id"], descriptor, claim.claim_id):
                return
            raise
        if sha256(plan_bytes).hexdigest() != value["plan_digest"]:
            raise ValueError("physical helper plan changed")
        plan = json.loads(plan_bytes)
        result_path = Path(plan["result"]).resolve()
        if result_path != plan_path.parent / "result.json":
            raise ValueError("physical result destination changed")
        result = None
        recovered_script = None
        if descriptor.tool_name == "run_script":
            if self._retire_script is None:
                raise OSError("the original script resource owner is unavailable")
            from orchestrator.permissions.wire import decode
            import base64

            helper = json.loads(base64.b64decode(plan["data"], validate=True))
            binding = decode(helper["binding"])
            if binding.context != context or helper["operation_id"] != value["operation_id"]:
                raise ValueError("script physical recovery binding changed")
            recovered_script = self._retire_script(binding, value["operation_id"], descriptor)
            if recovered_script is not None:
                if (not isinstance(recovered_script, dict) or recovered_script.get("outcome") not in {"succeeded", "failed"}
                        or not isinstance(recovered_script.get("payload"), str)
                        or sha256(recovered_script["payload"].encode("utf-8")).hexdigest() != recovered_script.get("result_digest")):
                    raise ValueError("the original script result changed")
                result = self.results.put(context, value["operation_id"], descriptor,
                                          outcome=recovered_script["outcome"], payload=recovered_script["payload"])
        if result_path.is_file():
            if result_path.stat().st_size > 24 * 1024 * 1024:
                raise ValueError("physical helper result is oversized")
            candidate = json.loads(result_path.read_bytes())
            if isinstance(candidate, dict) and set(candidate) == {"outcome", "data"} and candidate["outcome"] in {"succeeded", "failed"}:
                payload = render_helper_result(descriptor, value["arguments"], candidate)
                if recovered_script is not None and (recovered_script["payload"] != payload or recovered_script["outcome"] != candidate["outcome"]):
                    raise ValueError("the helper and original script result disagree")
                result = self.results.put(context, value["operation_id"], descriptor, outcome=candidate["outcome"], payload=payload)
        if result is not None:
            if status.state == "unknown":
                self.authority.reconcile_outcome(
                    context, claim.claim_id,
                    receipt_reference=self.issuer.issue_outcome(context, claim.claim_id, result),
                )
            elif status.state == "started":
                self.authority.finish(context, claim.claim_id, outcome=result.outcome, result_digest=result.result_digest)
            elif status.state != result.outcome or status.result_digest != result.result_digest:
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
                    "job_process_limit": value["max_processes"], "job_name": value["job_name"], "journal_digest": evidence_digest,
                    "plan_digest": value["plan_digest"],
                }),
            )
        final = self.authority.operation_status_for_task(context, value["operation_id"])
        self._state(value["claim_id"], "unknown" if final.state == "unknown" else "finished")
