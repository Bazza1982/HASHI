"""Durable, per-destination delivery dispatcher for HASHI Frontend Connectors.

This coordinator claims tasks from PAO's delivery_outbox with leased tokens,
routes standard events to connector adapters, records per-endpoint typed receipts,
and handles crash recovery and unknown outcomes without duplicating writes.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from orchestrator.frontend_connector_registry import ReferenceConnectorAdapter
from orchestrator.frontend_contracts import normalize_delivery_receipt
from orchestrator.session_store import SessionConflict, SessionNotFound, SessionStore

logger = logging.getLogger(__name__)


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
        self.adapters: dict[str, Any] = adapters or {}
        if "reference" not in self.adapters:
            self.adapters["reference"] = ReferenceConnectorAdapter()

    def register_adapter(self, connector_id: str, adapter: Any) -> None:
        """Register a destination connector adapter."""
        self.adapters[str(connector_id).strip().casefold()] = adapter

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
        claimed = self.store.claim_delivery_outbox(
            session_id=session_id,
            owner_id=owner_id,
            worker_id=self.worker_id,
            event_id=event_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )

        results: list[dict[str, Any]] = []

        for task in claimed:
            outbox_id = task["outbox_id"]
            lease_token = task["lease_token"]
            evt_id = task["event_id"]
            route = task.get("delivery_route") or {}

            destinations = self._resolve_destinations(task, route)
            destination_statuses: list[str] = []

            for dest in destinations:
                connector_id = dest["connector_id"]
                endpoint_id = dest["endpoint_id"]

                # Check if already delivered
                existing_receipts = self.store.frontend_delivery_receipts(
                    session_id=session_id,
                    owner_id=owner_id,
                    event_id=evt_id,
                )
                already_delivered = any(
                    r.get("endpoint_id") == endpoint_id and r.get("status") == "delivered"
                    for r in existing_receipts
                )
                if already_delivered:
                    destination_statuses.append("delivered")
                    continue

                # Dispatch via adapter
                adapter = self.adapters.get(connector_id)
                receipt: dict[str, Any]
                if adapter is not None and hasattr(adapter, "dispatch"):
                    try:
                        raw_receipt = await adapter.dispatch(task, endpoint_id=endpoint_id)
                        receipt = normalize_delivery_receipt(raw_receipt)
                    except ConnectionError:
                        receipt = {
                            "type": "hashi.delivery-receipt",
                            "version": 1,
                            "event_id": evt_id,
                            "endpoint_id": endpoint_id,
                            "status": "unknown",
                            "proof": None,
                        }
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
                elif connector_id in {"session_api", "backend_api", "tui", "external"}:
                    # Pull/local consumers can prove feed admission, not user delivery/read.
                    receipt = {
                        "type": "hashi.delivery-receipt",
                        "version": 1,
                        "event_id": evt_id,
                        "endpoint_id": endpoint_id,
                        "status": "accepted",
                        "proof": None,
                    }
                else:
                    # Unknown connector or no adapter
                    receipt = {
                        "type": "hashi.delivery-receipt",
                        "version": 1,
                        "event_id": evt_id,
                        "endpoint_id": endpoint_id,
                        "status": "failed",
                        "proof": None,
                    }

                # Record receipt in store
                self.store.record_frontend_delivery_receipt(
                    session_id=session_id,
                    owner_id=owner_id,
                    receipt=receipt,
                )
                destination_statuses.append(receipt["status"])

            # Determine outbox completion state
            if not destination_statuses:
                final_status = "completed"
            elif any(s == "unknown" for s in destination_statuses):
                final_status = "unknown"
            elif all(
                s in {"queued", "accepted", "delivered", "duplicate", "skipped"}
                for s in destination_statuses
            ):
                final_status = "completed"
            elif all(s == "failed" for s in destination_statuses):
                final_status = "failed"
            else:
                final_status = "completed"

            try:
                self.store.complete_delivery_outbox(
                    outbox_id=outbox_id,
                    lease_token=lease_token,
                    status=final_status,
                )
                results.append({"outbox_id": outbox_id, "status": final_status})
            except SessionConflict as exc:
                logger.warning("Delivery outbox lease conflict for %s: %s", outbox_id, exc)
                results.append({"outbox_id": outbox_id, "status": "stale_lease_conflict"})

        return results

    def _resolve_destinations(
        self,
        task: Mapping[str, Any],
        route: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Resolve destination endpoints for one outbox event."""
        destinations: list[dict[str, Any]] = []
        from orchestrator.frontend_connector_registry import endpoint_id_for

        primary = route.get("primary")
        if isinstance(primary, Mapping):
            surface = str(primary.get("surface") or "backend_api").casefold()
            channel = str(primary.get("channel_key") or "default")
            ep_id = endpoint_id_for(surface, ingress_transport=surface, channel_key=channel)
            destinations.append({"connector_id": surface, "endpoint_id": ep_id, "role": "primary"})

        for mirror in route.get("mirrors") or []:
            if isinstance(mirror, Mapping):
                surface = str(mirror.get("surface") or "telegram").casefold()
                channel = str(mirror.get("channel_key") or "default")
                ep_id = endpoint_id_for(surface, ingress_transport=surface, channel_key=channel)
                destinations.append({"connector_id": surface, "endpoint_id": ep_id, "role": "mirror"})

        if not destinations:
            # Fallback destination
            destinations.append(
                {
                    "connector_id": "session_api",
                    "endpoint_id": "session_api:default",
                    "role": "primary",
                }
            )

        return destinations


__all__ = [
    "FrontendDispatcher",
]
