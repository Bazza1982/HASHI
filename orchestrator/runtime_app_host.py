"""Shared Functions process: PAO services and Frontend Connectors.

The independent Core owns only this process's lifetime. Per-Agent rollouts
retain the existing stable handles and isolated worker transaction.
"""

from __future__ import annotations

import asyncio
import argparse
import importlib
import os
from pathlib import Path

from orchestrator.function_generation import candidate_import_guard
from orchestrator.function_contract import validate_function_contract
from orchestrator.function_worker_protocol import JsonConnectionPeer
from orchestrator.kernel_artifact import verify_artifact
from orchestrator import runtime_handoff


CONNECTOR_RETRY_SECONDS = 5


class RuntimeAppHost:
    def __init__(self, peer, bootstrap):
        self.peer, self.bootstrap = peer, bootstrap
        self.app = None
        self.task = None
        self.stopping = asyncio.Event()
        self.quiesced = []
        self.ingress = []
        self.whatsapp_paused = False
        self.connector_task = None
        self.connector_lock = asyncio.Lock()
        self.bootstrapped_agents = set()
        self.onboarding_paths = None
        self.onboarding_done = False

    async def prepare(self):
        manifest = self.bootstrap["manifest"]
        with candidate_import_guard():
            for entry in manifest["entries"]:
                importlib.import_module(entry["module"])
            validate_function_contract()
        verify_artifact(Path(self.bootstrap["generation_root"]), manifest)
        from orchestrator.pathing import build_bridge_paths
        from orchestrator.runtime_app import UniversalOrchestrator
        from orchestrator.bootstrap_logging import configure_terminal_console

        paths = build_bridge_paths(
            Path(self.bootstrap["code_root"]),
            self.bootstrap["bridge_home"],
            canonical_home=True,
        )
        configure_terminal_console(paths.bridge_home)
        from orchestrator.onboarding_gate import needs_onboarding

        if needs_onboarding(paths):
            if not self.bootstrap.get("initial"):
                raise RuntimeError(
                    "Candidate has no configured Agents; onboarding is a cold-start action"
                )
            self.onboarding_paths = paths
            return {
                "pid": os.getpid(),
                "generation_id": manifest["generation_id"],
                "interactive": True,
            }
        args = self.bootstrap.get("arguments", {})
        if "argv" in args:
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--agents", nargs="*")
            parser.add_argument("--api-gateway", action="store_true")
            try:
                args = vars(parser.parse_args(args["argv"]))
            except SystemExit as exc:
                raise ValueError("invalid Function command-line options") from exc
        agents = args.get("agents")
        self.app = UniversalOrchestrator(
            paths,
            selected_agents=set(agents) if agents else None,
            enable_api_gateway=bool(args.get("api_gateway")),
        )
        # Candidate config validation does not initialize providers or bind ports.
        self.app._load_config_bundle()
        self.app.kernel_pid = self.bootstrap["kernel_pid"]
        self.app.shared_generation_id = manifest["generation_id"]
        from orchestrator.function_worker_supervisor import generation_from_dict

        self.app._startup_artifact = (
            generation_from_dict(
                {
                    "code_root": self.bootstrap["code_root"],
                    "manifest": manifest,
                    "receipt": {
                        "generation_id": manifest["generation_id"],
                        "module_names": [e["module"] for e in manifest["entries"]],
                        "runtime": self.bootstrap["runtime"],
                        "probe_pid": 0,
                    },
                }
            ),
            Path(self.bootstrap["generation_root"]),
        )
        self.app._handoff_draining = True
        # This process is supervised; it must never exit around IPC cleanup.
        self.app.shutdown_manager.start_exit_watchdog = lambda: None
        return {
            "pid": os.getpid(),
            "generation_id": manifest["generation_id"],
            "project_root": str(self.app.global_cfg.project_root),
            "bridge_home": str(paths.bridge_home),
        }

    async def activate(self, handoff):
        if self.onboarding_paths is not None:
            from orchestrator.onboarding_gate import run_onboarding_gate

            await asyncio.to_thread(
                run_onboarding_gate,
                self.onboarding_paths,
                Path(self.bootstrap["code_root"]),
                terminal=self.bootstrap.get("stdin_terminal"),
            )
            self.onboarding_done = True
            return {"exit_requested": True}
        if self.app is None or self.task is not None:
            raise RuntimeError("shared Functions are not prepared or already active")
        handoff = handoff or self.bootstrap.get("handoff", {})
        if self.bootstrap.get("recovery"):
            handoff = runtime_handoff.load(self.app)
        if self.bootstrap.get("recovery") or self.bootstrap.get("restore"):
            self.app._startup_agent_artifacts = runtime_handoff.agent_artifacts(
                self.app, handoff
            )
        if "agents" in handoff:
            self.app._allow_empty_start = not handoff["agents"]
            self.app.selected_agents = set(handoff["agents"])
        self.app._handoff_offsets = handoff.get("telegram_offsets", {})
        if self.bootstrap.get("recovery"):
            for name in handoff["agents"]:
                self.app._handoff_offsets.setdefault(name, None)
        self.task = asyncio.create_task(self.app.run(), name="shared-functions")
        while not self.task.done():
            status = self.app.startup_status
            if status.get("services_ready"):
                if "agents" in handoff:
                    missing = set(handoff["agents"]) - {
                        rt.name for rt in self.app.runtimes
                    }
                    if missing:
                        raise RuntimeError(
                            f"replacement failed to restore agents: {sorted(missing)}"
                        )
                for name in handoff.get("services", []):
                    if getattr(self.app, name, None) is None:
                        raise RuntimeError(
                            f"replacement failed to restore service: {name}"
                        )
                return {"pid": os.getpid(), "status": status["phase"]}
            await asyncio.sleep(0.1)
        await self.task
        raise RuntimeError("shared Functions stopped before services became ready")

    async def _activate_connectors(self):
        app = self.app
        errors = {}

        async def activate_ingress(name, ingress):
            try:
                await asyncio.wait_for(
                    ingress.start(
                        drop_pending_updates=getattr(
                            ingress, "drop_pending_on_start", False
                        )
                    ),
                    timeout=45,
                )
                if name not in self.bootstrapped_agents:
                    handle = app._runtime_map().get(name)
                    if handle is not None:
                        await handle.enqueue_startup_bootstrap(
                            app.global_cfg.authorized_id
                        )
                    self.bootstrapped_agents.add(name)
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"

        async with self.connector_lock:
            await asyncio.gather(
                *(
                    activate_ingress(name, ingress)
                    for name, ingress in app.function_workers._telegram_ingress.items()
                )
            )
            try:
                _, whatsapp = app._load_whatsapp_cfg()
                if whatsapp.get("enabled") and app.whatsapp is None:
                    ok, message = await app.start_whatsapp_transport(
                        persist_enabled=False
                    )
                    if not ok:
                        errors["whatsapp"] = message
            except Exception as exc:
                errors["whatsapp"] = str(exc)
            status = dict(app.startup_status)
            issues = [
                issue
                for issue in status.get("issues", [])
                if issue.get("code") != "connector_activation"
            ]
            if errors:
                issues.append(
                    {
                        "code": "connector_activation",
                        "severity": "warning",
                        "summary": "Connectors are retrying activation",
                        "details": errors,
                        "automatic_retry": True,
                    }
                )
            degraded = bool(
                status.get("failed_agents")
                or any(
                    issue.get("severity") in {"warning", "error", "critical"}
                    for issue in issues
                )
            )
            status.update(
                issues=issues,
                degraded=degraded,
                ready=not degraded,
                phase="degraded" if degraded else "ready",
            )
            app.startup_status = status
        return errors

    async def _retry_connectors(self):
        while not self.stopping.is_set() and not self.app._handoff_draining:
            await asyncio.sleep(CONNECTOR_RETRY_SECONDS)
            if self.app._handoff_draining:
                return
            if not await self._activate_connectors():
                runtime_handoff.persist(self.app)
                return

    async def commit(self):
        if self.onboarding_done:
            await self.peer.emit("application.stopped", {})
            asyncio.get_running_loop().call_later(0.1, self.stopping.set)
            return {"exit_requested": True}
        app = self.app
        if self.task is None or self.task.done():
            raise RuntimeError("shared Functions are not active")
        app._handoff_draining = False
        app._shared_committed = True
        if app.api_gateway:
            app.api_gateway._accepting_requests = True
        # Checkpoint before opening external connectors: recovery must know the
        # exact installed set even if a connector crashes during activation.
        runtime_handoff.persist(app)
        errors = await self._activate_connectors()
        if errors and (self.connector_task is None or self.connector_task.done()):
            self.connector_task = asyncio.create_task(
                self._retry_connectors(), name="connector-activation-retry"
            )
        runtime_handoff.persist(app)
        return {"committed": True, "degraded": bool(errors), "connector_errors": errors}

    async def quiesce(self):
        app = self.app
        if (
            self.task is None
            or self.task.done()
            or app._restart_request is not None
            or getattr(getattr(app, "reboot_manager", None), "active_operation", None)
            or any(not task.done() for task in app._startup_tasks.values())
        ):
            raise RuntimeError("shared Functions cannot drain in this lifecycle phase")
        app._handoff_draining = True
        if self.connector_task is not None:
            self.connector_task.cancel()
            await asyncio.gather(self.connector_task, return_exceptions=True)
        try:
            # Stop new external intake, retain in-flight work until it completes.
            if app.whatsapp is not None:
                ok, message = await app.stop_whatsapp_transport(persist_enabled=False)
                if not ok:
                    raise RuntimeError(message)
                self.whatsapp_paused = True
            gateway = app.api_gateway
            if gateway:
                gateway._accepting_requests = False
            self.ingress = list(app.function_workers._telegram_ingress.values())
            pauses = await asyncio.gather(
                *(ingress.pause() for ingress in self.ingress), return_exceptions=True
            )
            failures = [item for item in pauses if isinstance(item, BaseException)]
            if failures:
                raise RuntimeError(f"Telegram intake did not drain: {failures[0]}")
            # Both background jobs and API generations own real side effects;
            # never cancel them just to make a rollout appear successful.
            async with asyncio.timeout(120):
                while (
                    getattr(app, "_handoff_requests", 0)
                    or (gateway and gateway._active_requests)
                    or any(
                        not task.done()
                        for task in getattr(
                            getattr(app, "background_job_manager", None),
                            "_monitor_tasks",
                            {},
                        ).values()
                    )
                ):
                    await asyncio.sleep(0.1)
                for handle in app.runtimes:
                    old = await handle.begin_cutover()
                    self.quiesced.append((handle, old))
                    await old.call("worker.quiesce", {"timeout": 120}, timeout=130)
                while any(
                    not task.done()
                    for task in getattr(
                        getattr(app, "background_job_manager", None),
                        "_monitor_tasks",
                        {},
                    ).values()
                ):
                    await asyncio.sleep(0.1)
            return runtime_handoff.snapshot(app)
        except BaseException:
            await self.resume()
            raise

    async def resume(self):
        failures = []
        for handle, old in self.quiesced:
            try:
                status = await old.call("worker.ping", timeout=10)
                if status.get("phase") in {"DRAINING", "QUIESCED"}:
                    await old.call("worker.resume", timeout=30)
            except Exception as exc:
                failures.append(str(exc))
            finally:
                await handle.abort_cutover()
        self.quiesced.clear()
        for ingress in self.ingress:
            ingress.drop_pending_on_start = False
        self.ingress.clear()
        if self.app:
            if self.whatsapp_paused:
                self.whatsapp_paused = False
            self.app._handoff_draining = False
            if self.app.api_gateway:
                self.app.api_gateway._accepting_requests = True
            await self.commit()
        return {"resumed": not failures, "errors": failures}

    async def stop(self):
        if self.connector_task is not None:
            self.connector_task.cancel()
            await asyncio.gather(self.connector_task, return_exceptions=True)
        if self.task is not None and not self.task.done():
            self.app.request_shutdown(reason="shared-functions-stop", source="kernel")
            await asyncio.wait_for(asyncio.shield(self.task), timeout=40)
        self.stopping.set()
        return {"stopped": True}

    async def request(self, method, params):
        if method == "prepare":
            return await self.prepare()
        if method == "activate":
            return await self.activate(params.get("handoff"))
        if method == "commit":
            return await self.commit()
        if method == "quiesce":
            return await self.quiesce()
        if method == "resume":
            return await self.resume()
        if method == "stop":
            return await self.stop()
        raise ValueError(f"unsupported process operation: {method}")


async def serve(connection, bootstrap):
    peer = JsonConnectionPeer(connection, label="kernel")
    host = RuntimeAppHost(peer, bootstrap)
    peer.request_handler = host.request
    peer.start()
    try:
        while not host.stopping.is_set() and not peer.is_closed:
            if host.task is not None and host.task.done():
                await host.task
                await peer.emit("application.stopped", {})
                break
            await asyncio.sleep(0.1)
    finally:
        await host.stop()
        await peer.close()
