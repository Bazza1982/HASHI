from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from orchestrator.bridge_protocol import required_scope_for_intent
from orchestrator.pathing import resolve_bridge_home, resolve_path_value


class AgentDirectory:
    def __init__(self, config_path: Path, capabilities_path: Path, runtimes: list[Any]):
        self.config_path = config_path
        self.capabilities_path = capabilities_path
        self.runtimes = runtimes
        self._agent_rows: dict[str, dict[str, Any]] = {}
        self._capabilities: dict[str, dict[str, Any]] = {}
        self.refresh()

    def refresh(self) -> None:
        raw_cfg = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        self._agent_rows = {
            row["name"]: row
            for row in raw_cfg.get("agents", [])
            if row.get("is_active", True)
        }
        self._groups: dict[str, dict] = raw_cfg.get("groups", {})

        # Handle missing capabilities file gracefully
        if self.capabilities_path.exists():
            raw_caps = json.loads(self.capabilities_path.read_text(encoding="utf-8-sig"))
        else:
            raw_caps = {"agents": {}}
        entries = raw_caps.get("agents", raw_caps)
        self._capabilities = {}
        if isinstance(entries, list):
            for entry in entries:
                name = str(entry.get("name") or "").strip()
                if name:
                    self._capabilities[name] = entry

    def runtime_map(self) -> dict[str, Any]:
        return {runtime.name: runtime for runtime in self.runtimes}

    def agent_exists(self, name: str) -> bool:
        return name in self._agent_rows

    def get_agent_row(self, name: str) -> dict[str, Any] | None:
        return self._agent_rows.get(name)

    def get_capability(self, name: str) -> dict[str, Any] | None:
        return self._capabilities.get(name)

    def get_runtime(self, name: str) -> Any | None:
        return self.runtime_map().get(name)

    def runtime_metadata(self, name: str) -> dict[str, Any] | None:
        runtime = self.get_runtime(name)
        if runtime is not None:
            return runtime.get_runtime_metadata()

        row = self.get_agent_row(name)
        if row is None:
            return None

        bridge_home = resolve_bridge_home(self.config_path.parent)
        workspace_dir = resolve_path_value(
            row["workspace_dir"],
            config_dir=self.config_path.parent,
            bridge_home=bridge_home,
        ) or (self.config_path.parent / row["workspace_dir"])
        transcript_name = "conversation_log.jsonl" if row.get("type") == "fixed" else "transcript.jsonl"
        engine = row.get("engine") or row.get("active_backend", "unknown")
        model = row.get("model", "unknown")
        if row.get("type") == "flex":
            for backend in row.get("allowed_backends", []):
                if backend.get("engine") == row.get("active_backend"):
                    model = backend.get("model", model)
                    break

        return {
            "id": name,
            "name": name,
            "display_name": row.get("display_name", name),
            "emoji": row.get("emoji", "🤖"),
            "engine": engine,
            "model": model,
            "workspace_dir": str(workspace_dir),
            "transcript_path": str(workspace_dir / transcript_name),
            "online": False,
            "status": "offline",
            "type": row.get("type", "unknown"),
        }

    def capability_view(self, name: str) -> dict[str, Any] | None:
        capability = self.get_capability(name)
        if capability is None:
            return None
        metadata = self.runtime_metadata(name) or {}
        return {
            "name": name,
            "can_talk_to": capability.get("can_talk_to", []),
            "can_receive_from": capability.get("can_receive_from", []),
            "allowed_incoming_intents": capability.get("allowed_incoming_intents", []),
            "granted_scopes": capability.get("granted_scopes", []),
            "tags": capability.get("tags", []),
            "runtime": {
                "online": metadata.get("online", False),
                "status": metadata.get("status", "offline"),
                "engine": metadata.get("engine", "unknown"),
                "model": metadata.get("model", "unknown"),
            },
        }

    # ── Group management ──────────────────────────────────────────────────────

    def list_groups(self) -> dict[str, dict]:
        """Return all group definitions."""
        return dict(self._groups)

    def resolve_group(self, name: str, exclude_self: str | None = None) -> list[str]:
        """Resolve a group name to a list of agent names.

        Supports the magic keyword '@active' as members value — dynamically
        returns all currently running agents minus any exclude_from_broadcast entries.
        Returns [] if group not found.
        """
        group = self._groups.get(name)
        if group is None:
            return []
        members = group.get("members", [])
        excludes = {e.lower() for e in group.get("exclude_from_broadcast", [])}
        if exclude_self:
            excludes.add(exclude_self.lower())

        if members == "@active":
            # Dynamic: all currently running agents
            active = list(self.runtime_map().keys())
            return [n for n in active if n.lower() not in excludes]
        else:
            return [n for n in members if n.lower() not in excludes]

    def group_exists(self, name: str) -> bool:
        return name in self._groups

    def _save_groups(self) -> None:
        """Persist groups back to agents.json."""
        raw_cfg = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        raw_cfg["groups"] = self._groups
        self.config_path.write_text(
            json.dumps(raw_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create_group(self, name: str, description: str = "") -> tuple[bool, str]:
        if name in self._groups:
            return False, f"Group '{name}' already exists."
        self._groups[name] = {"description": description, "members": [], "exclude_from_broadcast": []}
        self._save_groups()
        return True, f"Group '{name}' created."

    def delete_group(self, name: str) -> tuple[bool, str]:
        if name not in self._groups:
            return False, f"Group '{name}' not found."
        del self._groups[name]
        self._save_groups()
        return True, f"Group '{name}' deleted."

    def group_add_member(self, group_name: str, agent_name: str) -> tuple[bool, str]:
        if group_name not in self._groups:
            return False, f"Group '{group_name}' not found."
        grp = self._groups[group_name]
        if grp.get("members") == "@active":
            return False, "Cannot manually edit a dynamic group (@active)."
        members: list = grp.setdefault("members", [])
        if agent_name in members:
            return False, f"'{agent_name}' is already in group '{group_name}'."
        members.append(agent_name)
        self._save_groups()
        return True, f"Added '{agent_name}' to group '{group_name}'."

    def group_remove_member(self, group_name: str, agent_name: str) -> tuple[bool, str]:
        if group_name not in self._groups:
            return False, f"Group '{group_name}' not found."
        grp = self._groups[group_name]
        if grp.get("members") == "@active":
            return False, "Cannot manually edit a dynamic group (@active)."
        members: list = grp.get("members", [])
        if agent_name not in members:
            return False, f"'{agent_name}' is not in group '{group_name}'."
        members.remove(agent_name)
        self._save_groups()
        return True, f"Removed '{agent_name}' from group '{group_name}'."

    def group_rename(self, old_name: str, new_name: str) -> tuple[bool, str]:
        if old_name not in self._groups:
            return False, f"Group '{old_name}' not found."
        if new_name in self._groups:
            return False, f"Group '{new_name}' already exists."
        self._groups[new_name] = self._groups.pop(old_name)
        self._save_groups()
        return True, f"Group renamed '{old_name}' → '{new_name}'."

    def check_permission(self, message: dict[str, Any]) -> tuple[bool, str]:
        from_agent = message["from_agent"]
        to_agent = message["to_agent"]
        intent = message["intent"]

        if not self.agent_exists(from_agent):
            return False, f"sender does not exist: {from_agent}"
        if not self.agent_exists(to_agent):
            return False, f"target does not exist: {to_agent}"

        sender_cap = self.get_capability(from_agent)
        target_cap = self.get_capability(to_agent)
        if sender_cap is None:
            return False, f"sender capability missing: {from_agent}"
        if target_cap is None:
            return False, f"target capability missing: {to_agent}"

        if to_agent not in sender_cap.get("can_talk_to", []):
            return False, f"sender {from_agent} may not contact {to_agent}"
        if from_agent not in target_cap.get("can_receive_from", []):
            return False, f"target {to_agent} may not receive from {from_agent}"
        if intent not in target_cap.get("allowed_incoming_intents", []):
            return False, f"target {to_agent} does not accept intent {intent}"

        required_scope = required_scope_for_intent(intent)
        if required_scope not in sender_cap.get("granted_scopes", []):
            return False, f"sender {from_agent} lacks scope {required_scope}"

        runtime = self.get_runtime(to_agent)
        if runtime is None or not getattr(runtime, "startup_success", False):
            return False, f"target runtime is offline: {to_agent}"

        return True, "allowed"

    def check_reply_permission(self, reply: dict[str, Any], request_message: dict[str, Any]) -> tuple[bool, str]:
        from_agent = reply["from_agent"]
        to_agent = reply["to_agent"]

        if request_message.get("kind") != "request":
            return False, f"cannot reply to non-request message: {request_message.get('message_id')}"
        if request_message.get("thread_id") != reply.get("thread_id"):
            return False, "reply thread_id does not match original request"
        if request_message.get("from_agent") != to_agent:
            return False, "reply target does not match original sender"
        if request_message.get("to_agent") != from_agent:
            return False, "reply sender does not match original target"

        sender_cap = self.get_capability(from_agent)
        target_cap = self.get_capability(to_agent)
        if sender_cap is None:
            return False, f"sender capability missing: {from_agent}"
        if target_cap is None:
            return False, f"target capability missing: {to_agent}"

        if to_agent not in sender_cap.get("can_talk_to", []):
            return False, f"sender {from_agent} may not contact {to_agent}"
        if from_agent not in target_cap.get("can_receive_from", []):
            return False, f"target {to_agent} may not receive from {from_agent}"

        return True, "allowed"
