"""Bounded PAO speech tasks. Lost delivery never starts a second native execution."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from .speech_budget import ACTIVE_SPEECH_BUDGET, SpeechBudgetEstimator

class SpeechTaskError(Exception):
    def __init__(self, code, *, retryable=False, accepted=None):
        super().__init__(code)
        self.code, self.retryable, self.accepted = code, retryable, accepted

class SpeechTasks:
    def __init__(self, root, *, retry_delay=1.):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "speech.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS speech_tasks (
            id TEXT PRIMARY KEY, fingerprint TEXT, phase TEXT, state TEXT,
            created REAL, seen REAL, expires REAL, budget TEXT, attempts INTEGER,
            result TEXT, error TEXT)""")
        self.db.execute("UPDATE speech_tasks SET state='uncertain',error='speech_owner_restarted' WHERE state IN ('queued','running','retry_wait','cancelling')")
        self.db.commit()
        self.estimator = SpeechBudgetEstimator(self.db)
        self.running = {}
        self.retry_delay = retry_delay
        self.maintenance = None
        self.closed = False

    def _update(self, ident, **values):
        self.db.execute("UPDATE speech_tasks SET " + ",".join(k+"=?" for k in values) + " WHERE id=?",
                        (*values.values(), ident))
        self.db.commit()

    def status(self, ident):
        row = self.db.execute("SELECT * FROM speech_tasks WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise SpeechTaskError("speech_task_not_found", accepted=False)
        now = time.time()
        state = row["state"]
        if state == "completed" and row["expires"] < now:
            self._update(ident, state="expired", result=None)
            state = "expired"
        self._update(ident, seen=now)
        value = dict(request_id=ident, state=state, phase=row["phase"],
                     budget=json.loads(row["budget"]), attempts=row["attempts"],
                     can_retry=False, remaining_seconds=max(0., row["created"] + json.loads(row["budget"]).get("total_seconds", 0.) - now))
        if state == "completed":
            value["result"] = json.loads(row["result"])
        if row["error"]:
            value["error_code"] = row["error"]
        return value

    def start(self, ident, text, phase, selection, execute):
        if self.closed:
            raise SpeechTaskError("speech_owner_closed", accepted=False)
        if not isinstance(ident, str) or not ident or len(ident) > 160:
            raise SpeechTaskError("speech_invalid_request_id", accepted=False)
        if phase not in {"prepare", "generate"}:
            raise SpeechTaskError("speech_invalid_phase", accepted=False)
        fingerprint = hashlib.sha256(json.dumps([phase,text,selection],sort_keys=True,
                    ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
        existing = self.db.execute("SELECT fingerprint FROM speech_tasks WHERE id=?", (ident,)).fetchone()
        if existing:
            if existing[0] != fingerprint:
                raise SpeechTaskError("speech_request_conflict", accepted=False)
            return self.status(ident)
        if self.db.execute("SELECT COUNT(*) FROM speech_tasks").fetchone()[0] >= 10000:
            raise SpeechTaskError("speech_task_capacity", accepted=False)
        if len(self.running) >= 4:
            raise SpeechTaskError("speech_queue_full", retryable=True, accepted=False)
        budget = self.estimator.estimate(text,phase=phase,profile=str(selection.get("selection", "default")))
        now = time.time()
        self.db.execute("INSERT INTO speech_tasks (id,fingerprint,phase,state,created,seen,expires,budget,attempts) VALUES (?,?,?,?,?,?,?,?,0)",
                        (ident,fingerprint,phase,"queued",now,now,now+600,json.dumps(budget.public())))
        self.db.commit()
        self.running[ident] = asyncio.create_task(self._run(ident,text,phase,selection,budget,execute))
        if self.maintenance is None:
            self.maintenance = asyncio.create_task(self._maintain())
        return self.status(ident)

    async def _run(self, ident, text, phase, selection, budget, execute):
        deadline = time.monotonic() + budget.total_seconds
        try:
            for attempt in (1,2):
                if self.status(ident)["state"] == "cancelling":
                    raise asyncio.CancelledError()
                self._update(ident,state="running",attempts=attempt)
                attempt_deadline = min(deadline,time.monotonic()+budget.attempt_seconds)
                token = ACTIVE_SPEECH_BUDGET.set(dict(deadline=attempt_deadline,native_seconds=budget.native_seconds))
                started = time.monotonic()
                try:
                    async with asyncio.timeout_at(attempt_deadline) as timeout_scope:
                        result = await execute(attempt_deadline)
                    if timeout_scope.expired() or time.monotonic() >= attempt_deadline:
                        raise TimeoutError("Speech generation budget exhausted")
                    if self.status(ident)["state"] == "cancelling":
                        raise asyncio.CancelledError()
                    encoded = json.dumps(result,ensure_ascii=False)
                    if len(encoded.encode()) > 5*1024*1024:
                        raise SpeechTaskError("speech_result_too_large",accepted=True)
                    self.estimator.observe(str(selection.get("selection", "default")),phase,text,time.monotonic()-started)
                    self._update(ident,state="completed",result=encoded,expires=time.time()+600)
                    # Bounded transient delivery cache, never a native audio archive.
                    rows = self.db.execute("SELECT id,length(CAST(result AS BLOB)) FROM speech_tasks WHERE state='completed' ORDER BY created DESC").fetchall()
                    total = 0
                    for index, row in enumerate(rows):
                        total += row[1] or 0
                        if index >= 64 or total > 64*1024*1024:
                            self._update(row[0],state="expired",result=None)
                    return
                except SpeechTaskError as exc:
                    if exc.accepted is False and exc.retryable and attempt == 1 and deadline-time.monotonic() > budget.attempt_seconds:
                        self._update(ident,state="retry_wait",error=None)
                        await asyncio.sleep(self.retry_delay)
                        continue
                    self._update(ident,state="uncertain" if exc.accepted is None else "failed",error=exc.code)
                    return
                finally:
                    ACTIVE_SPEECH_BUDGET.reset(token)
        except asyncio.CancelledError:
            self._update(ident,state="cancelled",result=None,error="speech_cancelled")
        except (TimeoutError,ConnectionError):
            self._update(ident,state="uncertain",result=None,error="speech_outcome_unknown")
        except Exception as exc:
            code = getattr(exc, "code", None)
            code = code if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 96 else "speech_generation_failed"
            self._update(ident,state="failed",result=None,error=code)
        finally:
            self.running.pop(ident,None)

    def cancel(self, ident):
        value = self.status(ident)
        if value["state"] in {"queued","running","retry_wait","cancelling"}:
            self._update(ident,state="cancelling")
            task = self.running.get(ident)
            if task is not None and value["state"] != "cancelling":
                task.cancel()
                # A task cancelled before its coroutine enters cannot execute its finally.
                def drained(completed):
                    if completed.cancelled():
                        self._update(ident,state="cancelled",result=None,error="speech_cancelled")
                        self.running.pop(ident,None)
                task.add_done_callback(drained)
        return self.status(ident)

    async def _maintain(self):
        try:
            while True:
                await asyncio.sleep(10)
                now = time.time()
                for row in self.db.execute("SELECT id FROM speech_tasks WHERE state IN ('queued','running','retry_wait') AND seen<?",(now-60,)).fetchall():
                    self.cancel(row[0])
                self.db.execute("UPDATE speech_tasks SET state='expired',result=NULL WHERE state='completed' AND expires<?",(now,))
                self.db.commit()
        except asyncio.CancelledError:
            pass

    async def close(self):
        self.closed = True
        if self.maintenance:
            self.maintenance.cancel()
            await self.maintenance
        for ident in list(self.running):
            self.cancel(ident)
        if self.running:
            await asyncio.gather(*list(self.running.values()),return_exceptions=True)
        self.db.close()
