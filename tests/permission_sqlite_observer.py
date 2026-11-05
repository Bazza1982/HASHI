"""Opt-in, parameter-free timing for the three Windows CI failures.

Only these test protocols are observed; SQL, parameters, results and credentials
are never retained. The observer adds no waits, retries or transaction changes.
"""
from collections import Counter, defaultdict
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

import pytest


_TARGETS = frozenset({
    "test_concurrent_same_presentation_is_idempotent_and_grants_only_after_signed_decision",
    "test_authorized_read_uses_the_bound_file_and_requested_lines",
    "test_encrypted_result_is_reused_after_a_service_restart_without_rewriting",
})
_REPORTS = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if item.originalname not in _TARGETS:
        yield
        return
    original_connect = sqlite3.connect
    observations = []
    lock = threading.Lock()
    next_connection = 0

    class ObservedConnection(sqlite3.Connection):
        def observe(self, phase, operation, *args, **kwargs):
            started = time.perf_counter()
            before = self.total_changes
            error = None
            try:
                return operation(*args, **kwargs)
            except BaseException as exc:
                error = {
                    "type": type(exc).__name__,
                    "sqlite_code": getattr(exc, "sqlite_errorcode", None),
                    "sqlite_name": getattr(exc, "sqlite_errorname", None),
                }
                raise
            finally:
                try:
                    changes = self.total_changes - before
                except sqlite3.ProgrammingError:
                    changes = 0
                observations.append({
                    "phase": phase, "elapsed_s": time.perf_counter() - started,
                    "connection": self.observer_id, "database": self.observer_database,
                    "caller": self.observer_caller, "changes": changes, "error": error,
                })

        def execute(self, sql, *args, **kwargs):
            first = sql.split()[:2]
            phase = " ".join(first) if first[0].upper() in {"BEGIN", "PRAGMA", "ATTACH"} else first[0]
            return self.observe(phase, super().execute, sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            return self.observe(sql.split()[0] + " MANY", super().executemany, sql, *args, **kwargs)

        def commit(self):
            return self.observe("COMMIT", super().commit)

        def rollback(self):
            return self.observe("ROLLBACK", super().rollback)

        def close(self):
            return self.observe("CLOSE", super().close)

    def observed_connect(database, *args, **kwargs):
        nonlocal next_connection
        if "factory" in kwargs or len(args) >= 5:
            return original_connect(database, *args, **kwargs)
        connection = original_connect(database, *args, factory=ObservedConnection, **kwargs)
        with lock:
            next_connection += 1
            connection.observer_id = next_connection
        connection.observer_database = Path(str(database)).name
        frame = sys._getframe(1)
        caller = []
        for _ in range(12):
            if frame is None:
                break
            if Path(frame.f_code.co_filename).name in {"authority.py", "result_store.py"}:
                caller.append(frame.f_code.co_name)
            frame = frame.f_back
        connection.observer_caller = "/".join(caller)
        return connection

    sqlite3.connect = observed_connect
    started = time.perf_counter()
    try:
        yield
    finally:
        sqlite3.connect = original_connect
        groups = defaultdict(list)
        for observation in observations:
            groups[(observation["database"], observation["phase"])].append(observation["elapsed_s"])
        _REPORTS.append({
            "test": item.nodeid, "elapsed_s": time.perf_counter() - started,
            "connections": next_connection,
            "phases": [
                {"database": database, "phase": phase, "count": len(durations),
                 "total_s": sum(durations), "max_s": max(durations)}
                for (database, phase), durations in sorted(groups.items())
            ],
            "writes": dict(Counter(
                entry["caller"] for entry in observations if entry["changes"] > 0
            )),
            "errors": [entry for entry in observations if entry["error"]],
            "slowest": sorted(observations, key=lambda entry: entry["elapsed_s"], reverse=True)[:20],
        })


def pytest_terminal_summary(terminalreporter):
    if _REPORTS:
        terminalreporter.section("Permission SQLite timing (SQL and parameters omitted)")
        for report in _REPORTS:
            terminalreporter.write_line(json.dumps(report, sort_keys=True))
