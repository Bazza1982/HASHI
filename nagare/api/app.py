from __future__ import annotations

import argparse
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nagare.api.runs import RunNotFoundError, RunSnapshotService
from nagare.logging.events import utc_now
from nagare.protocols.notifier import Notifier, NullNotifier


class NagareApiServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        runs_root: str | Path = "flow/runs",
        repo_root: str | Path | None = None,
        callables_root: str | Path = "flow/callables",
        notifier: Notifier | None = None,
        ai_agent_id: str = "akane",
        log_level: int = logging.INFO,
    ) -> None:
        self.runs_service = RunSnapshotService(runs_root=runs_root)
        self.runs_root = Path(runs_root)
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()
        self.callables_root = Path(callables_root)
        self.notifier = notifier or NullNotifier()
        self.ai_agent_id = ai_agent_id
        self.logger = logging.getLogger("nagare.api")
        self.logger.setLevel(log_level)
        # run_id → CallableSetupManager (kept alive while run is in flight)
        self._active_setup_managers: dict[str, Any] = {}
        self._setup_managers_lock = threading.Lock()
        super().__init__(server_address, NagareApiRequestHandler)


class NagareApiRequestHandler(BaseHTTPRequestHandler):
    server: NagareApiServer

    def do_GET(self) -> None:  # noqa: N802
        request_id = self.server.runs_service.new_request_id()
        started_at = utc_now()
        try:
            payload = self._route_get(request_id=request_id)
            self._write_json(HTTPStatus.OK, payload)
        except RunNotFoundError as error:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {
                    "request_id": request_id,
                    "retrieved_at": started_at,
                    "error": "run_not_found",
                    "message": str(error),
                },
            )
        except ValueError as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "request_id": request_id,
                    "retrieved_at": started_at,
                    "error": "bad_request",
                    "message": str(error),
                },
            )
        except Exception as error:  # pragma: no cover
            self.server.logger.exception("Unhandled API error")
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "request_id": request_id,
                    "retrieved_at": started_at,
                    "error": "internal_error",
                    "message": str(error),
                },
            )

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS preflight for POST requests."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        request_id = self.server.runs_service.new_request_id()
        started_at = utc_now()
        try:
            payload = self._route_post(request_id=request_id)
            self._write_json(HTTPStatus.OK, payload)
        except ValueError as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"request_id": request_id, "retrieved_at": started_at, "error": "bad_request", "message": str(error)},
            )
        except Exception as error:  # pragma: no cover
            self.server.logger.exception("Unhandled API error")
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"request_id": request_id, "retrieved_at": started_at, "error": "internal_error", "message": str(error)},
            )

    def _route_post(self, *, request_id: str) -> dict[str, Any]:
        parsed = urlsplit(self.path)
        path_parts = [part for part in parsed.path.split("/") if part]

        # POST /runs — submit a new workflow run
        if len(path_parts) == 1 and path_parts[0] == "runs":
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}
            return self._handle_submit_run(body, request_id=request_id)

        # POST /runs/{run_id}/callables/{agent_id} — deliver AI-written callable code
        if (
            len(path_parts) == 4
            and path_parts[0] == "runs"
            and path_parts[2] == "callables"
        ):
            run_id = path_parts[1]
            agent_id = path_parts[3]
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}
            return self._handle_deliver_callable(run_id, agent_id, body, request_id=request_id)

        raise ValueError("POST only supported for /runs and /runs/{id}/callables/{agent_id}")

    def _handle_submit_run(self, body: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        from nagare.engine.callable_setup_manager import CallableSetupManager
        from nagare.engine.runner import FlowRunner
        from nagare.handlers import RoutingStepHandler, SubprocessStepHandler

        workflow_path = body.get("workflow_path")
        if not workflow_path:
            raise ValueError("workflow_path is required")

        wf_path = Path(workflow_path)
        if not wf_path.is_absolute():
            wf_path = self.server.repo_root / wf_path
        if not wf_path.exists():
            raise ValueError(f"Workflow file not found: {wf_path}")

        pre_flight_data = body.get("pre_flight", {})

        # Build a runner first so we get its auto-generated run_id,
        # then wrap with a routing handler that supports callable workers
        # (e.g. Veritas adapters) alongside subprocess-backed LLM workers.
        runner = FlowRunner(
            str(wf_path),
            runs_root=self.server.runs_root,
            repo_root=self.server.repo_root,
        )
        run_id = runner.run_id

        # One setup manager per run — drives the callable auto-setup loop.
        api_base_url = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        setup_manager = CallableSetupManager(
            run_id=run_id,
            runs_root=self.server.runs_root,
            callables_root=self.server.callables_root,
            notifier=self.server.notifier,
            ai_agent_id=self.server.ai_agent_id,
            api_base_url=api_base_url,
        )
        with self.server._setup_managers_lock:
            self.server._active_setup_managers[run_id] = setup_manager

        subprocess_handler = SubprocessStepHandler(
            run_id=run_id,
            runs_root=self.server.runs_root,
            repo_root=self.server.repo_root,
        )
        step_handler = RoutingStepHandler(
            run_id=run_id,
            fallback_handler=subprocess_handler,
            runs_root=self.server.runs_root,
            setup_manager=setup_manager,
        )

        # Auto-load callables persisted from previous runs
        self._autoload_callables(step_handler)

        # Register Veritas callable adapters if available
        try:
            from veritas import CALLABLE_REGISTRY
            step_handler.register_callables(CALLABLE_REGISTRY)
            self.server.logger.info("Veritas callable adapters registered (%d)", len(CALLABLE_REGISTRY))
        except ImportError:
            pass  # Veritas not installed — callable steps will fail at runtime with a clear error

        runner.step_handler = step_handler

        if pre_flight_data:
            runner.set_pre_flight_data(pre_flight_data)

        # Start workflow in background thread; clean up setup manager when done.
        def _run_workflow() -> None:
            try:
                runner.start()
            except Exception:
                self.server.logger.exception("Background workflow run failed: %s", run_id)
            finally:
                with self.server._setup_managers_lock:
                    self.server._active_setup_managers.pop(run_id, None)

        thread = threading.Thread(target=_run_workflow, name=f"nagare-run-{run_id}", daemon=True)
        thread.start()

        self.server.logger.info("Submitted workflow run %s from %s", run_id, wf_path)

        return {
            "request_id": request_id,
            "retrieved_at": utc_now(),
            "run_id": run_id,
            "workflow_path": str(wf_path),
            "status": "submitted",
        }

    def _autoload_callables(self, step_handler: Any) -> None:
        """Load any .py files from flow/callables/ as pre-registered callables."""
        callables_dir = self.server.callables_root
        if not callables_dir.is_dir():
            return
        loaded = 0
        for py_file in sorted(callables_dir.glob("*.py")):
            agent_id = py_file.stem
            try:
                code = py_file.read_text(encoding="utf-8")
                namespace: dict = {}
                exec(compile(code, str(py_file), "exec"), namespace)  # noqa: S102
                fn = namespace.get("run")
                if fn and callable(fn):
                    step_handler.register_callable_force(agent_id, fn)
                    loaded += 1
                    self.server.logger.debug("Auto-loaded callable '%s' from %s", agent_id, py_file)
                else:
                    self.server.logger.warning("Skipping %s — no top-level 'run' function", py_file)
            except Exception as exc:
                self.server.logger.warning("Failed to auto-load callable '%s': %s", agent_id, exc)
        if loaded:
            self.server.logger.info("Auto-loaded %d callable(s) from %s", loaded, callables_dir)

    def _handle_deliver_callable(
        self,
        run_id: str,
        agent_id: str,
        body: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """
        POST /runs/{run_id}/callables/{agent_id}
        Body: {"code": "def run(task_message): ..."}

        Delivers AI-written Python code to the waiting runner thread.
        """
        code = body.get("code", "").strip()
        if not code:
            raise ValueError("'code' field is required and must not be empty")

        with self.server._setup_managers_lock:
            mgr = self.server._active_setup_managers.get(run_id)

        if mgr is None:
            raise ValueError(
                f"No active run found for run_id='{run_id}'. "
                f"The run may have already completed or the run_id is incorrect."
            )

        result = mgr.deliver_code(agent_id, code)
        if not result["ok"]:
            raise ValueError(f"Code delivery failed: {result['error']}")

        self.server.logger.info(
            "Callable '%s' delivered to run '%s'", agent_id, run_id
        )
        return {
            "request_id": request_id,
            "retrieved_at": utc_now(),
            "run_id": run_id,
            "agent_id": agent_id,
            "status": "delivered",
        }

    def log_message(self, format: str, *args: Any) -> None:
        self.server.logger.info("%s - %s", self.address_string(), format % args)

    def _route_get(self, *, request_id: str) -> dict[str, Any]:
        parsed = urlsplit(self.path)
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/healthz":
            return {"request_id": request_id, "retrieved_at": utc_now(), "ok": True}
        if len(path_parts) < 2 or path_parts[0] != "runs":
            raise ValueError("Supported endpoints: /runs/{id}, /runs/{id}/events, /runs/{id}/artifacts")

        run_id = path_parts[1]
        if len(path_parts) == 2:
            return self.server.runs_service.get_run_snapshot(run_id, request_id=request_id)
        if len(path_parts) == 3 and path_parts[2] == "events":
            query = parse_qs(parsed.query)
            limit_value = query.get("limit", [None])[0]
            return self.server.runs_service.get_run_events(
                run_id,
                request_id=request_id,
                limit=int(limit_value) if limit_value is not None else None,
            )
        if len(path_parts) == 3 and path_parts[2] == "artifacts":
            return self.server.runs_service.get_run_artifacts(run_id, request_id=request_id)
        raise ValueError("Supported endpoints: /runs/{id}, /runs/{id}/events, /runs/{id}/artifacts")

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    runs_root: str | Path = "flow/runs",
    repo_root: str | Path | None = None,
    callables_root: str | Path = "flow/callables",
    notifier: Notifier | None = None,
    ai_agent_id: str = "akane",
) -> None:
    server = NagareApiServer(
        (host, port),
        runs_root=runs_root,
        repo_root=repo_root,
        callables_root=callables_root,
        notifier=notifier,
        ai_agent_id=ai_agent_id,
    )
    server.logger.info("Nagare API listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Nagare read-only API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--runs-root", default="flow/runs")
    args = parser.parse_args()
    serve(host=args.host, port=args.port, runs_root=args.runs_root)


if __name__ == "__main__":
    main()
