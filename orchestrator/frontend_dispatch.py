"""Durable, per-destination delivery dispatcher for HASHI Frontend Connectors.

This coordinator claims tasks from PAO's delivery_outbox with leased tokens,
routes standard events to connector adapters, records per-endpoint typed receipts,
and handles crash recovery and unknown outcomes without duplicating writes.
"""
from __future__ import annotations

import logging
import inspect
from collections.abc import Mapping
from typing import Any

from orchestrator.frontend_connector_registry import ReferenceConnectorAdapter
from orchestrator.frontend_contracts import normalize_delivery_receipt
from orchestrator.session_store import SessionConflict, SessionNotFound, SessionStore

logger = logging.getLogger(__name__)


def project_claimed_frontend_event(
    store: SessionStore,
    task: Mapping[str, Any],
    *,
    session_id: str,
    owner_id: str,
) -> dict[str, Any]:
    """Resolve one claimed outbox task to its authoritative standard Event."""

    evt_id = str(task["event_id"])
    if str(task.get("session_id") or session_id) != str(session_id):
        raise SessionConflict("delivery task Session mismatch")
    raw_event: Mapping[str, Any] = task
    try:
        candidates = store.events(
            session_id,
            owner_id=owner_id,
            after_sequence=max(0, int(task.get("sequence") or 0) - 1),
            limit=1,
        )
        if candidates and candidates[0].get("event_id") == evt_id:
            raw_event = candidates[0]
    except (SessionConflict, SessionNotFound):
        raw_event = task
    message_map: dict[str, Any] = {}
    detail = raw_event.get("detail")
    if isinstance(detail, Mapping):
        message_id = str(detail.get("message_id") or "").strip()
        if message_id:
            try:
                message_map[message_id] = store.get_message(
                    message_id,
                    session_id=session_id,
                    owner_id=owner_id,
                )
            except (SessionConflict, SessionNotFound):
                message_map = {}
    from orchestrator.frontend_projection import project_frontend_event

    return project_frontend_event(raw_event, message_map=message_map)


class OutcomeConnectorAdapter:
    """Adapt one transport callback to the standard Event/Receipt contract."""

    def __init__(self, connector_id: str, sender: Any):
        from orchestrator.frontend_connector_registry import (
            get_connector_capabilities,
        )

        self.connector_id = str(connector_id or "").strip().casefold()
        get_connector_capabilities(self.connector_id)
        if not callable(sender):
            raise ValueError("connector sender must be callable")
        self._sender = sender

    async def dispatch(
        self,
        event: Mapping[str, Any],
        *,
        endpoint_id: str,
    ) -> dict[str, Any]:
        from orchestrator.frontend_connector_registry import (
            require_connector_event,
        )

        require_connector_event(self.connector_id, event)
        outcome = self._sender(event, endpoint_id=endpoint_id)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, Mapping) and outcome.get("type") == (
            "hashi.delivery-receipt"
        ):
            receipt = normalize_delivery_receipt(outcome)
            if (
                receipt["event_id"] != str(event.get("event_id") or "")
                or receipt["endpoint_id"] != str(endpoint_id)
            ):
                raise ValueError("connector receipt identity mismatch")
            return receipt

        observed = dict(outcome or {}) if isinstance(outcome, Mapping) else {}
        state = str(observed.get("state") or "").strip().casefold()
        proof = observed.get("proof")
        if bool(observed.get("delivered")):
            status = "delivered" if proof or observed.get("message_id") else "accepted"
        elif state in {"queued", "accepted"}:
            status = "accepted"
        elif state == "duplicate":
            status = "duplicate"
        elif state in {"suppressed", "skipped", "not_attempted"} or not bool(
            observed.get("attempted")
        ):
            status = "skipped"
        elif state == "unknown":
            status = "unknown"
        else:
            status = "failed"
        if status == "delivered" and proof is None:
            proof = {
                "type": f"{self.connector_id}-message-id",
                "value": str(observed["message_id"]),
            }
        return normalize_delivery_receipt(
            {
                "type": "hashi.delivery-receipt",
                "version": 1,
                "event_id": str(event.get("event_id") or ""),
                "endpoint_id": str(endpoint_id),
                "status": status,
                "proof": proof,
            }
        )


class FrontendDispatcher:
    """Coordinates durable delivery across multiple frontend connector destinations."""

    def __init__(
        self,
        runtime_or_store: Any,
        *,
        worker_id: str | None = None,
        adapters: dict[str, Any] | None = None,
    ):
        if isinstance(runtime_or_store, SessionStore):
            self.store = runtime_or_store
            self.runtime = None
        else:
            self.runtime = runtime_or_store
            from orchestrator.runtime_session import ensure_store

            self.store = ensure_store(runtime_or_store)

        import uuid

        self.worker_id = worker_id or f"dispatcher_{uuid.uuid4().hex[:12]}"
        self.adapters: dict[str, Any] = {}
        for connector_id, adapter in (adapters or {}).items():
            self.register_adapter(connector_id, adapter)
        if "reference" not in self.adapters:
            self.register_adapter("reference", ReferenceConnectorAdapter())

    def register_adapter(self, connector_id: str, adapter: Any) -> None:
        """Register one declared adapter; arbitrary bypass IDs fail closed."""
        from orchestrator.frontend_connector_registry import (
            get_connector_capabilities,
        )

        normalized = str(connector_id or "").strip().casefold()
        get_connector_capabilities(normalized)
        declared = str(getattr(adapter, "connector_id", normalized) or "").strip().casefold()
        if declared != normalized:
            raise ValueError(
                f"connector adapter identity mismatch: {declared} != {normalized}"
            )
        if not callable(getattr(adapter, "dispatch", None)):
            raise ValueError("connector adapter must implement dispatch")
        self.adapters[normalized] = adapter

    async def dispatch_once(
        self,
        session_id: str,
        owner_id: str,
        *,
        event_id: str | None = None,
        limit: int = 10,
        lease_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        """Claim and dispatch pending outbox tasks for one Session."""
        claimed: list[dict[str, Any]] = []
        for connector_id in sorted(self.adapters):
            remaining = int(limit) - len(claimed)
            if remaining <= 0:
                break
            claimed.extend(
                self.store.claim_delivery_outbox(
                    session_id=session_id,
                    owner_id=owner_id,
                    worker_id=self.worker_id,
                    event_id=event_id,
                    connector_id=connector_id,
                    limit=remaining,
                    lease_seconds=lease_seconds,
                )
            )

        results: list[dict[str, Any]] = []

        for task in claimed:
            results.append(
                await self.dispatch_claimed_task(
                    task,
                    session_id=session_id,
                    owner_id=owner_id,
                )
            )

        return results

    async def dispatch_claimed_task(
        self,
        task: Mapping[str, Any],
        *,
        session_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        """Project and dispatch one task already claimed with a fenced lease."""

        outbox_id = str(task["outbox_id"])
        lease_token = str(task["lease_token"])
        evt_id = str(task["event_id"])
        route = task.get("delivery_route") or {}
        standard_event = project_claimed_frontend_event(
            self.store,
            task,
            session_id=session_id,
            owner_id=owner_id,
        )
        destinations = self._resolve_destinations(task, route)
        destination_statuses: list[str] = []
        for dest in destinations:
            connector_id = dest["connector_id"]
            endpoint_id = dest["endpoint_id"]
            existing_receipts = self.store.frontend_delivery_receipts(
                session_id=session_id,
                owner_id=owner_id,
                event_id=evt_id,
            )
            if any(
                row.get("endpoint_id") == endpoint_id
                and row.get("status") == "delivered"
                for row in existing_receipts
            ):
                destination_statuses.append("delivered")
                continue
            adapter = self.adapters.get(connector_id)
            if adapter is None:
                receipt = {
                    "type": "hashi.delivery-receipt",
                    "version": 1,
                    "event_id": evt_id,
                    "endpoint_id": endpoint_id,
                    "status": "failed",
                    "proof": None,
                }
            else:
                try:
                    from orchestrator.frontend_connector_registry import (
                        require_connector_event,
                    )

                    require_connector_event(connector_id, standard_event)
                    receipt = normalize_delivery_receipt(
                        await adapter.dispatch(
                            standard_event,
                            endpoint_id=endpoint_id,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Adapter %s dispatch outcome is unknown for %s: %s",
                        connector_id,
                        endpoint_id,
                        exc,
                    )
                    receipt = {
                        "type": "hashi.delivery-receipt",
                        "version": 1,
                        "event_id": evt_id,
                        "endpoint_id": endpoint_id,
                        "status": "unknown",
                        "proof": None,
                    }
            self.store.record_frontend_delivery_receipt(
                session_id=session_id,
                owner_id=owner_id,
                receipt=receipt,
            )
            destination_statuses.append(receipt["status"])

        if not destination_statuses:
            final_status = "suppressed"
        elif any(status == "unknown" for status in destination_statuses):
            final_status = "unknown"
        elif all(status == "failed" for status in destination_statuses):
            final_status = "failed"
        else:
            final_status = "completed"
        try:
            self.store.complete_delivery_outbox(
                outbox_id=outbox_id,
                lease_token=lease_token,
                status=final_status,
            )
            return {
                "outbox_id": outbox_id,
                "status": final_status,
                "event": standard_event,
                "destination_statuses": destination_statuses,
            }
        except SessionConflict as exc:
            logger.warning(
                "Delivery outbox lease conflict for %s: %s", outbox_id, exc
            )
            return {
                "outbox_id": outbox_id,
                "status": "stale_lease_conflict",
                "event": standard_event,
                "destination_statuses": destination_statuses,
            }

    def _resolve_destinations(
        self,
        task: Mapping[str, Any],
        route: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Resolve destination endpoints for one outbox event."""
        if task.get("connector_id") and task.get("endpoint_id"):
            return [
                {
                    "connector_id": str(task["connector_id"]),
                    "endpoint_id": str(task["endpoint_id"]),
                    "role": str(task.get("role") or "primary"),
                }
            ]
        destinations: list[dict[str, Any]] = []
        from orchestrator.frontend_connector_registry import (
            canonical_connector_id,
            endpoint_id_for,
        )

        primary = route.get("primary")
        if isinstance(primary, Mapping):
            surface = str(primary.get("surface") or "backend_api").casefold()
            channel = str(primary.get("channel_key") or "default")
            connector_id = canonical_connector_id(
                surface, ingress_transport=surface, surface=surface
            )
            ep_id = endpoint_id_for(
                connector_id, ingress_transport=surface, channel_key=channel
            )
            destinations.append({"connector_id": connector_id, "endpoint_id": ep_id, "role": "primary"})

        for mirror in route.get("mirrors") or []:
            if isinstance(mirror, Mapping):
                surface = str(mirror.get("surface") or "telegram").casefold()
                channel = str(mirror.get("channel_key") or "default")
                connector_id = canonical_connector_id(
                    surface, ingress_transport=surface, surface=surface
                )
                ep_id = endpoint_id_for(
                    connector_id, ingress_transport=surface, channel_key=channel
                )
                destinations.append({"connector_id": connector_id, "endpoint_id": ep_id, "role": "mirror"})

        return destinations


__all__ = [
    "FrontendDispatcher",
    "OutcomeConnectorAdapter",
    "project_claimed_frontend_event",
]
