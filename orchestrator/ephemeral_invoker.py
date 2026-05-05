from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SidecarFailureResponse:
    text: str
    error: str
    is_success: bool = False


class BackendSidecarInvoker:
    """Small sidecar adapter for optional features that need one-shot LLM calls."""

    def __init__(self, backend_manager: Any, *, logger: logging.Logger | None = None):
        self.backend_manager = backend_manager
        self.logger = logger or logging.getLogger("EphemeralInvoker")

    def current_context(self) -> dict[str, Any] | None:
        manager = self.backend_manager
        config = getattr(manager, "config", None)
        engine = getattr(config, "active_backend", None)
        if not engine:
            self.logger.warning("Sidecar context unavailable: active backend is missing")
            return None

        model = None
        current_backend = getattr(manager, "current_backend", None)
        backend_config = getattr(current_backend, "config", None)
        if backend_config is not None:
            model = getattr(backend_config, "model", None)
        if not model:
            model = getattr(manager, "_active_model_override", None)
        if not model:
            for backend_cfg in getattr(config, "allowed_backends", []) or []:
                if backend_cfg.get("engine") == engine:
                    model = backend_cfg.get("model")
                    break
        if not model:
            self.logger.warning("Sidecar context unavailable: model is missing for backend %s", engine)
            return None

        return {
            "engine": str(engine),
            "model": str(model),
        }

    async def __call__(
        self,
        *,
        engine: str,
        model: str,
        prompt: str,
        request_id: str,
        silent: bool = True,
    ) -> Any:
        started = time.monotonic()
        self.logger.info(
            "Starting sidecar invocation request_id=%s engine=%s model=%s silent=%s",
            request_id,
            engine,
            model,
            silent,
        )
        try:
            response = await self.backend_manager.generate_ephemeral_response(
                engine=engine,
                model=model,
                prompt=prompt,
                request_id=request_id,
                silent=silent,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.logger.warning(
                "Sidecar invocation failed request_id=%s engine=%s model=%s elapsed_s=%.2f error=%s",
                request_id,
                engine,
                model,
                elapsed,
                exc,
            )
            return SidecarFailureResponse(text="", error=str(exc))

        elapsed = time.monotonic() - started
        success = bool(getattr(response, "is_success", False))
        self.logger.info(
            "Completed sidecar invocation request_id=%s engine=%s model=%s success=%s elapsed_s=%.2f",
            request_id,
            engine,
            model,
            success,
            elapsed,
        )
        return response


def make_backend_sidecar_invoker(backend_manager: Any) -> tuple[BackendSidecarInvoker, Any]:
    invoker = BackendSidecarInvoker(backend_manager)
    return invoker, invoker.current_context
