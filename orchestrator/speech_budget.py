"""PAO-owned work estimates; transport polling is independent of generation."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass, asdict
import json
import math
import re
import time

ACTIVE_SPEECH_BUDGET: ContextVar[dict | None] = ContextVar("active_speech_budget", default=None)
NATIVE_LIMIT_SECONDS = 120.0
RESULT_MARGIN_SECONDS = 12.0
TASK_LIMIT_SECONDS = 300.0

def pronunciation_units(text: str) -> float:
    # Han/kana syllables, words in spaced languages and explicit pauses.
    east = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other = re.sub(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", " ", text)
    words = len(re.findall(r"[^\W_]+", other, flags=re.UNICODE))
    pauses = len(re.findall(r"[。！？!?；;\n]", text))
    return max(1.0, east + words * 2.0 + pauses * .4)

@dataclass(frozen=True)
class SpeechBudget:
    phase: str
    units: float
    estimated_audio_seconds: float
    native_seconds: float
    attempt_seconds: float
    total_seconds: float
    rate: float
    cold_and_queue_reserve_seconds: float
    def public(self):
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(self).items()}

class SpeechBudgetEstimator:
    def __init__(self, database):
        self.db = database
        self.db.execute("CREATE TABLE IF NOT EXISTS speech_timings (profile TEXT, phase TEXT, units REAL, seconds REAL, at REAL)")
        self.db.commit()

    def estimate(self, text, *, phase="generate", profile="default"):
        units = pronunciation_units(text)
        rows = self.db.execute(
            "SELECT units,seconds FROM speech_timings WHERE profile=? AND phase=? ORDER BY at DESC LIMIT 32",
            (profile, phase)).fetchall()
        if phase == "prepare":
            # Bounded planning prompt and output, including prefill, cold load and queue.
            samples = [seconds / max(1., amount) for amount, seconds in rows]
            rate = max(.009, sorted(samples)[math.ceil(len(samples)*.95)-1] if samples else 0)
            native = 22. + min(units, 4000.) * rate + 18.
            audio = 0.
            reserve = 22.
        else:
            audio = units / 5.
            # Conservative initial RTF; observed full-stage cost can only raise it.
            samples = [max(0., seconds-8.) / max(.2, amount/5.) for amount, seconds in rows]
            rate = max(2.0, sorted(samples)[math.ceil(len(samples)*.95)-1] if samples else 0)
            reserve = 20.  # Unknown residency/queue: reserve, never assume a warm GPU.
            expected = 4. + reserve + audio * rate
            native = expected + max(10., expected * .25)
        native = max(25., native)
        if native > NATIVE_LIMIT_SECONDS:
            raise ValueError("speech_split_required")
        attempt = native + RESULT_MARGIN_SECONDS
        total = min(TASK_LIMIT_SECONDS, attempt * 2. + 2.)
        return SpeechBudget(phase, units, audio, native, attempt, total, rate, reserve)

    def observe(self, profile, phase, text, seconds):
        units = pronunciation_units(text)
        self.db.execute("INSERT INTO speech_timings VALUES (?,?,?,?,?)", (profile, phase, units, max(.001, seconds), time.time()))
        self.db.execute("DELETE FROM speech_timings WHERE rowid NOT IN (SELECT rowid FROM speech_timings ORDER BY at DESC LIMIT 512)")
        self.db.commit()

def remaining_speech_budget():
    value = ACTIVE_SPEECH_BUDGET.get()
    if value is None:
        return None
    remaining = value["deadline"] - time.monotonic()
    if remaining <= RESULT_MARGIN_SECONDS:
        raise TimeoutError("Speech generation budget exhausted")
    return max(.001, min(value["native_seconds"], remaining - RESULT_MARGIN_SECONDS))
