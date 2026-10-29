"""Durable accounting for the actual Worker-owned presentation/model ports."""
from __future__ import annotations

from dataclasses import asdict, fields
from hashlib import sha256
import json

from orchestrator.permissions.host_usage import accept_host_usage, canonical_item
from tools.meter_cost import PerCallUsageLineItem


class WorkerAccounting:
    def __init__(self, provider, client, binding):
        self.provider, self.client, self.binding = provider, client, binding
        previous = getattr(provider, "_permission_worker_accounting", None)
        self.original = previous.original if previous is not None else getattr(provider, "usage_observer", None)
        self.receipts, self.used = {}, set()
        provider._permission_worker_accounting = self
        provider.usage_observer = self.observe
        provider._permission_projection_commit = self.commit
        observed = self.read()
        for receipt in observed:
            if receipt.get("execution_epoch") == binding.context.execution_epoch:
                self.received(receipt)
        old = [receipt for receipt in observed if receipt.get("execution_epoch") != binding.context.execution_epoch]
        if old:
            counters = getattr(provider, "_permission_usage_counters", {}).get(binding.context.execution_epoch, {})
            accept_host_usage(provider, {
                "provider_evidence": old, "usage": [], "accounting_epoch": binding.context.execution_epoch,
                "tool_call_count": counters.get("tool_call_count", 0), "tool_loop_count": counters.get("tool_loop_count", 0),
            })

    def read(self):
        value = self.client.call("worker_usage_receipts", {"binding": self.binding})
        if not isinstance(value, list):
            raise ValueError("durable Worker model observations are unavailable")
        return value

    def commit(self, projections):
        return self.client.call("project_model_usage", {"binding": self.binding, "projections": projections})

    def received(self, receipt):
        if (not isinstance(receipt, dict) or receipt.get("origin") != "worker"
                or receipt.get("execution_epoch") != self.binding.context.execution_epoch):
            raise ValueError("Worker model receipt belongs to another execution")
        previous = self.receipts.get(receipt["id"])
        if previous is not None and previous != receipt:
            raise ValueError("Worker model observation changed")
        self.receipts[receipt["id"]] = receipt

    def observe(self, item):
        if item.request_id.startswith("permission-provider:"):
            if self.original is not None:
                self.original(item)
            return
        candidates = [receipt for identity, receipt in self.receipts.items()
                      if identity not in self.used and (receipt["engine"], receipt["model"]) == (item.engine, item.model)]
        if not candidates:
            for receipt in self.read():
                if receipt.get("execution_epoch") == self.binding.context.execution_epoch:
                    self.received(receipt)
            candidates = [receipt for identity, receipt in self.receipts.items()
                          if identity not in self.used and (receipt["engine"], receipt["model"]) == (item.engine, item.model)]
        if not candidates:
            # A confirmed absent transport intent is a pre-HTTP diagnostic.
            # It cannot become a fabricated physical Provider charge.
            item.token_source, item.cost_source, item.cost_usd = "unknown", "unknown", None
            if self.original is not None:
                self.original(item)
            return
        receipt = next((r for r in candidates if item.provider_request_id in r["provider_ids"]), candidates[0])
        projection = canonical_item(receipt, item)
        self.commit({receipt["id"]: asdict(projection)})
        # Keep the original HERV3 line object and its actual phase; the observer
        # receives canonical physical identity/counts before it writes anything.
        for field in fields(PerCallUsageLineItem):
            setattr(item, field.name, getattr(projection, field.name))
        if self.original is not None:
            self.original(item)
        self.used.add(receipt["id"])
        seen = dict(getattr(self.provider, "_permission_usage_seen", {}))
        seen[receipt["id"]] = sha256(json.dumps({k: v for k, v in receipt.items() if k != "projection"},
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.provider._permission_usage_seen = seen
