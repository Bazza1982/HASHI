from __future__ import annotations

import argparse
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nagare.api.runs import RunNotFoundError, RunSnapshotService
from nagare.logging.events import utc_now


class NagareApiServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        runs_root: str | Path = "flow/runs",
        log_level: int = logging.INFO,
    ) -> None:
        self.runs_service = RunSnapshotService(runs_root=runs_root)
        self.logger = logging.getLogger("nagare.api")
        self.logger.setLevel(log_level)
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
) -> None:
    server = NagareApiServer((host, port), runs_root=runs_root)
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
