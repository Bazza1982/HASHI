"""FIFO admission for this authority's existing durable transaction boundary."""
from collections import deque
import threading


class LedgerTransactionQueue:
    """Later status polls cannot overtake an already waiting revocation.

    This does not interrupt SQLite, shorten its busy timeout, retry a mutation,
    or allow parallel writers. Nested calls by the owning thread remain safe.
    """
    def __init__(self):
        self._condition = threading.Condition()
        self._owner, self._depth = None, 0
        self._waiting = deque()

    def __enter__(self):
        current, ticket = threading.get_ident(), object()
        with self._condition:
            if self._owner == current:
                self._depth += 1
                return self
            self._waiting.append(ticket)
            try:
                self._condition.wait_for(lambda: self._owner is None and self._waiting[0] is ticket)
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiting.popleft()
            self._owner, self._depth = current, 1
            return self

    def __exit__(self, *_):
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError("ledger transaction ownership changed")
            self._depth -= 1
            if not self._depth:
                self._owner = None
                self._condition.notify_all()
# HASHI-Fingerprint: hfp_924711223ed547aca54ee144dc7f5219
