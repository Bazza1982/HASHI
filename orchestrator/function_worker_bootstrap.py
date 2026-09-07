"""Stable process entry for one HASHI Agent Function Worker."""

from __future__ import annotations

import asyncio
import os
from typing import Any


def run_function_worker_process(connection: Any, bootstrap: dict) -> None:
    """Multiprocessing ``spawn`` target.

    Keep this entry deliberately small.  Runtime enforcement and candidate
    source verification happen before the functional host imports an Agent
    runtime or adapter.
    """

    bridge_home = str(bootstrap.get("bridge_home") or "").strip()
    code_root = str(bootstrap.get("code_root") or "").strip()
    generation_root = str(bootstrap.get("generation_root") or "").strip()
    if bridge_home:
        os.environ["BRIDGE_HOME"] = bridge_home
    if code_root:
        os.environ["HASHI_SOURCE_ROOT"] = code_root
    if generation_root:
        os.environ["HASHI_FUNCTION_GENERATION_ROOT"] = generation_root
    os.environ["HASHI_FUNCTION_WORKER"] = "1"
    os.environ["HASHI_FUNCTION_WORKER_AGENT"] = str(
        bootstrap.get("agent_name") or ""
    )
    try:
        from orchestrator.function_worker_host import run_function_worker

        asyncio.run(run_function_worker(connection, bootstrap))
    finally:
        try:
            connection.close()
        except (OSError, ValueError):
            pass
