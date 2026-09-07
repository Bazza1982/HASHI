from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType

from orchestrator.runtime_contract import CORE_SOURCE_PATHS

FUNCTION_MODULE_PREFIXES = (
    "adapters.",
    "tools.",
    "orchestrator.",
    "flow.",
    "nagare.",
    "remote.",
    "transports.",
)
# These modules execute in a separately launched Windows helper environment.
# Their JSON/HTTP boundary is validated independently; they must never be
# materialised inside the HASHI Core merely because a test inspected them.
FUNCTION_SIDECAR_PREFIXES = (
    "tools.windows_helper",
    "tools.windows_use_mcp_client",
)
# These modules define identity objects already owned by the running process.
# They are not function-layer modules: changing one is incomplete until it has
# an explicit warm-handoff design.  /reboot must never claim that merely
# reloading the module replaced an already-held lock or path identity.
PROCESS_IDENTITY_MODULES = frozenset(
    relative.removesuffix("/__init__.py").removesuffix(".py").replace("/", ".")
    for relative in CORE_SOURCE_PATHS
    if relative.startswith(
        (
            "adapters/",
            "flow/",
            "nagare/",
            "orchestrator/",
            "remote/",
            "tools/",
            "transports/",
        )
    )
)


def is_function_module_name(name: str) -> bool:
    if name in PROCESS_IDENTITY_MODULES:
        return False
    if any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in FUNCTION_SIDECAR_PREFIXES
    ):
        return False
    return any(name.startswith(prefix) for prefix in FUNCTION_MODULE_PREFIXES)

# Only dependency roots that are imported by many consumers belong here.
# The order is centralized so /reboot has one generation contract. Providers
# are constructed before consumers that bind their classes or constants.
FOUNDATION_PHASES = {
    # The HER gateway context imports ToolRegistry at module scope. Construct
    # schemas, then the registry, then the context so a hot restart cannot
    # retain the pre-change ToolRegistry class after its constructor evolves.
    "tools.schemas": 0,
    # Pricing revision is imported by HER runtime configuration. Construct it
    # before that consumer so future price-table changes take effect together.
    "tools.token_tracker": 0,
    "tools.registry": 1,
    "tools.gateway.context": 2,
    "tools.gateway.mcp_stdio": 3,
    "adapters.stream_events": 0,
    "adapters.stream_io": 0,
    "orchestrator.flexible_backend_registry": 0,
    # API Gateway and provider adapters import multimodal constants and value
    # types at module scope. Construct the contract first so consumers cannot
    # bind to a previous-generation dictionary.
    "orchestrator.multimodal_contract": 0,
    "orchestrator.command_specs": 0,
    # Notification helpers are imported directly by command and runtime
    # consumers. Construct the provider first so a hot reboot that introduces a
    # new helper cannot ask freshly reloaded consumers to import it from the
    # previous in-memory module.
    "orchestrator.telegram_notifications": 0,
    # QueuedRequest is imported at module scope by both agent runtimes and
    # request-pipeline consumers. Construct it first so a hot reboot cannot bind
    # a new consumer to the previous dataclass constructor.
    "orchestrator.runtime_common": 0,
    "orchestrator.runtime_defaults": 0,
    # Config is Core-owned. Function consumers bind only its stable interface.
    "orchestrator.config": 2,
    "orchestrator.workspace_state": 0,
    # SessionStore defines classes imported directly by runtime_session.  The
    # provider must be constructed first; otherwise runtime_session can retain
    # a previous-generation class.
    "orchestrator.session_store": 0,
    "orchestrator.runtime_session": 1,
    # Context compaction owns value types and must be constructed before runtime
    # pipeline/command consumers bind its coordinator and exception classes.
    "orchestrator.context_compaction": 3,
    "adapters.base": 1,
    "adapters.her_persona": 1,
    "adapters.xai_oauth_credentials": 1,
    "orchestrator.model_catalog": 1,
    "orchestrator.manager_registry": 1,
    "orchestrator.ticket_manager": 1,
    "adapters.openrouter_api": 2,
    "adapters.xai_imagine": 2,
    # HER v2 dependency order. Construct value types first and the facade only
    # after the provider-neutral runtime graph is coherent.
    "orchestrator.her_v2.models": 0,
    "orchestrator.her_v2.audit": 0,
    "orchestrator.her_v2.progress": 0,
    "orchestrator.her_v2.task_state": 0,
    "orchestrator.her_v2.cognitive_control": 1,
    # Fixed-backend state types must be constructed before the protocol coordinator
    # imports them. This keeps repeated /reboot cycles from binding the newly
    # reloaded coordinator to the previous SessionStore class object.
    "orchestrator.her_v2.session_store": 0,
    "orchestrator.her_v2.backend_session": 1,
    "orchestrator.her_v2.config": 1,
    "orchestrator.her_v2.retry": 1,
    "orchestrator.her_v2.runtime_configuration": 2,
    "orchestrator.her_v2.lifecycle": 1,
    "orchestrator.her_v2.policy": 1,
    "orchestrator.her_v2.prompt_catalog": 1,
    "orchestrator.her_v2.prompts": 2,
    "orchestrator.her_v2.interfaces": 2,
    "orchestrator.her_v2.ledger": 3,
    "orchestrator.her_v2.learning": 3,
    "orchestrator.her_v2.presentation": 3,
    "orchestrator.her_v2.structured": 3,
    "orchestrator.her_v2.commentary": 4,
    "orchestrator.her_v2.runtime_support": 4,
    "orchestrator.her_v2.runtime_invocation": 5,
    "orchestrator.her_v2.runtime": 6,
    "orchestrator.her_v2": 7,
    "adapters.her_v2_provider": 7,
    "adapters.her_v2": 8,
}


class FunctionContractError(RuntimeError):
    """A candidate Function Worker generation violates its public contract."""


def function_module_order_key(name: str) -> tuple[int, str]:
    if name in FOUNDATION_PHASES:
        return (FOUNDATION_PHASES[name], name)
    if name.startswith(("adapters.", "tools.")):
        return (3, name)
    if "_runtime" in name:
        return (5, name)
    return (4, name)


def discover_loaded_function_modules(
    modules: Mapping[str, ModuleType] | None = None,
    *,
    code_root: Path | None = None,
) -> list[str]:
    loaded = modules if modules is not None else sys.modules
    root = Path(code_root).resolve() if code_root is not None else None

    def is_reloadable_project_module(name: str) -> bool:
        if not is_function_module_name(name):
            return False
        if root is None:
            return True
        module = loaded.get(name)
        raw_file = getattr(module, "__file__", None) if module is not None else None
        if not raw_file:
            return False
        path = Path(raw_file)
        if path.suffix in {".pyc", ".pyo"}:
            path = Path(str(path)[:-1])
        # A long-running process may still retain a module from a branch that
        # has since been switched away. importlib.reload() cannot reload that
        # stale object once its source file is gone, so exclude it up front.
        if not path.is_file():
            return False
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        return True

    return sorted(
        (name for name in list(loaded) if is_reloadable_project_module(name)),
        key=function_module_order_key,
    )


def validate_function_contract(
    module_loader: Callable[[str], ModuleType] = importlib.import_module,
) -> None:
    """Validate the public cross-module contract of one function generation.

    This validator is deliberately kernel-free so the exact same assertions
    run in the isolated candidate process and again on the prepared live
    generation before any Agent is stopped.
    """

    stream_events = module_loader("adapters.stream_events")
    adapter_base = module_loader("adapters.base")
    backend_registry = module_loader("adapters.registry")
    backend_catalog = module_loader("orchestrator.flexible_backend_registry")
    her_v2 = module_loader("adapters.her_v2")
    runtime_config = module_loader("orchestrator.config")
    runtime_pipeline = module_loader("orchestrator.runtime_pipeline")
    runtime_common = module_loader("orchestrator.runtime_common")
    session_store = module_loader("orchestrator.session_store")
    runtime_session = module_loader("orchestrator.runtime_session")
    flexible_runtime = module_loader("orchestrator.flexible_agent_runtime")
    command_registry = module_loader("orchestrator.command_registry")
    telegram_notifications = module_loader(
        "orchestrator.telegram_notifications"
    )
    tool_registry = module_loader("tools.registry")
    gateway_context = module_loader("tools.gateway.context")
    whatsapp_transport = module_loader("transports.whatsapp")
    chat_router = module_loader("transports.chat_router")

    acknowledgement_kind = getattr(stream_events, "KIND_ACKNOWLEDGEMENT", None)
    if acknowledgement_kind != "acknowledgement":
        raise FunctionContractError(
            "Function contract failed: adapters.stream_events does not expose "
            "KIND_ACKNOWLEDGEMENT='acknowledgement'"
        )
    resolver = getattr(backend_registry, "get_backend_class", None)
    supported_adapter = getattr(her_v2, "HERv2Adapter", None)
    if not callable(resolver) or supported_adapter is None:
        raise FunctionContractError(
            "Function contract failed: HER v2 registry contract unavailable"
        )
    if any(resolver(engine) is not supported_adapter for engine in ("her-v2", "her")):
        raise FunctionContractError(
            "Function contract failed: a HER ID can reach a stale or retired adapter"
        )
    base_backend = getattr(adapter_base, "BaseBackend", None)
    engines = getattr(backend_catalog, "BACKEND_REGISTRY", {})
    invalid_adapters: list[str] = []
    for engine in engines:
        try:
            adapter_class = resolver(engine)
        except Exception as exc:
            raise FunctionContractError(
                f"Function contract failed: backend {engine!r} cannot resolve: {exc}"
            ) from exc
        if (
            not isinstance(adapter_class, type)
            or not isinstance(base_backend, type)
            or not issubclass(adapter_class, base_backend)
        ):
            invalid_adapters.append(str(engine))
    if invalid_adapters:
        raise FunctionContractError(
            "Function contract failed: invalid backend adapter classes for "
            + ", ".join(sorted(invalid_adapters))
        )
    if (
        getattr(runtime_config, "DEFAULT_AGENT_MODE", None) != "fixed"
        or getattr(runtime_config, "SUPPORTED_AGENT_MODES", None)
        != frozenset({"fixed", "flex"})
        or not callable(getattr(runtime_config, "default_agent_mode_for_backend", None))
    ):
        raise FunctionContractError(
            "Function contract failed: fixed/flex configuration is not current"
        )
    if not callable(getattr(runtime_pipeline, "setup_interactive_feedback", None)):
        raise FunctionContractError(
            "Function contract failed: runtime acknowledgement pipeline unavailable"
        )
    notification_mode = getattr(telegram_notifications, "notification_mode", None)
    set_notification_mode = getattr(
        telegram_notifications, "set_notification_mode", None
    )
    disable_notification = getattr(
        telegram_notifications, "disable_notification", None
    )
    disable_parameters = (
        inspect.signature(disable_notification).parameters
        if callable(disable_notification)
        else {}
    )
    runtime_commands = command_registry.runtime_command_map()
    if (
        not callable(notification_mode)
        or not callable(set_notification_mode)
        or "purpose" not in disable_parameters
        or "notify" not in runtime_commands
    ):
        raise FunctionContractError(
            "Function contract failed: Telegram notification mode or /notify "
            "command is not current"
        )
    queued_request = getattr(runtime_common, "QueuedRequest", None)
    enqueue_request = getattr(
        getattr(flexible_runtime, "FlexibleAgentRuntime", None),
        "enqueue_request",
        None,
    )
    queued_fields = getattr(queued_request, "__dataclass_fields__", {})
    enqueue_parameters = (
        inspect.signature(enqueue_request).parameters
        if callable(enqueue_request)
        else {}
    )
    if (
        "habit_learning_eligible" not in queued_fields
        or "habit_learning_eligible" not in enqueue_parameters
    ):
        raise FunctionContractError(
            "Function contract failed: habit learning request intake is incomplete"
        )
    if getattr(flexible_runtime, "QueuedRequest", None) is not queued_request:
        raise FunctionContractError(
            "Function contract failed: flexible runtime retained a stale "
            "QueuedRequest class"
        )
    session_store_class = getattr(session_store, "SessionStore", None)
    if (
        session_store_class is None
        or getattr(runtime_session, "SessionStore", None) is not session_store_class
        or not callable(getattr(session_store_class, "recent_agent_exchanges", None))
    ):
        raise FunctionContractError(
            "Function contract failed: runtime session handling retained a stale "
            "SessionStore class"
        )
    if getattr(gateway_context, "ToolRegistry", None) is not getattr(
        tool_registry, "ToolRegistry", None
    ):
        raise FunctionContractError(
            "Function contract failed: tools.gateway.context retained a stale "
            "ToolRegistry class"
        )
    if not callable(
        getattr(
            getattr(tool_registry, "ToolRegistry", None),
            "execute_with_audit_context",
            None,
        )
    ):
        raise FunctionContractError(
            "Function contract failed: ToolRegistry scoped audit context is unavailable"
        )
    if not isinstance(getattr(whatsapp_transport, "WhatsAppTransport", None), type):
        raise FunctionContractError(
            "Function contract failed: WhatsApp transport unavailable"
        )
    if not isinstance(getattr(chat_router, "ChatRouter", None), type):
        raise FunctionContractError(
            "Function contract failed: WhatsApp router unavailable"
        )
