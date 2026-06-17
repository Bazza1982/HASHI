from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Literal

from orchestrator.enterprise.audit_export import format_otel_log, format_siem_event
from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger, LedgerEvent


AuditLiveExportFormat = Literal["ledger", "siem", "otel"]
AuditLiveExportTransport = Callable[[str, bytes, dict[str, str], float], tuple[int, str]]


@dataclass(frozen=True)
class AuditLiveExportEndpoint:
    url: str
    format: AuditLiveExportFormat = "siem"
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    batch_size: int = 100


@dataclass(frozen=True)
class AuditLiveExportResult:
    attempted: int
    sent: int
    last_chain_index: int
    status_code: int | None
    format: str
    endpoint_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "last_chain_index": self.last_chain_index,
            "status_code": self.status_code,
            "format": self.format,
            "endpoint_url": self.endpoint_url,
        }


@dataclass(frozen=True)
class AuditLiveExportCycleResult:
    result: AuditLiveExportResult
    attempts: int
    checkpoint_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.result.to_dict(),
            "attempts": self.attempts,
            "checkpoint_path": self.checkpoint_path,
        }


class FileAuditLiveExportCheckpoint:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> int:
        if not self.path.exists():
            return 0
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"audit live export checkpoint is invalid: {self.path}") from exc
        return max(0, int(payload.get("last_chain_index") or 0))

    def save(self, chain_index: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_chain_index": max(0, int(chain_index or 0))}
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)


class AuditLiveExporter:
    def __init__(self, ledger: EnterpriseAuditLedger, *, transport: AuditLiveExportTransport | None = None):
        self.ledger = ledger
        self.transport = transport

    def export_since(
        self,
        endpoint: AuditLiveExportEndpoint,
        *,
        checkpoint_chain_index: int = 0,
    ) -> AuditLiveExportResult:
        if self.transport is None:
            raise ValueError("audit live export transport is required")
        batch_size = max(1, min(int(endpoint.batch_size), 1000))
        events = [
            event
            for event in self.ledger.query(limit=1000)
            if int(event.chain_index or 0) > int(checkpoint_chain_index or 0)
        ][:batch_size]
        if not events:
            return AuditLiveExportResult(
                attempted=0,
                sent=0,
                last_chain_index=int(checkpoint_chain_index or 0),
                status_code=None,
                format=endpoint.format,
                endpoint_url=endpoint.url,
            )
        body, content_type = _encode_live_export(events, endpoint.format)
        headers = {"content-type": content_type, **{str(k).lower(): str(v) for k, v in endpoint.headers.items()}}
        status_code, response_text = self.transport(endpoint.url, body, headers, float(endpoint.timeout_seconds))
        if status_code < 200 or status_code >= 300:
            raise ValueError(f"audit live export failed with HTTP {status_code}: {_redact_response(response_text)}")
        return AuditLiveExportResult(
            attempted=len(events),
            sent=len(events),
            last_chain_index=max(int(event.chain_index or 0) for event in events),
            status_code=status_code,
            format=endpoint.format,
            endpoint_url=endpoint.url,
        )

    def export_with_checkpoint(
        self,
        endpoint: AuditLiveExportEndpoint,
        checkpoint: FileAuditLiveExportCheckpoint,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        sleeper: Callable[[float], None] | None = None,
    ) -> AuditLiveExportCycleResult:
        attempts = max(1, int(max_attempts or 1))
        sleep = sleeper or time.sleep
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            checkpoint_chain_index = checkpoint.load()
            try:
                result = self.export_since(endpoint, checkpoint_chain_index=checkpoint_chain_index)
                checkpoint.save(result.last_chain_index)
                return AuditLiveExportCycleResult(
                    result=result,
                    attempts=attempt,
                    checkpoint_path=str(checkpoint.path),
                )
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                sleep(max(0.0, float(backoff_seconds)))
        raise ValueError(f"audit live export failed after {attempts} attempt(s): {last_error}") from last_error


def _encode_live_export(events: list[LedgerEvent], export_format: str) -> tuple[bytes, str]:
    normalized = str(export_format or "siem").strip().lower()
    if normalized in {"ledger", "ndjson"}:
        lines = [json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) for event in events]
        return ("\n".join(lines) + "\n").encode("utf-8"), "application/x-ndjson"
    if normalized in {"siem", "ecs"}:
        lines = [json.dumps(format_siem_event(event), ensure_ascii=False, sort_keys=True) for event in events]
        return ("\n".join(lines) + "\n").encode("utf-8"), "application/x-ndjson"
    if normalized in {"otel", "opentelemetry"}:
        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "hashi"}}]},
                    "scopeLogs": [
                        {
                            "scope": {"name": "hashi.enterprise.audit"},
                            "logRecords": [format_otel_log(event) for event in events],
                        }
                    ],
                }
            ]
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"), "application/json"
    raise ValueError(f"unsupported audit live export format: {export_format}")


def _redact_response(text: str) -> str:
    compact = str(text or "").strip().replace("\n", " ")
    if len(compact) > 160:
        return compact[:157] + "..."
    return compact
