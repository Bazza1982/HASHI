"""PAO-owned, bounded reboot results; delivery never owns reboot outcome."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from orchestrator.kernel_process import write_record

MAX_RECORDS = 50
MAX_BYTES = 128 * 1024
MAX_DELIVERY_ATTEMPTS = 4
ACTIVE = frozenset({"accepted", "running"})
TERMINAL = frozenset({"succeeded", "failed", "rejected", "unconfirmed"})


def clean_origin(origin):
    if origin is None:
        return {}
    if not isinstance(origin, dict):
        raise ValueError("Invalid reboot origin")
    surface = origin.get("surface") or "telegram"
    if not isinstance(surface, str) or len(surface) > 32:
        raise ValueError("Invalid reboot frontend")
    result = {"surface": surface}
    for key in ("actor_id", "chat_id", "thread_id"):
        value = origin.get(key)
        if value in (None, ""):
            result[key] = None
        elif isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Invalid reboot origin identifier")
        elif key == "actor_id" or (key == "chat_id" and surface != "telegram"):
            if len(str(value)) > 200:
                raise ValueError("Invalid reboot origin identifier")
            result[key] = str(value)
        elif len(str(value)) > 24 or not str(value).lstrip("-").isdigit():
            raise ValueError("Invalid reboot origin identifier")
        else:
            result[key] = int(value)
    return result


def validate_record(record):
    try:
        valid = (
            isinstance(record["id"], str)
            and bool(record["id"])
            and isinstance(record["source_agent"], str)
            and isinstance(record["targets"], list)
            and len(record["targets"]) <= 100
            and all(isinstance(name, str) for name in record["targets"])
            and isinstance(record["display_names"], dict)
            and isinstance(record["mode"], str)
            and record["status"] in ACTIVE | TERMINAL
            and record["delivery"]["status"]
            in {"pending", "sent", "exhausted", "not_requested"}
            and isinstance(record["delivery"]["attempts"], int)
            and 0 <= record["delivery"]["attempts"] <= MAX_DELIVERY_ATTEMPTS
            and math.isfinite(record["delivery"]["next_attempt_at"])
        )
        clean_origin(record["origin"])
        if not valid:
            raise ValueError("Invalid reboot receipt record")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid reboot receipt record") from exc


class RebootReceipts:
    def __init__(self, home: Path | None):
        self.path = Path(home) / "state/instance/reboot-receipts.json" if home else None
        self._records = None
        self._inherited_ids = set()

    def records(self):
        if self._records is None:
            records = []
            if self.path is not None and self.path.exists():
                if self.path.stat().st_size > MAX_BYTES:
                    raise ValueError("Reboot receipt storage exceeds its size limit")
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema") != 1
                    or not isinstance(payload.get("records"), list)
                ):
                    raise ValueError("Invalid reboot receipt storage")
                records = payload["records"]
                if len(records) > MAX_RECORDS:
                    raise ValueError("Too many reboot receipts")
                for record in records:
                    validate_record(record)
            self._records = records
            self._inherited_ids = {record["id"] for record in records}
        return deepcopy(self._records)

    def _save(self, records):
        payload = {"schema": 1, "records": records}
        while (
            len(records) > MAX_RECORDS
            or len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_BYTES
        ):
            disposable = next(
                (
                    i
                    for i, r in enumerate(records[:-1])
                    if r["status"] not in ACTIVE
                    and r["delivery"]["status"] != "pending"
                ),
                None,
            )
            if disposable is None:
                raise ValueError("Reboot receipt storage is full of pending operations")
            records.pop(disposable)
        if self.path is not None:
            write_record(self.path, payload)
        self._records = deepcopy(records)

    def get(self, operation_id):
        return next((r for r in self.records() if r["id"] == operation_id), None)

    def by_request(self, source, request_key):
        if not request_key:
            return None
        return next(
            (
                r
                for r in reversed(self.records())
                if r["source_agent"] == source and r.get("request_key") == request_key
            ),
            None,
        )

    def create(
        self,
        *,
        source,
        targets,
        display_names,
        mode,
        origin=None,
        locale="en",
        request_key=None,
    ):
        origin = clean_origin(origin)
        if request_key is not None and (
            not isinstance(request_key, str) or len(request_key) > 200
        ):
            raise ValueError("Invalid reboot request key")
        now = time.time()
        record = {
            "id": uuid4().hex,
            "request_key": request_key,
            "source_agent": source,
            "targets": list(targets),
            "display_names": display_names,
            "mode": mode,
            "origin": origin,
            "locale": locale,
            "created_at": now,
            "updated_at": now,
            "status": "accepted",
            "phase": "accepted",
            "committed": False,
            "restored": None,
            "reason": "",
            "online": {},
            "delivery": {
                "status": "pending"
                if origin.get("chat_id")
                and origin.get("surface", "telegram") == "telegram"
                else "not_requested",
                "attempts": 0,
                "next_attempt_at": 0,
            },
        }
        self._save([*self.records(), record])
        return deepcopy(record)

    def update(self, operation_id, **changes):
        records = self.records()
        record = next(r for r in records if r["id"] == operation_id)
        record.update(deepcopy(changes), updated_at=time.time())
        self._save(records)
        return deepcopy(record)

    def recover(self):
        # Do not infer a successful transaction merely because some Agents are
        # online after a new shared process starts. Never rerun accepted work.
        for record in self.records():
            if record["id"] not in self._inherited_ids:
                continue
            changes = {}
            if record["status"] in ACTIVE:
                changes.update(
                    status="unconfirmed", phase="interrupted", reason="interrupted"
                )
            if record["delivery"]["status"] == "pending":
                changes["recovered"] = True
            if changes:
                self.update(record["id"], **changes)
        self._inherited_ids.clear()
