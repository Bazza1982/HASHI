"""Authorised local and consolidated HASHI memory search."""

from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.bridge_memory import BgeM3Encoder, BridgeMemoryStore


class MemorySearchAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class MemorySearchScope:
    instance_id: str
    agent_id: str
    cross_agent: bool
    purpose: str


class CentralMemorySearch:
    def __init__(
        self,
        config: dict[str, Any] | None,
        *,
        current_instance: str,
        current_agent: str,
    ):
        self.config = dict(config or {})
        self.current_instance = str(current_instance)
        self.current_agent = str(current_agent)

    @property
    def configured(self) -> bool:
        return bool(self.config.get("database_path"))

    def resolve_scope(self, arguments: dict[str, Any]) -> MemorySearchScope:
        cross = str(arguments.get("scope") or "current_agent") == "cross_agent"
        instance = str(arguments.get("instance_id") or self.current_instance).strip()
        agent = str(arguments.get("agent_id") or self.current_agent).strip()
        purpose = str(arguments.get("purpose") or "").strip()
        if not cross:
            if instance != self.current_instance or agent != self.current_agent:
                raise MemorySearchAuthorizationError(
                    "current_agent scope cannot select another instance or Agent"
                )
            return MemorySearchScope(instance, agent, False, purpose)
        authorization = arguments.get("_trusted_authorization")
        if not isinstance(authorization, dict) or authorization.get("authorization") != "explicit_user_authorization":
            raise MemorySearchAuthorizationError(
                "cross-Agent raw memory search requires a HASHI-bound explicit user authorization"
            )
        bound_instance = str(authorization.get("instance_id") or "").strip()
        bound_agent = str(authorization.get("agent_id") or "").strip()
        bound_purpose = str(authorization.get("purpose") or "").strip()
        if (instance, agent) != (bound_instance, bound_agent):
            raise MemorySearchAuthorizationError(
                "cross-Agent raw memory target does not match the bound user authorization"
            )
        if purpose and purpose != bound_purpose:
            raise MemorySearchAuthorizationError(
                "cross-Agent raw memory purpose does not match the bound user authorization"
            )
        purpose = bound_purpose
        if not purpose:
            raise MemorySearchAuthorizationError(
                "cross-Agent raw memory search requires an auditable purpose"
            )
        if not instance or not agent:
            raise MemorySearchAuthorizationError(
                "cross-Agent raw memory search requires exact instance_id and agent_id"
            )
        return MemorySearchScope(instance, agent, True, purpose)

    @staticmethod
    def _decode_embedding(value: Any) -> list[float]:
        if isinstance(value, bytes) and len(value) % 4 == 0:
            return list(struct.unpack(f"{len(value) // 4}f", value))
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return [float(item) for item in parsed] if isinstance(parsed, list) else []
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        return []

    def search(
        self,
        query: str,
        *,
        scope: MemorySearchScope,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not self.configured:
            return []
        database = Path(str(self.config["database_path"])).expanduser()
        if not database.is_file():
            return []
        encoder = BgeM3Encoder(
            model_dir=self.config.get("model_dir"),
            tokenizer_dir=self.config.get("tokenizer_dir"),
        )
        q_vec = encoder.encode(query)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(consolidated)").fetchall()
            }
            required = {"id", "instance", "agent_id", "content"}
            if not required.issubset(columns):
                raise RuntimeError("central memory schema lacks required consolidated columns")
            selected = [
                name
                for name in (
                    "id",
                    "instance",
                    "agent_id",
                    "content",
                    "source_ts",
                    "source",
                    "memory_type",
                    "importance",
                    "embedding",
                )
                if name in columns
            ]
            rows = connection.execute(
                f"SELECT {', '.join(selected)} FROM consolidated "
                "WHERE instance = ? AND agent_id = ? ORDER BY id DESC LIMIT 500",
                (scope.instance_id, scope.agent_id),
            ).fetchall()

        query_folded = query.casefold().strip()
        scored: list[tuple[float, dict[str, Any]]] = []
        for raw in rows:
            row = dict(raw)
            content = str(row.get("content") or "")
            embedding = self._decode_embedding(row.get("embedding"))
            vector_score = (
                max(0.0, encoder.cosine(q_vec, embedding))
                if q_vec and embedding and len(q_vec) == len(embedding)
                else 0.0
            )
            text_score = 1.0 if query_folded and query_folded in content.casefold() else 0.0
            importance_score = max(
                0.0, min(1.0, float(row.get("importance") or 1.0))
            )
            score = 0.70 * vector_score + 0.20 * text_score + 0.10 * importance_score
            result = {
                "record_id": row.get("id"),
                "instance_id": row.get("instance"),
                "agent_id": row.get("agent_id"),
                "content": content,
                "timestamp": row.get("source_ts"),
                "source": row.get("source") or "consolidated",
                "memory_type": row.get("memory_type") or "raw_consolidated",
                "score": score,
                "vector_score": vector_score,
                "text_score": text_score,
                "importance_score": importance_score,
                "provenance": {
                    "store": "central_consolidated",
                    "database_fingerprint": __import__("hashlib").sha256(
                        str(database.resolve()).encode("utf-8")
                    ).hexdigest(),
                    "scope": {
                        "instance_id": scope.instance_id,
                        "agent_id": scope.agent_id,
                        "cross_agent": scope.cross_agent,
                        "purpose": scope.purpose,
                    },
                },
            }
            scored.append((score, result))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [result for _score, result in scored[:limit]]


class MemorySearchService:
    def __init__(
        self,
        *,
        workspace_dir: Path,
        global_config: Any,
        agent_id: str,
        trusted_authorization: dict[str, Any] | None = None,
    ):
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.global_config = global_config
        self.agent_id = str(agent_id)
        self.trusted_authorization = dict(trusted_authorization or {})
        self.instance_id = str(getattr(global_config, "instance_id", "HASHI"))
        self.central = CentralMemorySearch(
            getattr(global_config, "central_memory", None),
            current_instance=self.instance_id,
            current_agent=self.agent_id,
        )

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("memory_search query is required")
        limit = max(1, min(20, int(arguments.get("limit") or 6)))
        source = str(arguments.get("source") or "all").strip().lower()
        if source not in {"local", "central", "all"}:
            raise ValueError("memory_search source must be local, central, or all")
        scoped_arguments = dict(arguments)
        # Tool arguments are untrusted model output. Only the service's
        # request-bound constructor input may create this internal authority.
        scoped_arguments.pop("_trusted_authorization", None)
        if self.trusted_authorization:
            scoped_arguments["_trusted_authorization"] = dict(
                self.trusted_authorization
            )
        scope = self.central.resolve_scope(scoped_arguments)
        results: list[dict[str, Any]] = []
        if source in {"local", "all"} and not scope.cross_agent:
            local = BridgeMemoryStore(self.workspace_dir).retrieve_memories(
                query, limit=limit, now=datetime.now(timezone.utc)
            )
            results.extend(
                {
                    "record_id": item.get("id"),
                    "instance_id": self.instance_id,
                    "agent_id": self.agent_id,
                    "content": item.get("content"),
                    "timestamp": item.get("ts"),
                    "source": item.get("source"),
                    "memory_type": item.get("memory_type"),
                    "score": item.get("score"),
                    "vector_score": item.get("vector_score"),
                    "text_score": item.get("text_score"),
                    "importance_score": item.get("importance_score"),
                    "recency_score": item.get("recency_score"),
                    "provenance": {
                        "store": "agent_local",
                        "workspace_agent": self.agent_id,
                    },
                }
                for item in local
            )
        central_status = "not_requested"
        if source in {"central", "all"}:
            central_status = "configured" if self.central.configured else "unconfigured"
            results.extend(self.central.search(query, scope=scope, limit=limit))
        results.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return {
            "query": query,
            "scope": {
                "instance_id": scope.instance_id,
                "agent_id": scope.agent_id,
                "cross_agent": scope.cross_agent,
                "purpose": scope.purpose,
            },
            "central_status": central_status,
            "result_count": min(limit, len(results)),
            "results": results[:limit],
        }
