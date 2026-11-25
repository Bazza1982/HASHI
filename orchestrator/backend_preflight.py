from __future__ import annotations

import logging
from pathlib import Path
import shutil

from adapters.xai_oauth_credentials import (
    find_hermes_auth_path,
    xai_api_credentials_available,
)
from orchestrator.pathing import resolve_agy_executable, resolve_dsh_executable, resolve_dsh_invocation
from orchestrator.flexible_backend_registry import (
    canonical_backend_engine,
    get_secret_lookup_order,
    removed_backend_reason,
)

main_logger = logging.getLogger("BridgeU.Orchestrator")


class BackendPreflight:
    """Determine which configured backends are available for startup."""

    def has_xai_api_credentials(self, global_cfg, agent_configs, secrets) -> bool:
        hermes_home = str(getattr(global_cfg, "hermes_home", "") or "").strip() or None
        if xai_api_credentials_available(hermes_home=hermes_home, secrets=secrets):
            return True
        for cfg in agent_configs:
            for secret_key in get_secret_lookup_order("xai-api", getattr(cfg, "name", "")):
                if secrets.get(secret_key):
                    return True
        return False

    def has_openrouter_api_key(self, agent_configs, secrets) -> bool:
        for cfg in agent_configs:
            for secret_key in get_secret_lookup_order("openrouter-api", getattr(cfg, "name", "")):
                if secrets.get(secret_key):
                    return True
        return False

    def _find_secret(self, engine: str, agent_configs, secrets) -> str:
        """Return the first configured secret value for ``engine``, if any."""
        for cfg in agent_configs:
            for secret_key in get_secret_lookup_order(engine, getattr(cfg, "name", "")):
                value = secrets.get(secret_key)
                if value:
                    return str(value)
        return ""

    @staticmethod
    def _engine_declares_no_secret(engine: str) -> bool:
        """Whether the registry deliberately declares no secret for ``engine``.

        An empty ``secret_keys`` list means the engine authenticates elsewhere
        (local runtime, CLI keyring, per-agent provider row). It must not be
        read as "missing API key", which would disable the engine forever.
        """
        return not get_secret_lookup_order(engine, "")

    @staticmethod
    def _probe_http(url: str, timeout: float = 1.0) -> tuple[bool, str]:
        """Best-effort reachability probe with a hard, short timeout."""
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return 200 <= int(response.status) < 500, f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            # An HTTP error still proves the endpoint is reachable.
            return True, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - report the real reason
            return False, f"{type(exc).__name__}: {exc}"

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
            "claude-cli": global_cfg.claude_cmd,
            "codex-cli": global_cfg.codex_cmd,
            "antigravity-cli": resolve_agy_executable(
                getattr(global_cfg, "agy_cmd", "agy")
            ),
            "grok-cli": getattr(global_cfg, "grok_cmd", "grok"),
            "dsh-cli": resolve_dsh_executable(getattr(global_cfg, "dsh_cmd", "dsh")),
        }
        agy_launch_mode = str(
            getattr(global_cfg, "agy_launch_mode", "direct") or "direct"
        ).strip().casefold()
        for engine in engines:
            if reason := removed_backend_reason(engine):
                result[engine] = (False, reason)
            elif engine in cli_map:
                cmd = cli_map[engine]
                if engine == "dsh-cli":
                    argv, _cwd, label = resolve_dsh_invocation(cmd)
                    exe_found = shutil.which(argv[0]) if argv else None
                    script_ok = (
                        len(argv) >= 2
                        and Path(label).is_file()
                        and exe_found is not None
                    )
                    if script_ok or (exe_found and not Path(label).suffix.lower() in {".js", ".ts", ".mjs", ".cjs", ".mts"}):
                        found = label or exe_found
                    else:
                        found = None
                else:
                    found = shutil.which(cmd)
                if not found:
                    result[engine] = (False, f"'{cmd}' not found on PATH")
                elif engine == "antigravity-cli" and agy_launch_mode == "user-session":
                    from adapters.agy_user_session_launcher import (
                        LAUNCHER_SCRIPT_PATH,
                    )

                    if not LAUNCHER_SCRIPT_PATH.is_file():
                        result[engine] = (
                            False,
                            "user-session launcher script missing "
                            f"({LAUNCHER_SCRIPT_PATH})",
                        )
                    else:
                        result[engine] = (
                            True,
                            f"{found} via user-session launcher",
                        )
                else:
                    result[engine] = (True, found)
            elif engine == "xai-api":
                if self.has_xai_api_credentials(global_cfg, agent_configs, secrets):
                    auth_path = find_hermes_auth_path(
                        str(getattr(global_cfg, "hermes_home", "") or "").strip() or None
                    )
                    if auth_path is not None:
                        result[engine] = (True, f"Hermes OAuth available ({auth_path})")
                    else:
                        result[engine] = (True, "xAI API key present")
                else:
                    result[engine] = (
                        False,
                        "no Hermes xai-oauth and no xai_api_key in secrets.json",
                    )
            elif engine == "openrouter-api":
                if self.has_openrouter_api_key(agent_configs, secrets):
                    result[engine] = (True, "API key present")
                else:
                    result[engine] = (False, "no API key in secrets.json")
            elif engine in {"deepseek-api", "hashi-api", "openai-compatible-api", "hli-api"}:
                if self._engine_declares_no_secret(engine):
                    result[engine] = (
                        True,
                        "no API key required by this engine",
                    )
                elif key := self._find_secret(engine, agent_configs, secrets):
                    result[engine] = (True, "API key present")
                else:
                    result[engine] = (
                        False,
                        f"no API key in secrets.json for {engine}",
                    )
            elif engine == "ollama-api":
                base_url = str(
                    getattr(global_cfg, "ollama_base_url", "")
                    or "http://127.0.0.1:11434"
                ).strip()
                alive, detail = self._probe_http(f"{base_url.rstrip('/')}/api/tags")
                result[engine] = (
                    (True, f"Ollama reachable at {base_url}") if alive
                    else (False, f"Ollama not reachable at {base_url}: {detail}")
                )
            elif canonical_backend_engine(engine) == "her-v2":
                # ``her-v2`` is the stored identifier of the live HER runtime,
                # not a removable engine. Every Agent row resolves its own
                # provider and credentials, so availability is decided per row;
                # a blanket False here would stop every HER Agent from starting.
                result[engine] = (
                    True,
                    "HER runtime: availability is decided by the Agent's configured provider row",
                )
            else:
                result[engine] = (
                    False,
                    f"unknown engine '{engine}': no availability check implemented",
                )
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
# HASHI-Fingerprint: hfp_f67303142f2e4bf989d56bcfa4c688b2
