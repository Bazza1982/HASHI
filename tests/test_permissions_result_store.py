"""Encrypted result copies keep a stable operation bound to one exact result."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import sqlite3

import pytest

from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.models import ExecutionContext
from orchestrator.permissions.result_store import EncryptedResultStore, ResultConflict, ResultUnavailable


@pytest.fixture
def example(tmp_path):
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="executor", policy_revision=1,
    )
    descriptor = describe_tool(
        "file_write", {"path": "report.txt", "content": "private source"},
        cwd=tmp_path, read_roots=(tmp_path,),
    )
    key = bytes(range(32))
    path = tmp_path / "results.sqlite"
    store = EncryptedResultStore(path, key=key)
    return store, path, key, context, descriptor


def test_result_survives_reopening_without_plaintext_on_disk(example):
    store, path, key, context, descriptor = example
    result = store.put(context, "operation", descriptor, outcome="succeeded", payload="private result 内容")
    restored = EncryptedResultStore(path, key=key).get(context, "operation", descriptor)
    assert restored == result
    assert restored.payload == "private result 内容"
    assert b"private result" not in path.read_bytes()
    assert b"private source" not in path.read_bytes()
    assert key not in path.read_bytes()


def test_same_operation_is_idempotent_but_cannot_replace_its_result(example):
    store, path, _, context, descriptor = example
    first = store.put(context, "operation", descriptor, outcome="succeeded", payload="first")
    with sqlite3.connect(path) as connection:
        original = connection.execute("SELECT nonce,ciphertext FROM encrypted_operation_results").fetchone()
    assert store.put(context, "operation", descriptor, outcome="succeeded", payload="first") == first
    with pytest.raises(ResultConflict):
        store.put(context, "operation", descriptor, outcome="succeeded", payload="replacement")
    with pytest.raises(ResultConflict):
        store.put(context, "operation", descriptor, outcome="failed", payload="first")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT nonce,ciphertext FROM encrypted_operation_results").fetchone() == original
    assert store.get(context, "operation", descriptor) == first


def test_result_is_bound_to_owner_run_and_exact_operation_facts(example):
    store, _, _, context, descriptor = example
    store.put(context, "operation", descriptor, outcome="succeeded", payload="private")
    for other in (replace(context, owner_id="other"), replace(context, run_id="other-run"),
                  replace(context, session_id="other-session")):
        assert store.get(other, "operation", descriptor) is None
    assert store.get(context, "other-operation", descriptor) is None
    changed = replace(descriptor, arguments_digest="b" * 64)
    with pytest.raises(ResultUnavailable):
        store.get(context, "operation", changed)
    with pytest.raises(ResultUnavailable):
        store.put(context, "operation", changed, outcome="succeeded", payload="other")
    assert store.get(context, "operation", descriptor).payload == "private"


def test_new_worker_of_same_run_can_recover_the_original_result(example):
    store, path, key, context, descriptor = example
    expected = store.put(context, "operation", descriptor, outcome="succeeded", payload="saved")
    later = replace(context, worker_generation="new-worker", execution_epoch="new-epoch",
                    context_generation="new-context")
    assert EncryptedResultStore(path, key=key).get(later, "operation", descriptor) == expected


@pytest.mark.parametrize("damage", ["ciphertext", "nonce", "wrong-key"])
def test_tampered_or_inaccessible_result_fails_closed(example, damage):
    store, path, key, context, descriptor = example
    store.put(context, "operation", descriptor, outcome="succeeded", payload="private")
    if damage != "wrong-key":
        with sqlite3.connect(path) as connection:
            field = "nonce" if damage == "nonce" else "ciphertext"
            existing = connection.execute(f"SELECT {field} FROM encrypted_operation_results").fetchone()[0]
            damaged = bytes([existing[0] ^ 1]) + existing[1:]
            connection.execute(f"UPDATE encrypted_operation_results SET {field}=?", (damaged,))
    else:
        store = EncryptedResultStore(path, key=bytes(reversed(key)))
    with pytest.raises(ResultUnavailable):
        store.get(context, "operation", descriptor)
    with pytest.raises(ResultUnavailable):
        store.put(context, "operation", descriptor, outcome="succeeded", payload="replacement")


def test_competing_results_for_same_operation_cannot_overwrite_each_other(example):
    store, _, _, context, descriptor = example
    barrier = Barrier(2)

    def write(payload):
        barrier.wait()
        try:
            store.put(context, "operation", descriptor, outcome="succeeded", payload=payload)
            return "saved"
        except ResultConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("first", "second")))
    assert sorted(results) == ["conflict", "saved"]
    assert store.get(context, "operation", descriptor).payload in {"first", "second"}


def test_unknown_database_is_not_overwritten(tmp_path):
    path = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE important(value TEXT)")
        connection.execute("INSERT INTO important VALUES('preserve')")
    with pytest.raises(ValueError):
        EncryptedResultStore(path, key=bytes(range(32)))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM important").fetchone() == ("preserve",)


@pytest.mark.parametrize("key", [b"", b"short", bytes(range(31)), "not-bytes"])
def test_key_must_be_supplied_by_the_trusted_service(tmp_path, key):
    with pytest.raises(ValueError):
        EncryptedResultStore(tmp_path / "results.sqlite", key=key)
    assert not (tmp_path / "results.sqlite").exists()


def test_oversized_or_unfinished_result_cannot_be_stored(example):
    store, _, _, context, descriptor = example
    with pytest.raises(ValueError):
        store.put(context, "operation", descriptor, outcome="unknown", payload="uncertain")
    with pytest.raises(ValueError):
        store.put(context, "operation", descriptor, outcome="succeeded", payload="x" * (1024 * 1024 + 1))
    assert store.get(context, "operation", descriptor) is None


@pytest.mark.parametrize("operation", ["", " ", "invalid" + chr(0), "x" * 513])
def test_invalid_operation_id_never_creates_a_result(example, operation):
    store, path, _, context, descriptor = example
    with pytest.raises(ValueError):
        store.put(context, operation, descriptor, outcome="succeeded", payload="result")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM encrypted_operation_results").fetchone() == (0,)


def test_schema_changed_after_open_is_rejected_without_touching_existing_rows(example):
    store, path, _, context, descriptor = example
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE encrypted_operation_results")
        connection.execute("CREATE TABLE encrypted_operation_results (result_key TEXT PRIMARY KEY, nonce BLOB, ciphertext BLOB)")
        connection.execute("INSERT INTO encrypted_operation_results VALUES('preserve', ?, ?)", (b"a", b"b"))
    with pytest.raises(ValueError):
        store.put(context, "operation", descriptor, outcome="succeeded", payload="result")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT result_key,nonce,ciphertext FROM encrypted_operation_results").fetchall() == [
            ("preserve", b"a", b"b"),
        ]


def test_oversized_corrupt_ciphertext_is_rejected_before_decryption(example):
    store, path, _, context, descriptor = example
    store.put(context, "operation", descriptor, outcome="succeeded", payload="private")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE encrypted_operation_results SET ciphertext=zeroblob(?)", (7 * 1024 * 1024,))

    class MustNotDecrypt:
        def decrypt(self, *args):
            raise AssertionError("unbounded ciphertext reached the decryptor")

    store._cipher = MustNotDecrypt()
    with pytest.raises(ResultUnavailable):
        store.get(context, "operation", descriptor)


def test_nonce_collision_never_overwrites_or_creates_a_second_result(example, monkeypatch):
    import orchestrator.permissions.result_store as module

    store, path, _, context, descriptor = example
    monkeypatch.setattr(module.os, "urandom", lambda _count: b"n" * 12)
    first = store.put(context, "first", descriptor, outcome="succeeded", payload="one")
    with pytest.raises(ResultUnavailable):
        store.put(context, "second", descriptor, outcome="succeeded", payload="two")
    assert store.get(context, "first", descriptor) == first
    assert store.get(context, "second", descriptor) is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM encrypted_operation_results").fetchone() == (1,)


def test_maximum_text_with_escaped_characters_remains_recoverable(example):
    store, _, _, context, descriptor = example
    payload = chr(0) * (1024 * 1024)
    store.put(context, "operation", descriptor, outcome="succeeded", payload=payload)
    restored = store.get(context, "operation", descriptor)
    assert len(restored.payload) == len(payload)
    assert restored.payload == payload


@pytest.mark.asyncio
async def test_encrypted_result_is_reused_after_a_service_restart_without_rewriting(example, tmp_path):
    import asyncio

    from orchestrator.permissions.authority import PermissionAuthority
    from orchestrator.permissions.execution_gate import PermissionExecutionGate
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.waiting import PermissionWaiter
    from tests.test_permission_execution_gate import Issuer, TestFileSupervisor, find_pending

    store, path, key, context, _ = example
    issuer = Issuer()
    authority = PermissionAuthority(
        tmp_path / "ledger.sqlite", verifier=issuer, clock=lambda: 1000.0, monotonic=lambda: 1000.0
    )
    authority.open_task(context)

    class StoredTestSupervisor(TestFileSupervisor):
        def __init__(self, authority, root, result_store):
            super().__init__(authority, root)
            self.result_store = result_store
            self.executions = 0
            self.recoveries = 0

        async def execute(self, ctx, operation_id, descriptor, arguments):
            self.executions += 1
            result = await super().execute(ctx, operation_id, descriptor, arguments)
            self.result_store.put(ctx, operation_id, descriptor, outcome=result.outcome, payload=result.payload)
            issuer.confirm_stopped(self.authority, ctx, self.last_claim)
            return result

        async def recover(self, ctx, operation_id, descriptor):
            self.recoveries += 1
            return self.result_store.get(ctx, operation_id, descriptor)

    ceiling = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    waiter = PermissionWaiter(authority, reconcile_seconds=0.02)
    supervisor = StoredTestSupervisor(authority, tmp_path, store)
    gate = PermissionExecutionGate(
        authority, waiter, supervisor, context=context, cwd=tmp_path,
        read_roots=(tmp_path,), requestable_scopes=(ceiling,),
    )
    arguments = {"path": "report.txt", "content": "created once"}
    task = asyncio.create_task(gate.execute("operation", "file_write", arguments, reason="report"))
    request = await find_pending(authority, context)
    facts = authority.operation_status_for_task(context, "operation")
    issuer.approve(authority, context, request, facts.arguments_digest)
    waiter.notify()
    assert (await asyncio.wait_for(task, 1)).state == "succeeded"
    assert supervisor.executions == 1
    (tmp_path / "report.txt").write_text("changed later", encoding="utf-8")

    reopened = PermissionAuthority(
        authority.path, verifier=issuer, clock=lambda: 1000.0, monotonic=lambda: 1000.0
    )
    later = replace(context, execution_epoch="new-epoch", worker_generation="new-worker")
    reopened.bind_execution(later)
    new_supervisor = StoredTestSupervisor(reopened, tmp_path, EncryptedResultStore(path, key=key))
    resumed = PermissionExecutionGate(
        reopened, PermissionWaiter(reopened), new_supervisor, context=later, cwd=tmp_path,
        read_roots=(tmp_path,), requestable_scopes=(ceiling,),
    )
    result = await resumed.execute("operation", "file_write", arguments, reason="report")
    assert result.state == "succeeded" and result.replayed and result.payload == "written"
    assert new_supervisor.executions == 0 and new_supervisor.recoveries == 1
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "changed later"

    with sqlite3.connect(path) as connection:
        old = connection.execute("SELECT ciphertext FROM encrypted_operation_results").fetchone()[0]
        connection.execute("UPDATE encrypted_operation_results SET ciphertext=?", (bytes([old[0] ^ 1]) + old[1:],))
    damaged = await resumed.execute("operation", "file_write", arguments, reason="report")
    assert damaged.state == "result_unavailable"
    assert new_supervisor.executions == 0 and new_supervisor.recoveries == 2
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "changed later"
