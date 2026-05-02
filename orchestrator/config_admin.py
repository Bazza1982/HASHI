from __future__ import annotations

import json
from pathlib import Path

from orchestrator.pathing import BridgePaths


class ConfigAdmin:
    """Small wrapper for mutable agents.json operations."""

    def __init__(self, paths: BridgePaths):
        self.paths = paths

    def load_raw_config(self) -> dict:
        return json.loads(self.paths.config_path.read_text(encoding="utf-8-sig"))

    def write_raw_config(self, raw_cfg: dict) -> None:
        self.paths.config_path.write_text(
            json.dumps(raw_cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8-sig",
            newline="\r\n",
        )

    def get_all_agents_raw(self) -> list[dict]:
        raw = self.load_raw_config()
        return raw.get("agents", [])

    def set_agent_active(self, agent_name: str, active: bool) -> bool:
        """Toggle is_active for an agent. Returns True if found and updated."""
        raw = self.load_raw_config()
        for ag in raw.get("agents", []):
            if ag.get("name") == agent_name:
                ag["is_active"] = active
                self.write_raw_config(raw)
                return True
        return False

    def delete_agent_from_config(self, agent_name: str) -> bool:
        """Remove an agent entry from config. Returns True if found and removed."""
        raw = self.load_raw_config()
        agents = raw.get("agents", [])
        orig_len = len(agents)
        raw["agents"] = [ag for ag in agents if ag.get("name") != agent_name]
        if len(raw["agents"]) < orig_len:
            self.write_raw_config(raw)
            return True
        return False

    def add_agent_to_config(
        self,
        agent_name: str,
        agent_cfg: dict | str | None = None,
        token: str | None = None,
    ):
        """Add a flex agent scaffold.

        The original API returned bool for dict/None input. Some runtime command
        paths pass a display-name string and optional token and expect (ok, msg).
        Keep both shapes for compatibility.
        """
        wants_message = isinstance(agent_cfg, str) or token is not None
        display_name = agent_cfg if isinstance(agent_cfg, str) else agent_name

        raw = self.load_raw_config()
        existing_names = {ag.get("name") for ag in raw.get("agents", [])}
        if agent_name in existing_names:
            return (False, f"Agent '{agent_name}' already exists.") if wants_message else False

        ws_dir = self.paths.workspaces_root / agent_name
        ws_dir.mkdir(parents=True, exist_ok=True)

        agent_md = ws_dir / "AGENT.md"
        if not agent_md.exists():
            agent_md.write_text(f"# {agent_name}\n\nNew HASHI agent.\n", encoding="utf-8")

        if isinstance(agent_cfg, dict):
            new_entry = dict(agent_cfg)
        else:
            new_entry = {
                "display_name": display_name,
                "telegram_token_key": agent_name if not token else f"{agent_name}_telegram_token",
            }
        new_entry.setdefault("name", agent_name)
        new_entry.setdefault("workspace_dir", f"workspaces/{agent_name}")
        new_entry.setdefault("system_md", f"workspaces/{agent_name}/AGENT.md")
        new_entry.setdefault("is_active", True)
        new_entry.setdefault("type", "flex")

        raw.setdefault("agents", []).append(new_entry)
        self.write_raw_config(raw)
        if wants_message:
            suffix = ""
            if token:
                suffix = " Telegram token was not written to secrets.json; add it before Telegram use."
            return True, f"Added agent '{agent_name}'.{suffix}"
        return True

    def configured_agent_names(self) -> list[str]:
        raw = self.load_raw_config()
        return [agent["name"] for agent in raw.get("agents", []) if agent.get("is_active", True)]

    def get_startable_agent_names(
        self,
        running: set[str],
        starting: set[str],
        exclude_name: str | None = None,
    ) -> list[str]:
        return [
            name for name in self.configured_agent_names()
            if name not in running and name not in starting and name != exclude_name
        ]
