"""Authenticated local AP controls; all configuration stays with existing owners."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
from pathlib import Path

from aiohttp import web
from orchestrator.agent_presence_controls import ControlError, SOURCE, encode
from orchestrator import agent_presence_settings
from orchestrator.config_json import ConfigConflictError

_PATH = "/api/agentpresence/controls"
_HEADERS = {"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"}


class AgentPresenceControls:
    def __init__(self, api):
        self.api = api
        self.lock = asyncio.Lock()
        self.requests = {}

    def auth(self, request, instance):
        try:
            local = ipaddress.ip_address(request.remote or "").is_loopback
        except ValueError:
            local = False
        if not local:
            raise ControlError("agentpresence_local_control_required", 403)
        rejection = self.api._agentpresence_ingress_rejection(
            request, source="agentpresence", surface="agentpresence", instance_id=instance)
        if rejection is not None:
            return rejection
        if self.api._is_governed_profile():
            raise ControlError("agentpresence_controls_profile_unsupported", 501)
        return None

    def agents(self):
        runtimes = self.api._runtime_map()
        result = []
        for row in self.api._load_agent_rows():
            name = row["name"]
            runtime = runtimes.get(name)
            metadata = runtime.get_runtime_metadata() if runtime else {}
            result.append({"id": name, "name": str(metadata.get("display_name") or row.get("display_name") or name),
                "online": bool(metadata.get("online")), "available": runtime is not None and bool(metadata.get("online"))})
        return result

    def instances(self):
        # Read the existing public registry owner. Never infer roots/ports or register instances.
        from tools.instance_registry import InstanceRegistry
        try:
            records = InstanceRegistry(program_root=Path(self.api.config_path).parent,
                                       program_version="").records()
            return [{"id": str(r["instance_id"]).upper(), "name": str(r["name"]),
                     "base_url": f"http://127.0.0.1:{int(r['api_port'])}"}
                    for r in records if 1 <= int(r.get("api_port") or 0) <= 65535]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def route(self):
        return agent_presence_settings.load_settings(self.api.global_config.bridge_home)

    async def worker(self, name, body):
        from orchestrator.admin_local_testing import try_execute_slash_command_text
        runtime = self.api._runtime_map().get(name)
        if runtime is None:
            raise ControlError("agent_unavailable", 503)
        result = await try_execute_slash_command_text(runtime, encode({**body, "agent_id": name}),
                                                      source_channel=SOURCE)
        if not isinstance(result, dict):
            raise ControlError("agentpresence_controls_unavailable", 503)
        if not result.get("ok"):
            raise ControlError(result.get("error_code") or "control_update_unconfirmed",
                               int(result.get("http_status") or 503), str(result.get("message") or ""))
        return result

    async def read(self):
        route = self.route()
        name = route["default_agent"]
        result = {"ok": True, "controls_version": 1, "instance_id": self.api.global_config.instance_id,
                  "route_revision": route["revision"], "agent_id": name, "agents": self.agents(),
                  "instances": self.instances(), "configuration": None, "configuration_version": "",
                  "backend_catalogue": {"ok": False, "backends": {}}, "online": False, "busy": False}
        if name:
            try:
                result.update(await self.worker(name, {"op": "read"}))
            except ControlError as exc:
                result["unavailable_reason"] = exc.code
        # A slow Worker response must not overwrite an externally changed route.
        latest = self.route()
        if latest != route:
            raise ControlError("agentpresence_route_changed")
        return result

    async def apply(self, body):
        async with self.lock:
            route = self.route()
            if (body["route_revision"] != route["revision"]
                    or body["agent_id"] != route["default_agent"]):
                raise ControlError("agentpresence_route_changed")
            if body["setting"] == "agent":
                target = next((a for a in self.agents() if a["id"] == body["value"]), None)
                if target is None or not target["available"]:
                    raise ControlError("agent_unavailable", 409)
                agent_presence_settings.set_default_agent(
                    self.api.global_config.bridge_home, target["id"], expected_revision=route["revision"])
                return {**await self.read(), "persisted": True}
            await self.worker(route["default_agent"], {**body, "op": "apply"})
            return {**await self.read(), "persisted": True}

    async def post(self, body):
        request_id = body["request_id"]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        now = time.monotonic()
        for key, (_, task, stamp) in list(self.requests.items()):
            if task.done() and now - stamp > 600:
                del self.requests[key]
        prior = self.requests.get(request_id)
        if prior:
            if prior[0] != digest:
                raise ControlError("control_request_id_reused")
            return await asyncio.shield(prior[1])
        if len(self.requests) >= 128:
            completed = [key for key, (_, task, _) in self.requests.items() if task.done()]
            if not completed:
                raise ControlError("control_busy", 429)
            del self.requests[completed[0]]
        task = asyncio.create_task(self.apply(body))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        self.requests[request_id] = (digest, task, now)
        return await asyncio.shield(task)


def register_agentpresence_controls(api):
    service = AgentPresenceControls(api)

    async def handle(request):
        try:
            if request.method == "GET":
                rejection = service.auth(request, request.query.get("instance_id"))
                if rejection is not None:
                    return rejection
                result = await service.read()
            else:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ControlError("control_request_invalid", 400)
                rejection = service.auth(request, body.get("instance_id"))
                if rejection is not None:
                    return rejection
                fields = {"instance_id", "agent_id", "route_revision", "configuration_version",
                          "setting", "value", "provider", "request_id", "ui_locale"}
                if set(body) - fields or not fields.difference({"provider", "ui_locale"}).issubset(body):
                    raise ControlError("control_request_invalid", 400)
                if (type(body["route_revision"]) is not int
                        or body["setting"] not in {"agent", "backend", "provider", "model", "effort"}
                        or not isinstance(body["value"], str)
                        or not isinstance(body["agent_id"], str)
                        or not isinstance(body["configuration_version"], str)
                        or not isinstance(body["request_id"], str)
                        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", body["request_id"]) is None):
                    raise ControlError("control_request_invalid", 400)
                result = await service.post(body)
            return web.json_response(result, headers=_HEADERS)
        except ControlError as exc:
            return web.json_response({"ok": False, "error_code": exc.code, "message": str(exc)},
                                     status=exc.status, headers=_HEADERS)
        except ConfigConflictError:
            return web.json_response({"ok": False, "error_code": "control_configuration_changed"},
                                     status=409, headers=_HEADERS)
        except (OSError, ValueError, TypeError, KeyError):
            return web.json_response({"ok": False, "error_code": "control_update_unconfirmed"},
                                     status=503, headers=_HEADERS)

    api.app.router.add_get(_PATH, handle)
    api.app.router.add_post(_PATH, handle)
