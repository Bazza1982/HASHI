from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from .backends import execute_action


class ActionRequest(BaseModel):
    action: str
    args: dict = {}


def _helper_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hashi.windows_helper")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(log_dir / "windows_helper.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def create_app(log_dir: Path) -> FastAPI:
    app = FastAPI(title="HASHI Windows Helper", version="0.1")
    audit = _helper_logger(log_dir)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "service": "windows-helper"}

    @app.post("/action")
    async def action(req: ActionRequest) -> dict:
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            output = await execute_action(req.action, req.args or {})
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            audit.info(json.dumps({
                "ts": time.time(),
                "request_id": request_id,
                "action": req.action,
                "elapsed_ms": elapsed_ms,
                "ok": True,
            }, ensure_ascii=False))
            return {"ok": True, "output": output, "request_id": request_id, "elapsed_ms": elapsed_ms}
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            audit.info(json.dumps({
                "ts": time.time(),
                "request_id": request_id,
                "action": req.action,
                "elapsed_ms": elapsed_ms,
                "ok": False,
                "error": str(exc),
            }, ensure_ascii=False))
            raise HTTPException(status_code=500, detail=str(exc))

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HASHI_WINDOWS_HELPER_PORT", "47831")))
    parser.add_argument("--log-dir", default=str(Path(os.environ.get("LOCALAPPDATA", ".")) / "HASHI" / "windows_helper" / "logs"))
    args = parser.parse_args()
    uvicorn.run(create_app(Path(args.log_dir)), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
