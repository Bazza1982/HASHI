"""Import host phase labels with service-observed model usage, never fake zero."""
from __future__ import annotations

from dataclasses import asdict, fields, replace
from hashlib import sha256
import json
import math

from adapters.base import TokenUsage
from adapters.her_v2_provider import HashiStageProvider
from tools.meter_cost import PerCallUsageLineItem
from tools.token_tracker import resolve_usage_cost


def accept_host_usage(provider, response):
    if not isinstance(provider, HashiStageProvider) or not isinstance(response, dict):
        raise ValueError("a real HERV3 accounting receiver is required")
    evidence, host = response.get("provider_evidence"), response.get("usage")
    if not isinstance(evidence, list) or not isinstance(host, list):
        raise ValueError("service-observed provider receipts are required")
    epoch = response.get("accounting_epoch")
    if not isinstance(epoch, str) or not epoch:
        raise ValueError("a service-bound accounting epoch is required")
    seen = dict(getattr(provider, "_permission_usage_seen", {}))
    counters = dict(getattr(provider, "_permission_usage_counters", {}))
    names = {field.name for field in fields(PerCallUsageLineItem)}
    labels = []
    for raw in host:
        if not isinstance(raw, dict) or set(raw) != names:
            raise ValueError("host usage fields changed")
        labels.append(PerCallUsageLineItem(**raw))
    counts = {}
    for name in ("tool_call_count", "tool_loop_count"):
        value = response.get(name)
        if type(value) is not int or value < 0:
            raise ValueError("invalid observed tool count")
        counts[name] = value
    pending = list(labels)
    imported, identities = [], set()
    for receipt in evidence:
        if not isinstance(receipt, dict) or receipt.get("status") not in {"completed", "unknown"}:
            raise ValueError("invalid observed model usage receipt")
        for key in ("id", "engine", "model"):
            if not isinstance(receipt.get(key), str) or not receipt[key]:
                raise ValueError("invalid observed invocation identity")
        ids = receipt.get("provider_ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("invalid observed provider identifiers")
        identity = receipt["id"]
        digest = sha256(json.dumps({key: value for key, value in receipt.items() if key != "projection"},
                                  sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if identity in identities or (identity in seen and seen[identity] != digest):
            raise ValueError("an observed invocation was duplicated or changed")
        identities.add(identity)
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if receipt.get(key) is not None and (type(receipt[key]) is not int or receipt[key] < 0):
                raise ValueError("invalid observed cache usage")
        for key in ("input_tokens", "output_tokens", "thinking_tokens"):
            if type(receipt.get(key)) is not int or receipt[key] < 0:
                raise ValueError("invalid observed token count")
        cost = receipt.get("cost_usd")
        if cost is not None and (type(cost) not in (float, int) or not math.isfinite(cost) or cost < 0):
            raise ValueError("invalid observed model cost")
        candidates = [item for item in pending if (item.engine, item.model) == (receipt["engine"], receipt["model"])]
        matching = next((item for item in candidates if item.provider_request_id in receipt["provider_ids"]), None)
        if matching is None and receipt.get("execution_epoch", epoch) == epoch:
            matching = candidates[0] if candidates else None
        if matching is not None:
            pending.remove(matching)
        else:
            matching = PerCallUsageLineItem(
                request_id="permission-provider:" + receipt["id"], phase="execution",
                engine=receipt["engine"], model=receipt["model"],
            )
        if receipt.get("projection") is not None:
            saved = receipt["projection"]
            if not isinstance(saved, dict) or set(saved) != names:
                raise ValueError("invalid saved canonical model projection")
            matching = PerCallUsageLineItem(**saved)
        source = receipt["token_source"]
        if source not in {"provider", "estimated", "unknown"}:
            raise ValueError("invalid observed token provenance")
        thinking_in_output = source == "provider"
        if source == "unknown" and cost is None:
            resolved, cost_source, revision = None, "unknown", "unknown"
        else:
            resolved, cost_source, revision = resolve_usage_cost(
                cost_usd=cost, model=receipt["model"], engine=receipt["engine"],
                input_tokens=receipt["input_tokens"], output_tokens=receipt["output_tokens"],
                thinking_tokens=receipt["thinking_tokens"],
                cached_tokens=receipt.get("prompt_cache_hit_tokens") or 0,
                thinking_in_output=thinking_in_output, schedule_missing=False,
            )
        imported.append((identity, digest, replace(
            matching, request_id="permission-provider:" + receipt["id"], input_tokens=receipt["input_tokens"], output_tokens=receipt["output_tokens"],
            thinking_tokens=receipt["thinking_tokens"], token_source=source,
            thinking_in_output=thinking_in_output, cost_usd=resolved, cost_source=cost_source,
            pricing_revision=revision, prompt_cache_hit_tokens=receipt.get("prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=receipt.get("prompt_cache_miss_tokens"),
            provider_request_id=(receipt["provider_ids"][0] if receipt["provider_ids"] else receipt["id"]),
            status="completed" if receipt["status"] == "completed" else "failed",
        )))
    if pending:
        raise ValueError("host claimed model invocations without service observations")
    # Validate the entire projection before notifying the durable accounting
    # owner. A transport retry cannot partially append a second set of rows.
    previous_counts = counters.get(epoch, {name: 0 for name in counts})
    if any(value < previous_counts[name] for name, value in counts.items()):
        raise ValueError("service-observed tool counters moved backwards")
    commit = getattr(provider, "_permission_projection_commit", None)
    if commit is not None:
        commit({identity: asdict(item) for identity, _, item in imported})
    for identity, digest, item in imported:
        if identity in seen:
            continue
        provider._notify_usage_observer(item)
        provider.usage_line_items.append(item)
        provider._accumulate_usage(TokenUsage(
            item.input_tokens, item.output_tokens, item.thinking_tokens,
            item.prompt_cache_hit_tokens, item.prompt_cache_miss_tokens,
        ))
        provider.cost_usd += float(item.cost_usd or 0)
        seen[identity] = digest
        provider._permission_usage_seen = dict(seen)
    for name, value in counts.items():
        setattr(provider, name, getattr(provider, name) + value - previous_counts[name])
    counters[epoch] = counts
    provider._permission_usage_counters = counters
