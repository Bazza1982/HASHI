from __future__ import annotations

import asyncio
import importlib
import logging
import os
import socket
import sys
import traceback
from contextlib import suppress
from pathlib import Path
from orchestrator.agent_directory import AgentDirectory
from orchestrator.api_gateway import available_gateway_models
from orchestrator.api_gateway_config import (
    config_path_for,
    load_api_gateway_config,
    save_api_gateway_config,
)
from orchestrator.background_jobs import BackgroundJobManager
from orchestrator.scheduler import TaskScheduler
from orchestrator.service_endpoints import ServiceEndpointError
from orchestrator.telegram_delivery_failover import delivery_health_watcher

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")
API_GATEWAY_SHUTDOWN_BUDGET_SEC = 30.0


class ServiceManager:
    """Control runtime services while live handles remain on the kernel."""

    def __init__(self, kernel):
        self.kernel = kernel

    def build_agent_directory(self):
        capabilities_path = self.kernel.paths.bridge_home / "agent_capabilities.json"
        self.kernel.agent_directory = AgentDirectory(
            self.kernel.paths.config_path,
            capabilities_path,
            self.kernel.runtimes,
        )
        return self.kernel.agent_directory

    def _api_gateway_state_path(self) -> Path:
        """Compatibility accessor for the canonical API Gateway config path."""
        return config_path_for(self.kernel.paths)

    def _load_api_gateway_state(self) -> dict:
        return load_api_gateway_config(self.kernel.paths)

    def _save_api_gateway_state(
        self,
        *,
        enabled: bool | None = None,
        default_model: str | None = None,
        updated_by: str = "service-manager",
    ) -> dict:
        return save_api_gateway_config(
            self.kernel.paths,
            enabled=enabled,
            default_model=default_model,
            updated_by=updated_by,
        )

    def _workbench_api_server_cls(self):
        module = sys.modules.get("orchestrator.workbench_api")
        if module is None:
            module = importlib.import_module("orchestrator.workbench_api")
        return module.WorkbenchApiServer

    def _api_gateway_server_cls(self):
        module = sys.modules.get("orchestrator.api_gateway")
        if module is None:
            module = importlib.import_module("orchestrator.api_gateway")
        return module.APIGatewayServer

    def _publish_service_endpoint(self, service: str, server, global_cfg, port: int):
        registry = getattr(self.kernel, "endpoint_registry", None)
        if registry is None:
            return None
        return registry.publish(
            service,
            instance_id=getattr(global_cfg, "instance_id", None)
            or getattr(self.kernel.paths, "instance_id", None),
            host=getattr(server, "bind_host", None),
            port=int(getattr(server, "bound_port", None) or port),
            metadata={"pid": os.getpid()},
        )

    def _unpublish_service_endpoint(self, service: str) -> None:
        registry = getattr(self.kernel, "endpoint_registry", None)
        if registry is not None:
            registry.unpublish(service)

    def api_gateway_base_url(self) -> str | None:
        global_cfg = self.kernel.global_cfg
        if global_cfg is None:
            return None
        registry = getattr(self.kernel, "endpoint_registry", None)
        if registry is not None:
            try:
                return registry.resolve(
                    "api_gateway",
                    expected_instance=getattr(global_cfg, "instance_id", None),
                ).base_url
            except ServiceEndpointError:
                return None
        running = self.kernel.api_gateway
        host = getattr(running, "bind_host", None) or str(getattr(global_cfg, "api_host", "") or "127.0.0.1").strip()
        if host in {"", "0.0.0.0", "localhost"}:
            host = "127.0.0.1"
        return f"http://{host}:{int(global_cfg.api_gateway_port)}"

    def api_gateway_state_snapshot(self) -> dict:
        state = self._load_api_gateway_state()
        return {
            "enabled": state["enabled"],
            "running": self.kernel.api_gateway is not None,
            "default_model": state["default_model"],
            "available_models": available_gateway_models(),
            "base_url": self.api_gateway_base_url(),
            "port": getattr(self.kernel.global_cfg, "api_gateway_port", None) if self.kernel.global_cfg else None,
        }

    async def start_workbench_api(
        self,
        global_cfg,
        secrets,
        *,
        reconcile_session_runs: bool = True,
    ):
        if self.kernel.workbench_api is not None:
            return
        try:
            server_cls = self._workbench_api_server_cls()
            self.kernel.workbench_api = server_cls(
                self.kernel.paths.config_path,
                global_cfg,
                self.kernel.runtimes,
                secrets=secrets,
                orchestrator=self.kernel,
                # Each isolated Worker reconciles its own Runs before
                # activation.  An instance-wide sweep here could interrupt a
                # request already accepted by a live Worker.
                reconcile_session_runs=False,
            )
            await self.kernel.workbench_api.start()
            bind_host = getattr(self.kernel.workbench_api, "bind_host", "127.0.0.1")
            endpoint = self._publish_service_endpoint(
                "workbench",
                self.kernel.workbench_api,
                global_cfg,
                global_cfg.workbench_port,
            )
            capability_broker = getattr(self.kernel, "capability_broker", None)
            if capability_broker is not None and endpoint is not None:
                capability_broker.start(endpoint)
            main_logger.info(
                "Backend API listening on http://%s:%s",
                bind_host,
                endpoint.port if endpoint is not None else global_cfg.workbench_port,
            )
            bridge_logger.info(
                "Backend API listening on http://%s:%s",
                bind_host,
                endpoint.port if endpoint is not None else global_cfg.workbench_port,
            )
        except Exception as e:
            failed_server = self.kernel.workbench_api
            capability_broker = getattr(self.kernel, "capability_broker", None)
            if capability_broker is not None:
                capability_broker.stop()
            self._unpublish_service_endpoint("workbench")
            self.kernel.workbench_api = None
            if failed_server is not None:
                try:
                    await asyncio.wait_for(failed_server.shutdown(), timeout=5.0)
                except Exception as shutdown_error:
                    bridge_logger.warning(
                        "Workbench rollback shutdown warning: %s: %s",
                        type(shutdown_error).__name__,
                        shutdown_error,
                    )
            main_logger.warning(
                "Backend API failed to start; continuing without external-client integration: %s",
                e,
            )
            main_logger.debug(traceback.format_exc())
            bridge_logger.warning(
                "Backend API failed to start; continuing without external-client integration: %s: %s",
                type(e).__name__,
                e,
            )

    async def start_api_gateway(self, global_cfg, secrets):
        state = self._load_api_gateway_state()
        if state["enabled"]:
            self.kernel.enable_api_gateway = True
        if not self.kernel.enable_api_gateway:
            main_logger.info("API Gateway disabled (use --api-gateway to enable).")
            return
        if self.kernel.api_gateway is not None:
            return
        try:
            server_cls = self._api_gateway_server_cls()
            self.kernel.api_gateway = server_cls(
                global_cfg,
                secrets,
                workspace_root=self.kernel.paths.workspaces_root,
                default_model=state["default_model"],
            )
            await self.kernel.api_gateway.start()
            if getattr(self.kernel, "_handoff_draining", False):
                self.kernel.api_gateway._accepting_requests = False
            bind_host = getattr(self.kernel.api_gateway, "bind_host", None) or "127.0.0.1"
            endpoint = self._publish_service_endpoint(
                "api_gateway",
                self.kernel.api_gateway,
                global_cfg,
                global_cfg.api_gateway_port,
            )
            main_logger.info(
                "API Gateway listening on http://%s:%s",
                bind_host,
                endpoint.port if endpoint is not None else global_cfg.api_gateway_port,
            )
            bridge_logger.info(
                "API Gateway listening on http://%s:%s",
                bind_host,
                endpoint.port if endpoint is not None else global_cfg.api_gateway_port,
            )
        except Exception as e:
            self._unpublish_service_endpoint("api_gateway")
            self.kernel.api_gateway = None
            main_logger.warning("API Gateway failed to start; continuing without it: %s", e)
            main_logger.debug(traceback.format_exc())

    async def start_api_gateway_runtime(self) -> tuple[bool, str]:
        if self.kernel.global_cfg is None:
            return False, "API Gateway cannot start before global config is loaded."
        if self.kernel.api_gateway is not None:
            self.kernel.enable_api_gateway = True
            self._save_api_gateway_state(enabled=True)
            return True, "API Gateway is already running."
        self.kernel.enable_api_gateway = True
        self._save_api_gateway_state(enabled=True)
        await self.start_api_gateway(self.kernel.global_cfg, self.kernel.secrets)
        if self.kernel.api_gateway is None:
            return False, "API Gateway failed to start."
        return True, f"API Gateway started on {self.api_gateway_base_url()}."

    async def set_api_gateway_enabled(self, enabled: bool) -> tuple[bool, str]:
        """Compatibility control used by the modular /api callbacks."""
        if enabled:
            return await self.start_api_gateway_runtime()
        return await self.stop_api_gateway_runtime()

    async def stop_api_gateway_runtime(self, timeout: float = 5.0) -> tuple[bool, str]:
        self.kernel.enable_api_gateway = False
        self._save_api_gateway_state(enabled=False)
        if self.kernel.api_gateway is None:
            return True, "API Gateway is already stopped."
        if not await self.stop_api_gateway(timeout=timeout):
            return False, "API Gateway could not be stopped safely; inspect the runtime log."
        return True, "API Gateway stopped."

    def set_api_gateway_default_model(self, model: str) -> tuple[bool, str]:
        normalized = str(model or "").strip()
        if normalized not in available_gateway_models():
            return False, f"Unknown API gateway model: {model}"
        self._save_api_gateway_state(default_model=normalized)
        if self.kernel.api_gateway is not None:
            self.kernel.api_gateway.set_default_model(normalized)
        return True, f"API Gateway default model set to {normalized}."

    def _resolve_enterprise_database_path(self, raw_url: str | None) -> Path:
        value = str(raw_url or "").strip()
        if not value:
            return self.kernel.paths.bridge_home / "state" / "enterprise.sqlite"
        if value.startswith("sqlite:///"):
            return Path(value[len("sqlite:///"):]).expanduser()
        if "://" in value:
            raise ValueError(f"unsupported enterprise scheduler lease database URL: {value}")
        return Path(value).expanduser()

    def _scheduler_enterprise_database_url(self, raw_url: str | None) -> str:
        value = str(raw_url or "").strip()
        if value:
            return value
        path = (
            self.kernel.paths.bridge_home / "state" / "enterprise.sqlite"
        ).resolve()
        # A bare Windows drive path is parsed as URL scheme ``c``.  Emit one
        # canonical SQLite URL on every platform so the enterprise store does
        # not mistake a local path for an unsupported backend.
        return "sqlite:///" + path.as_posix()

    def _scheduler_enterprise_lease_kwargs(self, global_cfg) -> dict:
        if not bool(getattr(global_cfg, "enterprise_scheduler_lease_enabled", False)):
            return {}
        holder = (
            getattr(global_cfg, "enterprise_scheduler_lease_holder", None)
            or os.environ.get("POD_NAME")
            or f"{getattr(global_cfg, 'instance_id', 'HASHI')}:{socket.gethostname()}:{os.getpid()}"
        )
        lease_name = str(getattr(global_cfg, "enterprise_scheduler_lease_name", None) or "superloop-scheduler")
        lease_ttl_seconds = max(1, int(getattr(global_cfg, "enterprise_scheduler_lease_ttl_seconds", 60) or 60))
        backend = str(getattr(global_cfg, "enterprise_scheduler_lease_backend", "db") or "db").strip().lower()

        if backend in {"k8s", "kubernetes"}:
            try:
                from orchestrator.enterprise import (
                    KubernetesApiLeaseClient,
                    KubernetesLeaseCoordinator,
                    KubernetesSchedulerLeaseStore,
                )

                namespace = (
                    getattr(global_cfg, "enterprise_scheduler_lease_kubernetes_namespace", None)
                    or os.environ.get("POD_NAMESPACE")
                    or "hashi-enterprise"
                )
                client = KubernetesApiLeaseClient.from_config(
                    in_cluster=bool(getattr(global_cfg, "enterprise_scheduler_lease_kubernetes_in_cluster", True)),
                    kubeconfig_path=getattr(global_cfg, "enterprise_scheduler_lease_kubeconfig_path", None),
                )
                lease_store = KubernetesSchedulerLeaseStore(
                    KubernetesLeaseCoordinator(client, namespace=str(namespace))
                )
            except Exception as exc:
                main_logger.warning("Enterprise scheduler Kubernetes lease disabled: %s", exc)
                bridge_logger.warning("Enterprise scheduler Kubernetes lease disabled: %s", exc)
                return {}
            return {
                "enterprise_lease_store": lease_store,
                "enterprise_lease_name": lease_name,
                "enterprise_lease_holder": str(holder),
                "enterprise_lease_ttl_seconds": lease_ttl_seconds,
            }

        if backend != "db":
            main_logger.warning("Enterprise scheduler lease disabled: unsupported backend %s", backend)
            bridge_logger.warning("Enterprise scheduler lease disabled: unsupported backend %s", backend)
            return {}

        try:
            from orchestrator.enterprise import EnterpriseLeaseStore

            database_url = self._scheduler_enterprise_database_url(
                getattr(global_cfg, "enterprise_database_url", None)
            )
            org_id = (
                getattr(global_cfg, "organization_id", None)
                or os.environ.get("HASHI_ORGANIZATION_ID")
                or os.environ.get("HASHI_ENTERPRISE_ORG_ID")
                or "ORG-001"
            )
            lease_store = EnterpriseLeaseStore.from_url(
                database_url,
                org_id=org_id,
                postgres_pool=bool(getattr(global_cfg, "enterprise_scheduler_lease_pool_enabled", False)),
                postgres_pool_min_size=max(
                    1,
                    int(getattr(global_cfg, "enterprise_scheduler_lease_pool_min_size", 1) or 1),
                ),
                postgres_pool_max_size=max(
                    1,
                    int(getattr(global_cfg, "enterprise_scheduler_lease_pool_max_size", 4) or 4),
                ),
            )
        except Exception as exc:
            main_logger.warning("Enterprise scheduler DB lease disabled: %s", exc)
            bridge_logger.warning("Enterprise scheduler DB lease disabled: %s", exc)
            return {}

        return {
            "enterprise_lease_store": lease_store,
            "enterprise_lease_name": lease_name,
            "enterprise_lease_holder": str(holder),
            "enterprise_lease_ttl_seconds": lease_ttl_seconds,
        }

    def start_scheduler(self, global_cfg):
        self.kernel.scheduler = TaskScheduler(
            self.kernel.paths.tasks_path,
            self.kernel.paths.state_path,
            self.kernel.runtimes,
            global_cfg.authorized_id,
            self.kernel.skill_manager,
            orchestrator=self.kernel,
            **self._scheduler_enterprise_lease_kwargs(global_cfg),
        )
        self.kernel.scheduler_task = asyncio.create_task(self.kernel.scheduler.run(), name="scheduler")

    def start_delivery_health_watcher(self):
        existing = getattr(self.kernel, "delivery_health_task", None)
        if existing is not None and not existing.done():
            return
        self.kernel.delivery_health_task = asyncio.create_task(
            delivery_health_watcher(self.kernel),
            name="telegram-delivery-health",
        )

    async def stop_delivery_health_watcher(self):
        task = getattr(self.kernel, "delivery_health_task", None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.kernel.delivery_health_task = None

    async def start_background_jobs(self):
        existing = getattr(self.kernel, "background_job_manager", None)
        if existing is not None:
            return existing
        manager = BackgroundJobManager(
            self.kernel.paths.bridge_home / "state" / "background_jobs",
            kernel=self.kernel,
        )
        await manager.start()
        self.kernel.background_job_manager = manager
        main_logger.info("Background job manager started.")
        bridge_logger.info("Background job manager started")
        return manager

    async def stop_background_jobs(self):
        manager = getattr(self.kernel, "background_job_manager", None)
        if manager is None:
            return
        await manager.stop()
        self.kernel.background_job_manager = None
        main_logger.info("Background job manager stopped.")
        bridge_logger.info("Background job manager stopped")

    async def start_runtime_services(self, global_cfg, secrets):
        self.build_agent_directory()
        await self.start_workbench_api(global_cfg, secrets)
        await self.start_api_gateway(global_cfg, secrets)
        self.start_scheduler(global_cfg)
        self.start_delivery_health_watcher()
        await self.start_background_jobs()
        workers = getattr(self.kernel, "function_workers", None)
        if workers is not None:
            await workers.broadcast_topology()

    async def stop_scheduler(self, timeout: float = 5.0):
        if self.kernel.scheduler_task is None:
            return
        scheduler = self.kernel.scheduler
        bridge_logger.info("Stopping scheduler task")
        self.kernel.scheduler_task.cancel()
        try:
            await asyncio.wait_for(self.kernel.scheduler_task, timeout=timeout)
        except asyncio.CancelledError:
            bridge_logger.info("Scheduler task stopped after cancellation")
        except asyncio.TimeoutError:
            bridge_logger.warning(
                "Scheduler task did not stop within %.1fs after cancellation",
                timeout,
            )
        finally:
            lease_store = getattr(scheduler, "enterprise_lease_store", None)
            close = getattr(lease_store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as e:
                    bridge_logger.warning("Enterprise scheduler lease store close failed: %s", e)
            self.kernel.scheduler_task = None
            self.kernel.scheduler = None

    async def stop_workbench_api(self, timeout: float = 5.0):
        if self.kernel.workbench_api is None:
            capability_broker = getattr(self.kernel, "capability_broker", None)
            if capability_broker is not None:
                capability_broker.stop()
            self._unpublish_service_endpoint("workbench")
            return
        bridge_logger.info("Stopping Backend API")
        try:
            await asyncio.wait_for(self.kernel.workbench_api.shutdown(), timeout=timeout)
        except (asyncio.TimeoutError, Exception) as e:
            main_logger.warning("Backend API shutdown warning: %s", e)
            bridge_logger.warning("Backend API shutdown warning: %s: %s", type(e).__name__, e)
        finally:
            self.kernel.workbench_api = None
            capability_broker = getattr(self.kernel, "capability_broker", None)
            if capability_broker is not None:
                capability_broker.stop()
            self._unpublish_service_endpoint("workbench")

    async def stop_api_gateway(
        self,
        timeout: float = API_GATEWAY_SHUTDOWN_BUDGET_SEC,
    ) -> bool:
        if self.kernel.api_gateway is None:
            self._unpublish_service_endpoint("api_gateway")
            return True
        bridge_logger.info("Stopping API Gateway")
        gateway = self.kernel.api_gateway
        shutdown_budget = max(
            float(timeout),
            float(
                getattr(
                    gateway,
                    "shutdown_budget_sec",
                    API_GATEWAY_SHUTDOWN_BUDGET_SEC,
                )
            ),
        )
        try:
            await asyncio.wait_for(gateway.stop(), timeout=shutdown_budget)
        except (asyncio.TimeoutError, Exception) as e:
            main_logger.warning("API Gateway shutdown warning: %s", e)
            bridge_logger.warning("API Gateway shutdown warning: %s: %s", type(e).__name__, e)
            self._unpublish_service_endpoint("api_gateway")
            return False
        else:
            self.kernel.api_gateway = None
            self._unpublish_service_endpoint("api_gateway")
            return True

    async def stop_runtime_services(self):
        await self.stop_scheduler()
        await self.stop_delivery_health_watcher()
        await self.stop_background_jobs()
        await self.stop_workbench_api()
        await self.stop_api_gateway()
