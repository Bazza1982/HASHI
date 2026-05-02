from __future__ import annotations

import logging
import shutil

from orchestrator.flexible_backend_registry import get_secret_lookup_order

main_logger = logging.getLogger("BridgeU.Orchestrator")


class BackendPreflight:
    """Determine which configured backends are available for startup."""

    def has_openrouter_api_key(self, agent_configs, secrets) -> bool:
        for cfg in agent_configs:
            for secret_key in get_secret_lookup_order("openrouter-api", getattr(cfg, "name", "")):
                if secrets.get(secret_key):
                    return True
        return False

    def check_backend_availability(self, global_cfg, agent_configs, secrets) -> dict[str, tuple[bool, str]]:
        """
        Check which backend engines are available. Returns {engine: (available, reason)}.

        CLI backends: check shutil.which().
        openrouter-api: check that the API key exists in secrets.
        """
        engines = set()
        for cfg in agent_configs:
            if hasattr(cfg, "allowed_backends"):  # flex agent
                for b in cfg.allowed_backends:
                    engines.add(b.get("engine", ""))
            engines.add(getattr(cfg, "engine", "") or getattr(cfg, "active_backend", ""))
        engines.discard("")

        result = {}
        cli_map = {
            "gemini-cli": global_cfg.gemini_cmd,
            "claude-cli": global_cfg.claude_cmd,
            "codex-cli": global_cfg.codex_cmd,
        }
        for engine in engines:
            if engine in cli_map:
                cmd = cli_map[engine]
                found = shutil.which(cmd)
                if found:
                    result[engine] = (True, found)
                else:
                    result[engine] = (False, f"'{cmd}' not found on PATH")
            elif engine == "openrouter-api":
                if self.has_openrouter_api_key(agent_configs, secrets):
                    result[engine] = (True, "API key present")
                else:
                    result[engine] = (False, "no API key in secrets.json")
            else:
                result[engine] = (True, "unknown engine, assuming available")
        return result

    def partition_agents_by_availability(
        self,
        agent_configs,
        engine_status: dict[str, tuple[bool, str]],
    ) -> tuple[list, list[tuple[str, str]]]:
        """
        Split agents into (startable_configs, skipped_list).
        skipped_list is [(agent_name, reason), ...].
        For flex agents: startable if active_backend is available OR any allowed_backend is.
        """
        startable = []
        skipped = []
        for cfg in agent_configs:
            if hasattr(cfg, "allowed_backends"):  # flex
                active_ok, _ = engine_status.get(cfg.active_backend, (False, "unknown"))
                if active_ok:
                    startable.append(cfg)
                    continue
                fallback = None
                for b in cfg.allowed_backends:
                    eng = b.get("engine", "")
                    ok, _ = engine_status.get(eng, (False, ""))
                    if ok:
                        fallback = eng
                        break
                if fallback:
                    main_logger.info(
                        "Flex agent '%s': active backend '%s' unavailable, will start with '%s' instead.",
                        cfg.name,
                        cfg.active_backend,
                        fallback,
                    )
                    cfg = type(cfg)(**{**cfg.__dict__, "active_backend": fallback})
                    startable.append(cfg)
                else:
                    reasons = [
                        f"{b.get('engine')}: {engine_status.get(b.get('engine', ''), (False, '?'))[1]}"
                        for b in cfg.allowed_backends
                    ]
                    skipped.append((cfg.name, f"no available backend ({', '.join(reasons)})"))
            else:  # fixed
                engine = cfg.engine
                ok, reason = engine_status.get(engine, (False, "unknown engine"))
                if ok:
                    startable.append(cfg)
                else:
                    skipped.append((cfg.name, f"{engine}: {reason}"))
        return startable, skipped
