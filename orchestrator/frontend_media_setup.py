"""Frontend-owned media setup over existing owner/Session authority.

Configuration remains in its canonical owners. No test starts a Call, captures
media, sends conversation content, or promotes an API probe to voice readiness.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote
import aiohttp
from aiohttp import web
from orchestrator.config_json import (
    ConfigConflictError, ConfigDurabilityError, read_config_json,
    new_config_json, write_config_json,
)
from orchestrator.enterprise.secret_refs import ResolvedSecret
from orchestrator.frontend_live_voice.protocol import LiveVoiceError


class VoiceSetupError(RuntimeError):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


class CurrentVoiceSecrets(Mapping):
    """A voice consumer's live view of the existing private secret document."""
    def __init__(self, path, fallback=None):
        self.path, self.fallback = Path(path), dict(fallback or {})
    def snapshot(self):
        try:
            return read_config_json(self.path)
        except FileNotFoundError:
            return new_config_json(self.path, self.fallback)
        except (OSError, ValueError) as exc:
            raise VoiceSetupError("setup_configuration_unreadable") from exc
    def __getitem__(self, key):
        return self.snapshot()[key]
    def __iter__(self):
        return iter(self.snapshot())
    def __len__(self):
        return len(self.snapshot())
    def __bool__(self):
        return True


class VoiceSetupSecretResolver:
    def __init__(self, current, delegate):
        self.current, self.delegate = current, delegate
    def resolve(self, ref):
        if ref.startswith(("secrets://", "hashi://")):
            key = ref.partition("://")[2]
            value = self.current.get(key)
            if not value:
                raise ValueError("voice credential is not configured")
            return ResolvedSecret(ref=ref, value=str(value), source="hashi")
        return self.delegate.resolve(ref)


async def probe_api(url, *, key="", session_factory=aiohttp.ClientSession):
    """Bounded authenticated GET to the selected API; never inference."""
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        async with session_factory(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as http:
            async with http.get(url, headers=headers, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise VoiceSetupError("setup_auth_rejected", response.status)
                if response.status in (404, 405, 501) or 300 <= response.status < 400:
                    raise VoiceSetupError("setup_test_not_supported")
                if response.status == 429:
                    raise VoiceSetupError("setup_rate_limited", 429)
                if response.status != 200:
                    raise VoiceSetupError("setup_service_rejected")
                if "application/json" not in response.headers.get("Content-Type", ""):
                    raise VoiceSetupError("setup_response_invalid")
                # Headers prove an API response, not audio or model generation.
                await response.content.read(1024)
    except VoiceSetupError:
        raise
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        raise VoiceSetupError("setup_service_unreachable") from exc


class MediaServiceSetup:
    def __init__(self, api, *, http_probe=probe_api):
        self.api, self.http_probe = api, http_probe
        self.current = getattr(api, "voice_secrets", None) or CurrentVoiceSecrets(
            getattr(api.global_config, "secrets_path", None) or Path(api.config_path).parent / "secrets.json",
            getattr(api, "secrets", {}),
        )
        self.resolver = VoiceSetupSecretResolver(self.current, getattr(api, "connector_secret_resolver", None))
        self._revision_key = secrets.token_bytes(32)
        adapter = getattr(api.live_voice_manager, "providers", {}).get("hy-simulmt")
        if adapter is not None:
            from orchestrator.compute_services import ComputeServiceDirectory, ComputeServicesConfig, ComputeServiceClient
            if adapter.directory is None:
                adapter.directory = ComputeServiceDirectory(
                    ComputeServicesConfig(Path(api.config_path).parent / "compute_services.json"),
                    client=ComputeServiceClient(secret_resolver=self.resolver))
            elif getattr(adapter.directory.client, "secret_resolver", None) is None:
                adapter.directory.client.secret_resolver = self.resolver

    @staticmethod
    def _credential_key(target):
        ref = str(target.get("credential_ref") or "")
        key = ref.partition("://")[2] if ref.startswith(("secrets://", "hashi://")) else ""
        return key if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", key) else None

    def _describe(self, entrypoint, owner, scope):
        binding, extra_revision = None, None
        if entrypoint == "call":
            ctx = self.api.call_service.config.context(owner, scope["agent_id"])
            services = []
            if ctx.get("call_ready"):
                _, targets, catalog = self.api.call_service.config.freeze_runtime(owner, scope["agent_id"], ctx["revision"])
                for kind, target in targets.items():
                    if not target:
                        continue
                    target = dict(target)
                    if kind == "tts" and catalog is not None:
                        target.update(catalog.service(target["id"]))
                    target.update(kind=kind)
                    if target.get("base_url"):
                        target["endpoint"] = target["base_url"]
                    target["credential_key"] = self._credential_key(target)
                    services.append(target)
            return ctx["revision"], services, binding, extra_revision
        if entrypoint not in {"phone", "translate"}:
            raise VoiceSetupError("setup_entrypoint_invalid", 400)
        manager = self.api.live_voice_manager
        phone = manager._phone_session(scope["agent_id"], owner_id=owner, session_id=scope["session_id"],
            context_generation=scope["context_generation"], entrypoint=entrypoint)
        adapter = phone["adapter"]
        provider = phone["provider"]
        service = {"id":provider,"label":phone["public"].get("provider_label") or provider,
            "model":phone["model"],"kind":entrypoint,"_phone":phone}
        if provider == "openai":
            from orchestrator.frontend_live_voice.openai_live import COUNT_INPUT_URL
            root = COUNT_INPUT_URL.split("/responses/")[0]
            service.update(endpoint=root, probe_url=root+"/models/"+quote(phone["model"],safe=""),
                credential_key="openai_api_key")
        elif provider == "local-cascade":
            from orchestrator.frontend_live_voice.cascade_provider import _local_worker_url, DEFAULT_CASCADE_WORKER_URL
            endpoint = _local_worker_url(os.environ.get("CASCADE_WORKER_URL",DEFAULT_CASCADE_WORKER_URL))
            service.update(endpoint=endpoint,probe_url=endpoint+"/health",credential_key=None)
        elif provider == "hy-simulmt":
            directory = adapter.directory
            node_id, service_id = adapter.connection_selection()
            binding = {"node_id":node_id,"service_id":service_id,"base_url":"","location":"lan","authenticated":False,
                "manifest_path":"/v1/translation/manifest"}
            try:
                doc,nodes = directory.config.read()
                extra_revision = doc.revision
                service['compute_revision'] = doc.revision
                node = next((node for node in nodes if node.id == node_id),None)
            except Exception as exc:
                if getattr(exc,"code","") != "compute_not_configured":
                    raise
                node = None
            if node is not None:
                binding.update(base_url=node.base_url,location=node.location,authenticated=bool(node.credential_ref),manifest_path=node.manifest_path)
                service.update(endpoint=node.base_url,credential_ref=node.credential_ref)
            service.update(node_id=node_id,service_id=service_id,directory=directory,
                credential_key=self._credential_key(service),kind="translate")
        return phone["public"]["revision"],[service],binding,extra_revision

    def _view(self, entrypoint, owner, scope):
        revision, services, binding, extra = self._describe(entrypoint,owner,scope)
        document = self.current.snapshot()
        identity = [entrypoint,owner,scope,revision,document.revision,extra]
        token = hmac.new(self._revision_key,json.dumps(identity,sort_keys=True).encode(),hashlib.sha256).hexdigest()
        public = []
        for service in services:
            key = service.get("credential_key")
            ref, env = service.get("credential_ref"),service.get("credential_env")
            configured = bool(document.get(key)) if key else True
            if not key and (ref or env):
                try:
                    configured = bool(self.resolver.resolve(ref).value) if ref else bool(os.environ.get(env))
                except (ValueError,AttributeError):
                    configured = False
            if service["id"] == "openai":
                configured = bool(service["adapter"].credential(self.current)) if service.get("adapter") else bool(document.get(key) or os.environ.get("OPENAI_API_KEY"))
            if service.get("kind") == "translate":
                configured = configured and bool(binding and binding.get("base_url") and binding.get("service_id"))
            public.append({k:service[k] for k in ("id","label","kind","model","endpoint") if k in service} |
                {"credential_editable":bool(key),"credential_configured":configured})
        value = {"ok":True,"entrypoint":entrypoint,"revision":token,"services":public,
            "configured":bool(public) and all(row["credential_configured"] for row in public)}
        if binding is not None:
            value["binding"] = binding
        return value,services,document

    def status(self, entrypoint, owner, scope):
        return self._view(entrypoint,owner,scope)[0]

    def _checked(self, entrypoint, owner, scope, expected_revision):
        view,services,document = self._view(entrypoint,owner,scope)
        if not isinstance(expected_revision,str) or not hmac.compare_digest(view["revision"],expected_revision):
            raise VoiceSetupError("setup_configuration_changed",409)
        return view,services,document

    def _require_idle(self, owner):
        if self.api.live_voice_manager.has_foreground_call(owner) or self.api.call_service.busy(owner):
            raise VoiceSetupError("setup_media_busy",409)

    def _forget_failure(self, service, key=None):
        phone = service.get("_phone")
        forget = getattr(self.api.live_voice_manager,"forget_provider_failure",None)
        if phone is not None and callable(forget):
            forget(phone,phone["adapter"].credential(self.current) if key is None else key)

    def save_key(self, entrypoint, owner, scope, *, expected_revision, service_id, api_key):
        self._require_idle(owner)
        _,services,document = self._checked(entrypoint,owner,scope,expected_revision)
        target = next((s for s in services if s["id"] == service_id),None)
        key = target.get("credential_key") if target else None
        if not key:
            raise VoiceSetupError("setup_service_invalid",400)
        if not isinstance(api_key,str) or not 1 <= len(api_key.strip()) <= 4096 or any(ord(c)<33 or ord(c)>126 for c in api_key.strip()):
            raise VoiceSetupError("setup_key_invalid",400)
        document[key] = api_key.strip()
        try:
            write_config_json(self.current.path,document)
        except ConfigConflictError as exc:
            raise VoiceSetupError("setup_configuration_changed",409) from exc
        except ConfigDurabilityError as exc:
            raise VoiceSetupError("setup_outcome_unknown") from exc
        self._forget_failure(target)
        return self.status(entrypoint,owner,scope)

    def save_binding(self, entrypoint, owner, scope, *, expected_revision, binding):
        self._require_idle(owner)
        if entrypoint != "translate":
            raise VoiceSetupError("setup_entrypoint_invalid",400)
        _,services,_ = self._checked(entrypoint,owner,scope,expected_revision)
        directory = services[0].get("directory")
        if directory is None:
            raise VoiceSetupError("setup_service_invalid",400)
        directory.config.save_interpretation_connection(binding, expected_revision=services[0].get('compute_revision'))
        for service in self._describe(entrypoint,owner,scope)[1]:
            self._forget_failure(service)
        return self.status(entrypoint,owner,scope)

    async def test(self, entrypoint, owner, scope, *, expected_revision):
        _,services,document = self._checked(entrypoint,owner,scope,expected_revision)
        if not services:
            raise VoiceSetupError("setup_service_not_configured")
        results = []
        for service in services:
            result = {"id":service["id"],"label":service.get("label") or service["id"],"scope":"api_access"}
            try:
                if service.get("kind") == "translate":
                    from orchestrator.compute_services import ComputeReference,ComputeRequirements
                    directory = service["directory"]
                    _,nodes = directory.config.read()
                    node = next((n for n in nodes if n.id == service["node_id"] and n.enabled),None)
                    if node is None:
                        raise VoiceSetupError("setup_service_not_configured")
                    snapshot = await directory.client.fetch_manifest(node)
                    snapshot.catalogue().resolve(ComputeReference(node.id,service["service_id"],service["model"]),
                        ComputeRequirements(input_modalities=frozenset({"audio"}),output_modalities=frozenset({"audio","text"}),
                            protocols=frozenset({"hashi.phone.webrtc","hashi.phone.sideband"}),locations=frozenset({"local","lan"})))
                    if node.health_path:
                        health = await directory.client.probe_health(node)
                        if health.status not in {"ready","healthy","ok","available"}:
                            raise VoiceSetupError("setup_service_unreachable")
                    result["scope"] = "compute_manifest"
                else:
                    url = service.get("probe_url")
                    if not url and service.get("base_url"):
                        from orchestrator.frontend_call.config import OPENROUTER_API_BASE
                        base = service["base_url"].rstrip("/")
                        url = base + ("/key" if base == OPENROUTER_API_BASE.rstrip("/") else "/models")
                    if not url:
                        raise VoiceSetupError("setup_test_not_supported")
                    key = str(document.get(service.get("credential_key")) or "")
                    if service.get("credential_ref"):
                        key = self.resolver.resolve(service["credential_ref"]).value
                    elif service.get("credential_env"):
                        key = os.environ.get(service["credential_env"],"")
                    elif service["id"] == "openai":
                        key = key or os.environ.get("OPENAI_API_KEY","")
                    if (service.get("credential_key") or service.get("credential_ref") or service.get("credential_env")) and not key:
                        raise VoiceSetupError("setup_credential_missing")
                    await self.http_probe(url,key=key)
                self._forget_failure(service,"" if service.get("kind") == "translate" else key)
                result["ok"] = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc,"code","setup_service_unreachable")
                result.update(ok=False,error_code=code if re.fullmatch(r"[a-z][a-z0-9_]{1,100}",str(code)) else "setup_service_unreachable")
            results.append(result)
        return {"ok":True,"results":results}


def register_media_setup_api(api):
    setup = MediaServiceSetup(api)
    api.media_service_setup = setup
    headers = {"Cache-Control":"no-store, private","X-Content-Type-Options":"nosniff"}
    allowed = {"status":set(),"test":{"expected_revision"},"save_key":{"expected_revision","service_id","api_key"},
        "save_binding":{"expected_revision","binding"}}
    async def handler(request):
        try:
            if request.content_type != "application/json":
                raise VoiceSetupError("setup_body_invalid",415)
            data = bytearray()
            async for chunk in request.content.iter_chunked(4096):
                data.extend(chunk)
                if len(data)>16384:
                    raise VoiceSetupError("setup_body_limit",413)
            payload = json.loads(data)
            if not isinstance(payload,dict) or payload.get("operation") not in allowed:
                raise VoiceSetupError("setup_operation_invalid",400)
            operation = payload["operation"]
            common = {"operation","entrypoint","agent_id","session_id","context_generation"}
            if set(payload)-common-allowed[operation]:
                raise VoiceSetupError("setup_body_invalid",400)
            if payload.get("entrypoint") not in {"phone","call","translate"}:
                raise VoiceSetupError("setup_entrypoint_invalid",400)
            if type(payload.get("context_generation")) is not int or payload["context_generation"] < 1:
                raise VoiceSetupError("setup_scope_invalid",400)
            if any(not isinstance(payload.get(key),str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}",payload[key]) for key in ("agent_id","session_id")):
                raise VoiceSetupError("setup_scope_invalid",400)
            authority = await api._authorize_live_voice(request,"context",payload)
            owner = authority.get("owner_id")
            scope = {key:payload.get(key) for key in ("agent_id","session_id","context_generation")}
            api._require_live_voice_session_scope(owner_id=owner,**scope)
            if operation.startswith("save_") and not authority.get("admin"):
                raise VoiceSetupError("setup_permission_denied",403)
            entrypoint = payload["entrypoint"]
            if operation == "status":
                value = setup.status(entrypoint,owner,scope)
            elif operation == "test":
                value = await setup.test(entrypoint,owner,scope,expected_revision=payload.get("expected_revision"))
            else:
                params = {key:payload.get(key) for key in allowed[operation]}
                value = getattr(setup,operation)(entrypoint,owner,scope,**params)
            return web.json_response(value,headers=headers)
        except asyncio.CancelledError:
            raise
        except (VoiceSetupError,LiveVoiceError) as exc:
            return web.json_response({"ok":False,"error_code":exc.code},status=exc.status,headers=headers)
        except (json.JSONDecodeError,UnicodeDecodeError):
            return web.json_response({"ok":False,"error_code":"setup_body_invalid"},status=400,headers=headers)
        except ConfigConflictError:
            return web.json_response({"ok":False,"error_code":"setup_configuration_changed"},status=409,headers=headers)
        except ConfigDurabilityError:
            return web.json_response({"ok":False,"error_code":"setup_outcome_unknown"},status=503,headers=headers)
        except Exception:
            return web.json_response({"ok":False,"error_code":"setup_configuration_unreadable"},status=503,headers=headers)
    api.app.router.add_post("/api/v1/media-services/operation",handler)
