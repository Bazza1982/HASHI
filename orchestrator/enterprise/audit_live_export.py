from __future__ import annotations

import json
from dataclasses import dataclass, field
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
