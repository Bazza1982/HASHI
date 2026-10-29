"""Optional instance entrypoints; no service or native process is started here."""
from __future__ import annotations

from orchestrator.permissions.clients import PermissionControlClient, PermissionRunClient, service_clients
from orchestrator.permissions.her_binding import PermissionAgentController
from orchestrator.permissions.models import PermissionLevel


async def bind_agent(runtime):
    """Qualify installed controls before a managed native backend is built."""
    settings, _ = runtime.backend_manager._agent_permission_settings()
    if settings is None or settings.requested_level is PermissionLevel.L0:
        return None
    from orchestrator import runtime_session
    from orchestrator.flexible_backend_registry import canonical_backend_engine

    try:
        pao, _ = service_clients(runtime.global_config)
        service = PermissionRunClient(
            pao, store=runtime_session.ensure_store(runtime),
            workspace_dir=runtime.workspace_dir, settings=settings,
        )
        try:
            await service.qualify()
        except OSError:
            # The HER orchestration shell can remain available while its
            # installed service is offline. It has no local API/tool escape;
            # actual Runs durably wait at their original PAO identity.
            runtime.error_logger.warning("Permission service is unavailable; managed execution will wait")
    except Exception:
        # A user-selected third-party CLI remains honestly unmanaged. Managed
        # native paths stay closed; no exception may trigger their L0 fallback.
        if canonical_backend_engine(runtime.config.active_backend) != "her-v2":
            return None
        raise
    runtime.permission_service_client = service
    runtime.permission_runs = PermissionAgentController(runtime, service)
    return runtime.permission_runs


def bind_frontend(api):
    raw = getattr(api.global_config, "permission_service", None)
    if raw is None:
        return None
    _, client = service_clients(api.global_config)
    control = PermissionControlClient(client)
    api.permission_control = control
    return control
