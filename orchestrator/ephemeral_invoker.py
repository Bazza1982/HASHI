from __future__ import annotations

import logging
import time
import contextlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SidecarFailureResponse:
    text: str
    error: str
    is_success: bool = False


class BackendSidecarInvoker:
    """Small sidecar adapter for optional features that need one-shot LLM calls."""

    def __init__(self, backend_manager: Any, *, logger: logging.Logger | None = None, session_id_getter: Any | None = None):
        self.backend_manager = backend_manager
        self.logger = logger or logging.getLogger("EphemeralInvoker")
        self.session_id_getter = session_id_getter

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
        self._record_sidecar_usage(
            engine=engine,
            model=model,
            prompt=prompt,
            request_id=request_id,
            response=response,
            elapsed_s=elapsed,
            success=success,
        )
        self.logger.info(
            "Completed sidecar invocation request_id=%s engine=%s model=%s success=%s elapsed_s=%.2f",
            request_id,
            engine,
            model,
            success,
            elapsed,
        )
        return response

    def _record_sidecar_usage(
        self,
        *,
        engine: str,
        model: str,
        prompt: str,
        request_id: str,
        response: Any,
        elapsed_s: float,
        success: bool,
    ) -> None:
        try:
            from pathlib import Path
            from tools.token_tracker import estimate_tokens, record_audit_event, record_usage

            config = getattr(self.backend_manager, "config", None)
            workspace_dir = getattr(config, "workspace_dir", None)
            if workspace_dir is None:
                return
            workspace = Path(workspace_dir)
            usage = getattr(response, "usage", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                thinking_tokens = int(getattr(usage, "thinking_tokens", 0) or 0)
                token_source = "api"
            else:
                input_tokens = estimate_tokens(prompt)
                output_tokens = estimate_tokens(getattr(response, "text", "") or "")
                thinking_tokens = 0
                token_source = "estimated"
            record_usage(
                workspace,
                model=model,
                backend=engine,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                session_id=self._session_id(),
                cost_usd=getattr(response, "cost_usd", None),
            )
            record_audit_event(
                workspace,
                {
                    "request_id": request_id,
                    "runtime": "flex",
                    "completion_path": "sidecar",
                    "backend": engine,
                    "model": model,
                    "success": success,
                    "token_source": token_source,
                    "final_prompt_chars": len(prompt or ""),
                    "response_chars": len(getattr(response, "text", "") or ""),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "thinking_tokens": thinking_tokens,
                    "elapsed_s": round(elapsed_s, 3),
                },
            )
        except Exception:
            return

    def _session_id(self) -> str:
        if callable(self.session_id_getter):
            with contextlib.suppress(Exception):
                value = self.session_id_getter()
                if value:
                    return str(value)
        return "sidecar"


def make_backend_sidecar_invoker(backend_manager: Any, *, session_id_getter: Any | None = None) -> tuple[BackendSidecarInvoker, Any]:
    invoker = BackendSidecarInvoker(backend_manager, session_id_getter=session_id_getter)
    return invoker, invoker.current_context
