from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StepProtocolError(RuntimeError):
    pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


@dataclass
class SequentialStepState:
    path: Path

    @classmethod
    def create(cls, path: Path, *, target_steps: int, seed: str) -> "SequentialStepState":
        if target_steps < 1:
            raise ValueError("target_steps must be positive")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"step state already exists: {path}")
        state = cls(path)
        _atomic_json(
            path,
            {
                "schema_version": 1,
                "seed": str(seed),
                "target_steps": int(target_steps),
                "accepted_steps": 0,
                "accepted_tokens": [],
                "events": [],
            },
        )
        return state

    def load(self) -> dict[str, Any]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise StepProtocolError("unsupported step state schema")
        return payload

    @staticmethod
    def token_for(seed: str, step: int) -> str:
        suffix = hashlib.sha256(f"{seed}:{step}".encode("utf-8")).hexdigest()[:12]
        return f"HER-STEP-{step:04d}-{suffix}"

    def expected_token(self) -> str | None:
        payload = self.load()
        step = int(payload["accepted_steps"]) + 1
        if step > int(payload["target_steps"]):
            return None
        return self.token_for(str(payload["seed"]), step)

    def accept(self, token: str) -> dict[str, Any]:
        payload = self.load()
        current = int(payload["accepted_steps"])
        target = int(payload["target_steps"])
        if current >= target:
            raise StepProtocolError("all sequential steps are already complete")
        expected = self.token_for(str(payload["seed"]), current + 1)
        supplied = str(token)
        if supplied in payload.get("accepted_tokens", []):
            raise StepProtocolError("repeated step token")
        if supplied != expected:
            raise StepProtocolError(f"out-of-order step token; expected step {current + 1}")
        payload["accepted_steps"] = current + 1
        payload.setdefault("accepted_tokens", []).append(supplied)
        payload.setdefault("events", []).append(
            {
                "step": current + 1,
                "token_sha256": hashlib.sha256(supplied.encode("utf-8")).hexdigest(),
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _atomic_json(self.path, payload)
        complete = current + 1 == target
        return {
            "ok": True,
            "state_changed": True,
            "accepted_step": current + 1,
            "target_steps": target,
            "complete": complete,
            "next_token": None if complete else self.token_for(str(payload["seed"]), current + 2),
        }
