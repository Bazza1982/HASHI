from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

from orchestrator.admin_local_testing import (
    execute_local_command,
    supported_commands,
    try_execute_slash_command_text,
)
from orchestrator.agent_overview import build_agent_overview
from orchestrator.conversation_router import ConversationRouter
from orchestrator.enterprise.audit_export import format_otel_log, format_siem_event
from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger
from orchestrator.enterprise.audit_schema import AuditEvent, AuditEventWriter
from orchestrator.enterprise.auth_providers import load_auth_providers
from orchestrator.enterprise.capabilities import AgentCapabilityRegistry
from orchestrator.enterprise.channel_gate import EnterpriseChannelGate
from orchestrator.enterprise.channels import ChannelRegistry
from orchestrator.enterprise.connectors import (
    ConnectorAction,
    ConnectorExecutionService,
    ConnectorFactory,
    ConnectorRegistry,
    connector_action_schemas,
    validate_connector_action,
)
from orchestrator.enterprise.credentials import ConnectorCredentialStore
from orchestrator.enterprise.identity import EnterpriseRole, IdentityService
from orchestrator.enterprise.oidc_exchange import (
    build_oidc_token_exchange_request,
    map_oidc_claims,
)
from orchestrator.enterprise.oidc_flow import build_oidc_authorization_start
from orchestrator.enterprise.oidc_http import (
    OidcJwksCache,
    exchange_oidc_authorization_code,
    fetch_oidc_jwks,
)
from orchestrator.enterprise.oidc_session import complete_oidc_session
from orchestrator.enterprise.oidc_token import verify_oidc_id_token
from orchestrator.enterprise.policy import PolicyEvaluator
from orchestrator.enterprise.policy_templates import install_default_connector_policy
from orchestrator.enterprise.routing import agent_project_ids
from orchestrator.enterprise.saml import (
    build_saml_authn_start,
    validate_saml_assertion,
    verify_saml_assertion_signature,
)
from orchestrator.enterprise.scim import (
    ScimProvisioningService,
    scim_resource_type,
    scim_resource_types,
    scim_schema,
    scim_schemas,
    scim_service_provider_config,
    scim_user_resource,
)
from orchestrator.enterprise.secret_refs import ConnectorSecretResolver
from orchestrator.pathing import resolve_path_value
from orchestrator.session_store import (
    TERMINAL_RUN_STATES,
    IdempotencyConflict,
    SessionConflict,
    SessionNotFound,
    SessionStore,
)
from orchestrator.transfer_store import TransferStore

_SUPPORTED_CONNECTOR_TYPES = frozenset(
    {"github", "slack", "google_chat", "teams", "feishu"}
)
# External Session API publication stays fail-closed until the complete
# qualification gate in the Session architecture contract has passed.
PERSISTENT_SESSION_V1_QUALIFIED = False
_CONNECTOR_REQUIRED_SCOPES = {
    "github": frozenset({"repo:read", "repo:write"}),
    "slack": frozenset({"message.send"}),
    "google_chat": frozenset({"message.send"}),
    "teams": frozenset({"message.send"}),
    "feishu": frozenset({"message.send"}),
}
_CONNECTOR_SECRET_REF_PREFIXES = (
    "env://",
    "env:",
    "secrets://",
    "hashi://",
    "file://",
    "k8s://",
    "vault://",
)


def _connector_scopes_from_payload(value) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    return sorted({str(item).strip() for item in raw_items if str(item).strip()})


def _validate_connector_credential_payload(
    payload: dict, scopes: list[str]
) -> str | None:
    connector_type = str(payload.get("connector_type") or "").strip().lower()
    if connector_type not in _SUPPORTED_CONNECTOR_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_CONNECTOR_TYPES))
        return f"unsupported connector_type: {connector_type or '<empty>'}; supported: {supported}"
    secret_ref = str(payload.get("secret_ref") or "").strip()
    if not secret_ref:
        return "secret_ref is required"
    if not secret_ref.startswith(_CONNECTOR_SECRET_REF_PREFIXES):
        return "secret_ref must use env://, secrets://, hashi://, file://, k8s://, or vault://"
    required_scopes = _CONNECTOR_REQUIRED_SCOPES[connector_type]
    if not required_scopes.intersection(scopes):
        required = " or ".join(sorted(required_scopes))
        return f"{connector_type} credentials require scope {required}"
    return None


def _read_jsonl_recent(file_path: Path, limit: int = 50) -> dict:
    if not file_path.exists():
        return {"messages": [], "offset": 0}

    text = file_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    records = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("role") not in {"user", "assistant", "thinking"} or not obj.get(
            "text"
        ):
            continue
        records.append(obj)

    return {
        "messages": records[-limit:],
        "offset": len(text.encode("utf-8")),
    }


def _read_jsonl_increment(file_path: Path, offset: int = 0) -> dict:
    if not file_path.exists():
        return {"messages": [], "offset": 0}

    size = file_path.stat().st_size
    safe_offset = offset if 0 <= offset <= size else 0
    with open(file_path, "rb") as f:
        f.seek(safe_offset)
        chunk = f.read()

    messages = []
    for line in chunk.decode("utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("role") not in {"user", "assistant", "thinking"} or not obj.get(
            "text"
        ):
            continue
        messages.append(obj)

    return {"messages": messages, "offset": size}


def _saml_assertion_xml_from_payload(payload: dict) -> str:
    assertion_xml = str(payload.get("assertion_xml") or "").strip()
    if assertion_xml:
        return assertion_xml
    saml_response = str(payload.get("SAMLResponse") or "").strip()
    if not saml_response:
        raise ValueError("SAMLResponse or assertion_xml is required")
    try:
        return base64.b64decode(saml_response.encode("ascii"), validate=True).decode(
            "utf-8"
        )
    except Exception as exc:
        raise ValueError("SAMLResponse must be base64 encoded XML") from exc


class WorkbenchApiServer:
    TRANSFER_ACCEPT_PREFIX = "TRANSFER_ACCEPTED "
    FORK_ACCEPT_PREFIX = "FORK_ACCEPTED "

    def __init__(
        self,
        config_path: Path,
        global_config,
        runtimes: list | None = None,
        secrets: dict | None = None,
        orchestrator=None,
        connectors: list | None = None,
    ):
        self.config_path = config_path
        self.global_config = global_config
        self.runtimes = runtimes or []
        self.orchestrator = orchestrator
        self.secrets = dict(secrets or {})
        self.admin_token = (self.secrets.get("workbench_admin_token") or "").strip()
        self._static_connectors = list(connectors or [])
        self.session_store = SessionStore.from_global_config(self.global_config)
        self.reconciled_session_runs = self.session_store.reconcile_incomplete_runs()
        self.identity_service = self._build_identity_service()
        self.channel_registry = self._build_channel_registry()
        self.audit_writer = self._build_audit_writer()
        self.audit_ledger = self._build_audit_ledger()
        self.connector_credentials = self._build_connector_credentials()
        self.connector_secret_resolver = ConnectorSecretResolver(secrets=self.secrets)
        self._pending_oidc_flows: dict[str, object] = {}
        self._pending_saml_flows: dict[str, object] = {}
        self._oidc_jwks_cache = OidcJwksCache()
        self._oidc_token_transport = None
        self._oidc_jwks_transport = None
        self._saml_assertion_verifier = None
        self.connector_registry_errors: list[dict] = []
        self.connector_registry = self._build_connector_registry()
        self.bridge_router = ConversationRouter(
            config_path=self.config_path,
            capabilities_path=self.config_path.parent / "agent_capabilities.json",
            store_path=self.config_path.parent
            / "state"
            / "bridge_conversations.sqlite",
            runtimes=self._runtime_list(),
        )
        self.transfer_store = TransferStore(
            self.config_path.parent / "state" / "bridge_transfers.sqlite"
        )
        self.app = web.Application(client_max_size=64 * 1024 * 1024)
        self.app.router.add_post("/api/auth/login", self.handle_auth_login)
        self.app.router.add_post("/api/auth/logout", self.handle_auth_logout)
        self.app.router.add_get("/api/auth/me", self.handle_auth_me)
        self.app.router.add_get("/api/auth/providers", self.handle_auth_providers)
        self.app.router.add_get(
            "/api/auth/oidc/{provider_id}/start", self.handle_auth_oidc_start
        )
        self.app.router.add_get(
            "/api/auth/oidc/{provider_id}/callback", self.handle_auth_oidc_callback
        )
        self.app.router.add_get(
            "/api/auth/saml/{provider_id}/start", self.handle_auth_saml_start
        )
        self.app.router.add_post(
            "/api/auth/saml/{provider_id}/callback", self.handle_auth_saml_callback
        )
        self.app.router.add_get("/api/enterprise/users", self.handle_enterprise_users)
        self.app.router.add_post(
            "/api/enterprise/users", self.handle_enterprise_users_create
        )
        self.app.router.add_post(
            "/api/enterprise/scim/users", self.handle_enterprise_scim_users_upsert
        )
        self.app.router.add_post(
            "/api/enterprise/scim/users/deactivate",
            self.handle_enterprise_scim_users_deactivate,
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/Users", self.handle_enterprise_scim_v2_users_list
        )
        self.app.router.add_post(
            "/api/enterprise/scim/v2/Users", self.handle_enterprise_scim_v2_users_create
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/Users/{user_id}",
            self.handle_enterprise_scim_v2_users_get,
        )
        self.app.router.add_patch(
            "/api/enterprise/scim/v2/Users/{user_id}",
            self.handle_enterprise_scim_v2_users_patch,
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/Groups", self.handle_enterprise_scim_v2_groups_list
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/Groups/{group_id}",
            self.handle_enterprise_scim_v2_groups_get,
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/ServiceProviderConfig",
            self.handle_enterprise_scim_v2_service_provider_config,
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/ResourceTypes",
            self.handle_enterprise_scim_v2_resource_types,
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/ResourceTypes/{resource_type}",
            self.handle_enterprise_scim_v2_resource_type_get,
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/Schemas", self.handle_enterprise_scim_v2_schemas
        )
        self.app.router.add_get(
            "/api/enterprise/scim/v2/Schemas/{schema_id:.+}",
            self.handle_enterprise_scim_v2_schema_get,
        )
        self.app.router.add_post(
            "/api/enterprise/scim/v2/Bulk", self.handle_enterprise_scim_v2_bulk
        )
        self.app.router.add_get("/scim/v2/Users", self.handle_public_scim_v2_users_list)
        self.app.router.add_post(
            "/scim/v2/Users", self.handle_public_scim_v2_users_create
        )
        self.app.router.add_get(
            "/scim/v2/Users/{user_id}", self.handle_public_scim_v2_users_get
        )
        self.app.router.add_patch(
            "/scim/v2/Users/{user_id}", self.handle_public_scim_v2_users_patch
        )
        self.app.router.add_get(
            "/scim/v2/Groups", self.handle_public_scim_v2_groups_list
        )
        self.app.router.add_get(
            "/scim/v2/Groups/{group_id}", self.handle_public_scim_v2_groups_get
        )
        self.app.router.add_get(
            "/scim/v2/ServiceProviderConfig",
            self.handle_public_scim_v2_service_provider_config,
        )
        self.app.router.add_get(
            "/scim/v2/ResourceTypes", self.handle_public_scim_v2_resource_types
        )
        self.app.router.add_get(
            "/scim/v2/ResourceTypes/{resource_type}",
            self.handle_public_scim_v2_resource_type_get,
        )
        self.app.router.add_get("/scim/v2/Schemas", self.handle_public_scim_v2_schemas)
        self.app.router.add_get(
            "/scim/v2/Schemas/{schema_id:.+}", self.handle_public_scim_v2_schema_get
        )
        self.app.router.add_post("/scim/v2/Bulk", self.handle_public_scim_v2_bulk)
        self.app.router.add_get(
            "/api/enterprise/api-tokens", self.handle_enterprise_api_tokens
        )
        self.app.router.add_post(
            "/api/enterprise/api-tokens", self.handle_enterprise_api_tokens_create
        )
        self.app.router.add_post(
            "/api/enterprise/api-tokens/{token_id}/revoke",
            self.handle_enterprise_api_token_revoke,
        )
        self.app.router.add_get(
            "/api/enterprise/projects", self.handle_enterprise_projects
        )
        self.app.router.add_post(
            "/api/enterprise/projects", self.handle_enterprise_projects_create
        )
        self.app.router.add_get(
            "/api/enterprise/channels", self.handle_enterprise_channels
        )
        self.app.router.add_post(
            "/api/enterprise/channels", self.handle_enterprise_channels_register
        )
        self.app.router.add_post(
            "/api/enterprise/channels/bind", self.handle_enterprise_channels_bind
        )
        self.app.router.add_get("/api/enterprise/audit", self.handle_enterprise_audit)
        self.app.router.add_get(
            "/api/enterprise/audit/export", self.handle_enterprise_audit_export
        )
        self.app.router.add_get(
            "/api/enterprise/policies", self.handle_enterprise_policies
        )
        self.app.router.add_post(
            "/api/enterprise/policies", self.handle_enterprise_policies_create
        )
        self.app.router.add_post(
            "/api/enterprise/policies/install-defaults",
            self.handle_enterprise_policies_install_defaults,
        )
        self.app.router.add_get(
            "/api/enterprise/approvals", self.handle_enterprise_approvals
        )
        self.app.router.add_post(
            "/api/enterprise/approvals/{request_id}/approve",
            self.handle_enterprise_approval_approve,
        )
        self.app.router.add_post(
            "/api/enterprise/approvals/{request_id}/deny",
            self.handle_enterprise_approval_deny,
        )
        self.app.router.add_get(
            "/api/enterprise/agent-capabilities",
            self.handle_enterprise_agent_capabilities,
        )
        self.app.router.add_get(
            "/api/enterprise/connectors/health", self.handle_enterprise_connector_health
        )
        self.app.router.add_get(
            "/api/enterprise/connectors/action-schemas",
            self.handle_enterprise_connector_schemas,
        )
        self.app.router.add_post(
            "/api/enterprise/connectors/execute",
            self.handle_enterprise_connector_execute,
        )
        self.app.router.add_get(
            "/api/enterprise/connectors/credentials",
            self.handle_enterprise_connector_credentials,
        )
        self.app.router.add_post(
            "/api/enterprise/connectors/credentials",
            self.handle_enterprise_connector_credentials_create,
        )
        self.app.router.add_post(
            "/api/enterprise/connectors/credentials/{credential_id}/revoke",
            self.handle_enterprise_connector_credential_revoke,
        )
        self.app.router.add_get("/api/agents", self.handle_agents)
        self.app.router.add_get("/api/v1/capabilities", self.handle_v1_capabilities)
        self.app.router.add_get("/api/v1/agents", self.handle_v1_agents)

        def session_handler(handler):
            return (
                handler
                if self._persistent_session_v1_ready()
                else self.handle_v1_session_not_ready
            )

        for method, path, handler in (
            ("POST", "/api/v1/sessions", self.handle_v1_sessions_create),
            ("GET", "/api/v1/sessions", self.handle_v1_sessions_list),
            ("GET", "/api/v1/sessions/{session_id}", self.handle_v1_session_get),
            ("PATCH", "/api/v1/sessions/{session_id}", self.handle_v1_session_patch),
            ("DELETE", "/api/v1/sessions/{session_id}", self.handle_v1_session_delete),
            (
                "GET",
                "/api/v1/sessions/{session_id}/snapshot",
                self.handle_v1_session_snapshot,
            ),
            (
                "GET",
                "/api/v1/sessions/{session_id}/messages",
                self.handle_v1_session_messages,
            ),
            (
                "POST",
                "/api/v1/sessions/{session_id}/runs",
                self.handle_v1_session_runs_create,
            ),
            (
                "GET",
                "/api/v1/sessions/{session_id}/runs/{run_id}",
                self.handle_v1_session_run_get,
            ),
            (
                "POST",
                "/api/v1/sessions/{session_id}/runs/{run_id}/cancel",
                self.handle_v1_session_run_cancel,
            ),
            (
                "GET",
                "/api/v1/sessions/{session_id}/events",
                self.handle_v1_session_events,
            ),
            (
                "POST",
                "/api/v1/sessions/{session_id}/event-consumers",
                self.handle_v1_event_consumer_create,
            ),
            (
                "POST",
                "/api/v1/sessions/{session_id}/event-consumers/{consumer_id}/ack",
                self.handle_v1_event_consumer_ack,
            ),
            (
                "POST",
                "/api/v1/sessions/{session_id}/fresh",
                self.handle_v1_session_fresh,
            ),
            (
                "POST",
                "/api/v1/sessions/{session_id}/attachments",
                self.handle_v1_attachment_stage,
            ),
            (
                "POST",
                "/api/v1/sessions/{session_id}/attachments/{attachment_id}/commit",
                self.handle_v1_attachment_commit,
            ),
            ("POST", "/api/v1/approvals/{approval_id}", self.handle_v1_approval_decide),
            (
                "GET",
                "/api/v1/agents/{agent_id}/promotion",
                self.handle_v1_promotion_get,
            ),
            (
                "POST",
                "/api/v1/agents/{agent_id}/promotion",
                self.handle_v1_promotion_post,
            ),
        ):
            self.app.router.add_route(method, path, session_handler(handler))
        self.app.router.add_get(
            "/api/agents/{name}/overview", self.handle_agent_overview
        )
        self.app.router.add_get("/api/transcript/{name}", self.handle_transcript_recent)
        self.app.router.add_get(
            "/api/transcript/{name}/poll", self.handle_transcript_poll
        )
        self.app.router.add_get(
            "/api/agents/{name}/requests/{request_id}/activity",
            self.handle_request_activity,
        )
        self.app.router.add_get(
            "/api/project-chat/{name}/{project}", self.handle_project_chat_log
        )
        self.app.router.add_post("/api/chat", self.handle_chat)
        self.app.router.add_post(
            "/api/browser/chat/send", self.handle_browser_chat_send
        )
        self.app.router.add_post("/api/bridge/message", self.handle_bridge_message)
        self.app.router.add_post("/api/bridge/reply", self.handle_bridge_reply)
        self.app.router.add_post(
            "/api/bridge/hchat-exchange", self.handle_hchat_exchange
        )
        self.app.router.add_post("/api/bridge/transfer", self.handle_bridge_transfer)
        self.app.router.add_post("/api/bridge/fork", self.handle_bridge_fork)
        self.app.router.add_post("/api/bridge/cos", self.handle_bridge_cos)
        self.app.router.add_get(
            "/api/bridge/transfer/{transfer_id}", self.handle_bridge_transfer_get
        )
        self.app.router.add_post("/api/bridge/spawn", self.handle_bridge_spawn)
        self.app.router.add_get(
            "/api/bridge/message/{message_id}", self.handle_bridge_message_get
        )
        self.app.router.add_get(
            "/api/bridge/thread/{thread_id}", self.handle_bridge_thread
        )
        self.app.router.add_get(
            "/api/bridge/capabilities/{agent}", self.handle_bridge_capabilities
        )
        self.app.router.add_get(
            "/api/admin/commands/{name}", self.handle_admin_commands
        )
        self.app.router.add_post("/api/admin/command", self.handle_admin_command)
        self.app.router.add_post(
            "/api/agents/{name}/command", self.handle_agent_command
        )
        self.app.router.add_get(
            "/api/agents/{name}/scheduler/jobs",
            self.handle_agent_scheduler_jobs,
        )
        self.app.router.add_get(
            "/api/agents/{name}/scheduler/status",
            self.handle_agent_scheduler_status,
        )
        self.app.router.add_get(
            "/api/agents/{name}/scheduler/runs",
            self.handle_agent_scheduler_runs,
        )
        self.app.router.add_post(
            "/api/agents/{name}/jobs/run", self.handle_agent_run_job
        )
        self.app.router.add_post(
            "/api/background-jobs", self.handle_background_jobs_start
        )
        self.app.router.add_get(
            "/api/background-jobs", self.handle_background_jobs_list
        )
        self.app.router.add_get(
            "/api/background-jobs/{job_id}", self.handle_background_jobs_get
        )
        self.app.router.add_get(
            "/api/background-jobs/{job_id}/tail", self.handle_background_jobs_tail
        )
        self.app.router.add_post(
            "/api/background-jobs/{job_id}/cancel", self.handle_background_jobs_cancel
        )
        self.app.router.add_post("/api/admin/smoke", self.handle_admin_smoke)
        self.app.router.add_post(
            "/api/admin/start-agent", self.handle_admin_start_agent
        )
        self.app.router.add_post("/api/admin/stop-agent", self.handle_admin_stop_agent)
        self.app.router.add_post("/api/admin/shutdown", self.handle_admin_shutdown)
        self.app.router.add_post("/api/admin/notify", self.handle_admin_notify)
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_post("/api/jobs/import", self.handle_jobs_import)
        self.runner = None
        self.site = None
        self.bind_host = None

    def _learn_reply_route(self, text: str, reply_route: dict) -> None:
        """Auto-learn sender's routing info from reply_route metadata in hchat messages."""
        try:
            from tools.hchat_send import parse_return_address, update_contact

            info = parse_return_address(text)
            if not info:
                return
            inst = reply_route.get("instance_id", info.get("instance_id", ""))
            host = reply_route.get("host", "")
            port = reply_route.get("port", 0)
            wb_port = reply_route.get("wb_port", port)
            ttl = reply_route.get("ttl", 3600)
            if inst and host and port:
                update_contact(
                    info["agent"], inst, host, port, wb_port=wb_port, ttl=ttl
                )
        except Exception:
            pass  # non-critical — don't break message delivery

    def _runtime_list(self) -> list:
        if self.orchestrator is not None:
            return list(getattr(self.orchestrator, "runtimes", []))
        return list(self.runtimes)

    def _load_agent_rows(self) -> list[dict]:
        raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        return [
            agent for agent in raw.get("agents", []) if agent.get("is_active", True)
        ]

    def _load_agent_capability_rows(self):
        capabilities_path = self.config_path.parent / "agent_capabilities.json"
        if not capabilities_path.exists():
            return []
        try:
            raw = json.loads(capabilities_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return []
        entries = raw.get("agents", raw)
        return entries if isinstance(entries, (list, dict)) else []

    def _runtime_map(self) -> dict:
        return {runtime.name: runtime for runtime in self._runtime_list()}

    def _is_governed_profile(self) -> bool:
        return (
            str(
                getattr(self.global_config, "deployment_profile", "personal")
                or "personal"
            )
            != "personal"
        )

    def _build_identity_service(self) -> IdentityService | None:
        if (
            str(
                getattr(self.global_config, "deployment_profile", "personal")
                or "personal"
            )
            == "personal"
        ):
            return None
        bridge_home = Path(
            getattr(self.global_config, "bridge_home", None) or self.config_path.parent
        )
        return IdentityService.from_path(bridge_home / "state" / "enterprise.sqlite")

    def _build_channel_registry(self) -> ChannelRegistry | None:
        if (
            str(
                getattr(self.global_config, "deployment_profile", "personal")
                or "personal"
            )
            == "personal"
        ):
            return None
        bridge_home = Path(
            getattr(self.global_config, "bridge_home", None) or self.config_path.parent
        )
        return ChannelRegistry.from_path(bridge_home / "state" / "enterprise.sqlite")

    def _build_audit_ledger(self) -> EnterpriseAuditLedger | None:
        if (
            str(
                getattr(self.global_config, "deployment_profile", "personal")
                or "personal"
            )
            == "personal"
        ):
            return None
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        if not org_id:
            return None
        bridge_home = Path(
            getattr(self.global_config, "bridge_home", None) or self.config_path.parent
        )
        return EnterpriseAuditLedger.from_path(
            bridge_home / "state" / "enterprise.sqlite", org_id=org_id
        )

    def _build_connector_credentials(self) -> ConnectorCredentialStore | None:
        if (
            str(
                getattr(self.global_config, "deployment_profile", "personal")
                or "personal"
            )
            == "personal"
        ):
            return None
        bridge_home = Path(
            getattr(self.global_config, "bridge_home", None) or self.config_path.parent
        )
        return ConnectorCredentialStore.from_path(
            bridge_home / "state" / "enterprise.sqlite"
        )

    def _build_connector_registry(self) -> ConnectorRegistry:
        registry = ConnectorRegistry(self._static_connectors)
        self.connector_registry_errors = []
        if not self._is_governed_profile() or self.connector_credentials is None:
            return registry
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        if not org_id:
            return registry
        factory = ConnectorFactory(secret_resolver=self.connector_secret_resolver)
        for credential in self.connector_credentials.list_credentials(org_id=org_id):
            if credential.connector_type in registry.list_types():
                continue
            try:
                registry.register(factory.build(credential))
            except Exception as exc:
                self.connector_registry_errors.append(
                    {
                        "credential_id": credential.id,
                        "connector_type": credential.connector_type,
                        "error": str(exc),
                    }
                )
        return registry

    def _refresh_connector_registry(self) -> None:
        self.connector_registry = self._build_connector_registry()

    def _enterprise_policy_evaluator(self) -> PolicyEvaluator | None:
        if not self._is_governed_profile():
            return None
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        if not org_id:
            return None
        bridge_home = Path(
            getattr(self.global_config, "bridge_home", None) or self.config_path.parent
        )
        return PolicyEvaluator.from_path(
            bridge_home / "state" / "enterprise.sqlite", org_id=org_id
        )

    def _build_audit_writer(self) -> AuditEventWriter:
        if not self._is_governed_profile():
            return AuditEventWriter(enabled=False)
        bridge_home = Path(
            getattr(self.global_config, "bridge_home", None) or self.config_path.parent
        )
        return AuditEventWriter(
            enabled=True,
            jsonl_path=bridge_home / "state" / "enterprise_audit.jsonl",
        )

    def _append_enterprise_audit(
        self,
        *,
        event_type: str,
        action: str,
        status: str,
        actor_id: str | int | None = None,
        context: dict | None = None,
    ) -> None:
        self.audit_writer.append(
            AuditEvent(
                event_type=event_type,
                actor_id=actor_id,
                action=action,
                status=status,
                context=context or {},
            )
        )

    def _enterprise_channel_gate(self) -> EnterpriseChannelGate:
        return EnterpriseChannelGate(
            governed=self._is_governed_profile(),
            org_id=str(getattr(self.global_config, "organization_id", "") or "").strip()
            or None,
            registry=self.channel_registry,
            audit_writer=self.audit_writer,
        )

    def _refresh_bridge_router(self) -> None:
        self.bridge_router.refresh(self._runtime_list())

    def _check_admin_auth(self, request) -> bool:
        if self._is_governed_profile():
            user = self._enterprise_user_from_request(request)
            if user is None:
                return False
            return self._enterprise_user_has_admin_role(user.id)
        if not self.admin_token:
            return True
        provided = (
            request.headers.get("X-Workbench-Token")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        return provided == self.admin_token

    def _bearer_token_from_request(self, request) -> str:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return request.headers.get("X-Workbench-Session", "").strip()

    def _enterprise_user_from_request(self, request):
        if self.identity_service is None:
            return None
        token = self._bearer_token_from_request(request)
        if not token:
            return None
        return self.identity_service.get_session_user(token)

    def _enterprise_user_has_admin_role(self, user_id: str) -> bool:
        if self.identity_service is None:
            return False
        admin_roles = {
            EnterpriseRole.ORG_ADMIN.value,
            EnterpriseRole.TEAM_ADMIN.value,
            EnterpriseRole.SECURITY_ADMIN.value,
            EnterpriseRole.SYSTEM_OPERATOR.value,
        }
        memberships = self.identity_service.list_project_memberships(user_id=user_id)
        return any(row.get("role") in admin_roles for row in memberships)

    def _enterprise_user_has_audit_reader_role(self, user_id: str) -> bool:
        if self.identity_service is None:
            return False
        audit_roles = {
            EnterpriseRole.ORG_ADMIN.value,
            EnterpriseRole.TEAM_ADMIN.value,
            EnterpriseRole.SECURITY_ADMIN.value,
            EnterpriseRole.SYSTEM_OPERATOR.value,
            EnterpriseRole.AUDITOR.value,
        }
        memberships = self.identity_service.list_project_memberships(user_id=user_id)
        return any(row.get("role") in audit_roles for row in memberships)

    def _enterprise_api_token_with_scope(self, request, *, required_scope: str):
        if self.identity_service is None:
            return None, None
        token_text = self._bearer_token_from_request(request)
        if not token_text:
            return None, None
        api_token = self.identity_service.validate_api_token(token_text)
        if api_token is None:
            return None, None
        scopes = set(api_token.scopes)
        implied = {"scim:write"} if required_scope == "scim:read" else set()
        if (
            required_scope not in scopes
            and "scim:*" not in scopes
            and not scopes.intersection(implied)
        ):
            return None, api_token
        user = self.identity_service.get_user(api_token.user_id)
        if user is None or user.status != "active":
            return None, api_token
        return api_token, user

    def _enterprise_scim_scope_error_response(self, request, *, required_scope: str):
        if not self._is_governed_profile():
            return web.json_response(
                {"schemas": [], "detail": "SCIM requires governed profile"}, status=404
            )
        if self.identity_service is None:
            return web.json_response(
                {"schemas": [], "detail": "identity service unavailable"}, status=503
            )
        api_token, user = self._enterprise_api_token_with_scope(
            request, required_scope=required_scope
        )
        if api_token is None or user is None:
            self._append_enterprise_audit(
                event_type="auth",
                action="scim_token_auth",
                status="denied",
                actor_id=getattr(api_token, "user_id", None),
                context={
                    "path": getattr(request, "path", ""),
                    "required_scope": required_scope,
                },
            )
            return web.json_response(
                {"schemas": [], "detail": "SCIM token auth failed"}, status=403
            )
        return None

    def _enterprise_scim_actor_from_request(self, request, *, required_scope: str):
        return self._enterprise_api_token_with_scope(
            request, required_scope=required_scope
        )

    def _enterprise_visible_project_ids(self, user_id: str) -> set[str]:
        if self.identity_service is None:
            return set()
        memberships = self.identity_service.list_project_memberships(user_id=user_id)
        return {
            str(row.get("project_id") or "").strip()
            for row in memberships
            if row.get("project_id")
        }

    def _agent_project_ids(self, agent_row: dict) -> set[str]:
        return agent_project_ids(agent_row)

    def _filter_enterprise_agent_rows_for_user(
        self, user, agent_rows: list[dict]
    ) -> list[dict]:
        if self._enterprise_user_has_admin_role(user.id):
            return agent_rows
        visible_project_ids = self._enterprise_visible_project_ids(user.id)
        if not visible_project_ids:
            return []
        visible_rows = []
        for agent_row in agent_rows:
            agent_project_ids = self._agent_project_ids(agent_row)
            if agent_project_ids and agent_project_ids.intersection(
                visible_project_ids
            ):
                visible_rows.append(agent_row)
        return visible_rows

    def _default_smoke_commands(self, runtime) -> list[str]:
        commands = ["/status", "/model"]
        available = set(supported_commands(runtime))
        if "backend" in available:
            commands.append("/backend")
        if "memory" in available:
            commands.append("/memory")
        if "effort" in available:
            commands.append("/effort")
        if "think" in available:
            commands.append("/think")
        return commands

    async def _wait_for_assistant_reply(
        self,
        transcript_path: Path,
        offset: int,
        timeout_s: float,
        expected_source: str | None = None,
        expected_prompt: str | None = None,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        current_offset = offset
        matched_prompt = False
        while time.monotonic() < deadline:
            data = _read_jsonl_increment(transcript_path, current_offset)
            current_offset = data.get("offset", current_offset)
            new_messages = data.get("messages", [])
            if expected_source or expected_prompt:
                for message in new_messages:
                    role = message.get("role")
                    text = message.get("text")
                    if not text:
                        continue
                    if role == "user":
                        source_ok = (
                            expected_source is None
                            or message.get("source") == expected_source
                        )
                        prompt_ok = expected_prompt is None or text == expected_prompt
                        if source_ok and prompt_ok:
                            matched_prompt = True
                            continue
                    if matched_prompt and role == "assistant":
                        return {
                            "received": True,
                            "offset": current_offset,
                            "assistant_text": text,
                            "new_messages": new_messages,
                        }
            else:
                assistants = [
                    m
                    for m in new_messages
                    if m.get("role") == "assistant" and m.get("text")
                ]
                if assistants:
                    return {
                        "received": True,
                        "offset": current_offset,
                        "assistant_text": assistants[-1]["text"],
                        "new_messages": new_messages,
                    }
            await asyncio.sleep(0.5)
        return {
            "received": False,
            "offset": current_offset,
            "assistant_text": None,
            "new_messages": [],
        }

    def _resolve_transcript_path(self, agent_row: dict, runtime) -> Path:
        if runtime is not None and getattr(runtime, "transcript_log_path", None):
            return Path(runtime.transcript_log_path)

        workspace_dir = resolve_path_value(
            agent_row["workspace_dir"],
            config_dir=self.config_path.parent,
            bridge_home=self.global_config.bridge_home,
        ) or (self.config_path.parent / agent_row["workspace_dir"])
        return workspace_dir / "transcript.jsonl"

    def _metadata_for_agent(self, agent_row: dict, runtime) -> dict:
        if runtime is not None:
            metadata = runtime.get_runtime_metadata()
        else:
            transcript_path = self._resolve_transcript_path(agent_row, runtime)
            workspace_dir = resolve_path_value(
                agent_row["workspace_dir"],
                config_dir=self.config_path.parent,
                bridge_home=self.global_config.bridge_home,
            ) or (self.config_path.parent / agent_row["workspace_dir"])
            engine = agent_row.get("engine") or agent_row.get(
                "active_backend", "unknown"
            )
            model = agent_row.get("model", "unknown")
            if agent_row.get("type") in {"flex", "limited"}:
                for backend in agent_row.get("allowed_backends", []):
                    if backend.get("engine") == agent_row.get("active_backend"):
                        model = backend.get("model", model)
                        break
            metadata = {
                "id": agent_row["name"],
                "name": agent_row["name"],
                "display_name": agent_row.get("display_name", agent_row["name"]),
                "emoji": agent_row.get("emoji", "🤖"),
                "engine": engine,
                "active_backend": agent_row.get("active_backend", engine),
                "model": model,
                "allowed_backends": [
                    dict(backend) for backend in agent_row.get("allowed_backends", [])
                ],
                "workspace_dir": str(workspace_dir),
                "transcript_path": str(transcript_path),
                "online": False,
                "status": "offline",
                "type": agent_row.get("type", "unknown"),
                "telegram_connected": False,
                "channels": {
                    "telegram": False,
                    "workbench": False,
                    "whatsapp": self._is_whatsapp_available(),
                },
            }
        return metadata

    def _is_whatsapp_available(self) -> bool:
        if self.orchestrator is None:
            return False
        wa = getattr(self.orchestrator, "whatsapp", None)
        if wa is None:
            return False
        is_connected = getattr(wa, "is_connected", None)
        if callable(is_connected):
            return bool(is_connected())
        return getattr(wa, "_client", None) is not None

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        bind_host = self._select_bind_host()
        self.bind_host = bind_host
        self.site = web.TCPSite(
            self.runner, bind_host, self.global_config.workbench_port
        )
        await self.site.start()

    def _select_bind_host(self) -> str:
        configured = str(
            getattr(self.global_config, "api_host", "") or "127.0.0.1"
        ).strip()
        if configured not in {"127.0.0.1", "localhost"}:
            return configured
        for candidate in ("10.255.255.254",):
            if self._host_can_bind(candidate):
                return candidate
        return "127.0.0.1"

    @staticmethod
    def _host_can_bind(host: str) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, 0))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    async def shutdown(self):
        if self.runner:
            await self.runner.cleanup()

    async def handle_auth_login(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {
                    "ok": False,
                    "error": "session login is only enabled for team/enterprise deployment profiles",
                },
                status=404,
            )
        if self.identity_service is None:
            return web.json_response(
                {"ok": False, "error": "identity service unavailable"}, status=503
            )
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        email = str(payload.get("email") or "").strip()
        password = str(payload.get("password") or "")
        if not org_id or not email or not password:
            return web.json_response(
                {"ok": False, "error": "org_id, email, and password are required"},
                status=400,
            )

        user = self.identity_service.authenticate_user(
            org_id=org_id, email=email, password=password
        )
        if user is None:
            self._append_enterprise_audit(
                event_type="auth",
                action="login",
                status="failed",
                actor_id=email,
                context={"org_id": org_id},
            )
            return web.json_response(
                {"ok": False, "error": "invalid credentials"}, status=401
            )
        session = self.identity_service.create_session(user_id=user.id)
        self._append_enterprise_audit(
            event_type="auth",
            action="login",
            status="success",
            actor_id=user.id,
            context={"org_id": org_id},
        )
        return web.json_response(
            {
                "ok": True,
                "session": {
                    "token": session.token,
                    "expires_at": session.expires_at,
                },
                "user": self._enterprise_user_payload(user),
            }
        )

    async def handle_auth_logout(self, request):
        if not self._is_governed_profile():
            return web.json_response({"ok": True})
        if self.identity_service is None:
            return web.json_response(
                {"ok": False, "error": "identity service unavailable"}, status=503
            )
        token = self._bearer_token_from_request(request)
        user = self.identity_service.get_session_user(token) if token else None
        revoked = self.identity_service.revoke_session(token) if token else False
        self._append_enterprise_audit(
            event_type="auth",
            action="logout",
            status="success" if revoked else "noop",
            actor_id=user.id if user else None,
        )
        return web.json_response({"ok": True, "revoked": revoked})

    async def handle_auth_me(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {"ok": True, "profile": "personal", "user": {"role": "owner"}}
            )
        user = self._enterprise_user_from_request(request)
        if user is None:
            return web.json_response(
                {"ok": False, "error": "not authenticated"}, status=401
            )
        return web.json_response(
            {
                "ok": True,
                "profile": "enterprise",
                "user": self._enterprise_user_payload(user),
            }
        )

    async def handle_auth_providers(self, request):
        configured = getattr(self.global_config, "enterprise_auth_providers", []) or []
        providers = load_auth_providers(
            configured if self._is_governed_profile() else []
        )
        return web.json_response(
            {
                "ok": True,
                "profile": str(
                    getattr(self.global_config, "deployment_profile", "personal")
                    or "personal"
                ),
                "providers": [provider.public_payload() for provider in providers],
            }
        )

    async def handle_auth_oidc_start(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {
                    "ok": False,
                    "error": "OIDC login is only enabled for governed profiles",
                },
                status=404,
            )
        provider_id = str(request.match_info.get("provider_id") or "").strip()
        redirect_uri = str(
            (getattr(request, "query", {}) or {}).get("redirect_uri") or ""
        ).strip()
        providers = load_auth_providers(
            getattr(self.global_config, "enterprise_auth_providers", []) or []
        )
        provider = next((item for item in providers if item.id == provider_id), None)
        if provider is None:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_start",
                status="failed",
                actor_id=provider_id,
                context={"error": "provider not found"},
            )
            return web.json_response(
                {"ok": False, "error": "OIDC provider not found"}, status=404
            )
        try:
            start = build_oidc_authorization_start(provider, redirect_uri=redirect_uri)
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_start",
                status="failed",
                actor_id=provider_id,
                context={"provider_id": provider_id, "error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._pending_oidc_flows[start.state] = start
        self._append_enterprise_audit(
            event_type="auth",
            action="oidc_start",
            status="success",
            actor_id=provider_id,
            context={"provider_id": provider_id, "expires_at": start.expires_at},
        )
        return web.json_response({"ok": True, "oidc": start.public_payload()})

    async def handle_auth_oidc_callback(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {
                    "ok": False,
                    "error": "OIDC login is only enabled for governed profiles",
                },
                status=404,
            )
        provider_id = str(request.match_info.get("provider_id") or "").strip()
        query = getattr(request, "query", {}) or {}
        state = str(query.get("state") or "").strip()
        code = str(query.get("code") or "").strip()
        provider_error = str(query.get("error") or "").strip()
        if provider_error:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="failed",
                actor_id=provider_id,
                context={
                    "provider_id": provider_id,
                    "error": provider_error,
                    "error_description": str(
                        query.get("error_description") or ""
                    ).strip()
                    or None,
                },
            )
            return web.json_response({"ok": False, "error": provider_error}, status=400)
        if not state:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="failed",
                actor_id=provider_id,
                context={"provider_id": provider_id, "error": "missing state"},
            )
            return web.json_response(
                {"ok": False, "error": "state is required"}, status=400
            )
        flow = self._pending_oidc_flows.get(state)
        if flow is None:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="failed",
                actor_id=provider_id,
                context={"provider_id": provider_id, "error": "invalid state"},
            )
            return web.json_response(
                {"ok": False, "error": "invalid OIDC state"}, status=400
            )
        if getattr(flow, "provider_id", None) != provider_id:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="failed",
                actor_id=provider_id,
                context={
                    "provider_id": provider_id,
                    "expected_provider_id": getattr(flow, "provider_id", None),
                    "error": "provider mismatch",
                },
            )
            return web.json_response(
                {"ok": False, "error": "OIDC provider mismatch"}, status=400
            )
        if not code:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="failed",
                actor_id=provider_id,
                context={"provider_id": provider_id, "error": "missing code"},
            )
            return web.json_response(
                {"ok": False, "error": "authorization code is required"}, status=400
            )
        providers = load_auth_providers(
            getattr(self.global_config, "enterprise_auth_providers", []) or []
        )
        provider = next((item for item in providers if item.id == provider_id), None)
        if provider is None:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="failed",
                actor_id=provider_id,
                context={"provider_id": provider_id, "error": "provider not found"},
            )
            return web.json_response(
                {"ok": False, "error": "OIDC provider not found"}, status=404
            )
        try:
            exchange = build_oidc_token_exchange_request(provider, flow, code=code)
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="failed",
                actor_id=provider_id,
                context={"provider_id": provider_id, "error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._pending_oidc_flows.pop(state, None)
        exchange_payload = exchange.public_payload()
        if bool(getattr(self.global_config, "enterprise_oidc_complete_login", False)):
            try:
                token_response = exchange_oidc_authorization_code(
                    exchange,
                    transport=self._oidc_token_transport,
                )
                jwks = self._oidc_jwks_cache.get(
                    provider,
                    fetcher=lambda selected_provider: fetch_oidc_jwks(
                        selected_provider,
                        transport=self._oidc_jwks_transport,
                    ),
                )
                validated = verify_oidc_id_token(
                    provider, flow, token_response.id_token, jwks
                )
                org_id = str(
                    getattr(self.global_config, "organization_id", "") or ""
                ).strip()
                mapped = map_oidc_claims(
                    provider_id=provider.id, org_id=org_id, claims=validated.claims
                )
                default_project_id = (
                    f"{org_id}-default"
                    if org_id and self.identity_service.get_project(f"{org_id}-default")
                    else None
                )
                completion = complete_oidc_session(
                    identity_service=self.identity_service,
                    mapped_identity=mapped,
                    default_project_id=default_project_id,
                )
            except Exception as exc:
                self._append_enterprise_audit(
                    event_type="auth",
                    action="oidc_callback",
                    status="failed",
                    actor_id=provider_id,
                    context={
                        "provider_id": provider_id,
                        "state_validated": True,
                        "token_exchange": "failed",
                        "error": str(exc),
                    },
                )
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
            self._append_enterprise_audit(
                event_type="auth",
                action="oidc_callback",
                status="success",
                actor_id=completion.user.id,
                context={
                    "provider_id": provider_id,
                    "state_validated": True,
                    "token_exchange": "completed",
                    "user_created": completion.user_created,
                    "default_project_id": completion.default_project_id,
                },
            )
            return web.json_response(
                {
                    "ok": True,
                    "oidc": {
                        "provider_id": provider_id,
                        "state": state,
                        "code_received": True,
                        "token_exchange": "completed",
                        "token_response": token_response.public_payload(),
                    },
                    **completion.public_payload(),
                }
            )
        self._append_enterprise_audit(
            event_type="auth",
            action="oidc_callback",
            status="validated",
            actor_id=provider_id,
            context={
                "provider_id": provider_id,
                "state_validated": True,
                "token_exchange": "prepared",
                "token_endpoint": exchange.token_endpoint,
            },
        )
        return web.json_response(
            {
                "ok": True,
                "oidc": {
                    "provider_id": provider_id,
                    "state": state,
                    "code_received": True,
                    "token_exchange": "prepared",
                    "token_exchange_request": exchange_payload,
                },
            }
        )

    async def handle_auth_saml_start(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {
                    "ok": False,
                    "error": "SAML login is only enabled for governed profiles",
                },
                status=404,
            )
        provider_id = str(
            getattr(request, "match_info", {}).get("provider_id") or ""
        ).strip()
        providers = load_auth_providers(
            getattr(self.global_config, "enterprise_auth_providers", []) or []
        )
        provider = next((item for item in providers if item.id == provider_id), None)
        if provider is None:
            self._append_enterprise_audit(
                event_type="auth",
                action="saml_start",
                status="failed",
                actor_id=provider_id,
                context={"error": "provider_not_found"},
            )
            return web.json_response(
                {"ok": False, "error": "SAML provider not found"}, status=404
            )
        try:
            start = build_saml_authn_start(provider)
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="auth",
                action="saml_start",
                status="failed",
                actor_id=provider_id,
                context={"error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._pending_saml_flows[start.state] = start
        self._append_enterprise_audit(
            event_type="auth",
            action="saml_start",
            status="success",
            actor_id=provider_id,
            context={
                "provider_id": provider_id,
                "binding": start.binding,
                "request_id": start.request_id,
            },
        )
        return web.json_response({"ok": True, "saml": start.public_payload()})

    async def handle_auth_saml_callback(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {
                    "ok": False,
                    "error": "SAML login is only enabled for governed profiles",
                },
                status=404,
            )
        provider_id = str(
            getattr(request, "match_info", {}).get("provider_id") or ""
        ).strip()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        state = str(payload.get("RelayState") or payload.get("state") or "").strip()
        flow = self._pending_saml_flows.get(state)
        if flow is None:
            self._append_enterprise_audit(
                event_type="auth",
                action="saml_callback",
                status="failed",
                actor_id=provider_id,
                context={"error": "invalid_state"},
            )
            return web.json_response(
                {"ok": False, "error": "invalid SAML state"}, status=400
            )
        if getattr(flow, "provider_id", None) != provider_id:
            self._append_enterprise_audit(
                event_type="auth",
                action="saml_callback",
                status="failed",
                actor_id=provider_id,
                context={"error": "provider_mismatch"},
            )
            return web.json_response(
                {"ok": False, "error": "SAML provider mismatch"}, status=400
            )
        providers = load_auth_providers(
            getattr(self.global_config, "enterprise_auth_providers", []) or []
        )
        provider = next((item for item in providers if item.id == provider_id), None)
        if provider is None:
            return web.json_response(
                {"ok": False, "error": "SAML provider not found"}, status=404
            )
        assertion_xml = _saml_assertion_xml_from_payload(payload)
        signature_verified = False
        try:
            if self._saml_assertion_verifier is not None:
                verified = self._saml_assertion_verifier(
                    provider, assertion_xml, payload
                )
                if isinstance(verified, tuple):
                    assertion_xml, signature_verified = verified
                else:
                    signature_verified = bool(verified)
            elif getattr(
                self.global_config,
                "enterprise_saml_allow_preverified_assertions",
                False,
            ):
                signature_verified = bool(payload.get("signature_verified")) and bool(
                    getattr(
                        self.global_config,
                        "enterprise_saml_allow_preverified_assertions",
                        False,
                    )
                )
            else:
                signature_verified = verify_saml_assertion_signature(
                    assertion_xml, provider
                )
            claims = validate_saml_assertion(
                assertion_xml,
                expected_issuer=flow.idp_entity_id,
                expected_audience=flow.sp_entity_id,
                signature_verified=signature_verified,
            )
            org_id = str(
                getattr(self.global_config, "organization_id", "") or ""
            ).strip()
            default_project_id = (
                str(
                    provider.config.get("default_project_id") or f"{org_id}-default"
                ).strip()
                or None
            )
            user, created = self.identity_service.upsert_oidc_user(
                org_id=org_id,
                email=claims.email,
                display_name=claims.display_name,
                default_project_id=default_project_id,
            )
            session = self.identity_service.create_session(user_id=user.id)
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="auth",
                action="saml_callback",
                status="failed",
                actor_id=provider_id,
                context={"error": str(exc), "state_validated": True},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._pending_saml_flows.pop(state, None)
        self._append_enterprise_audit(
            event_type="auth",
            action="saml_callback",
            status="success",
            actor_id=user.id,
            context={
                "provider_id": provider_id,
                "request_id": flow.request_id,
                "user_created": created,
                "signature_verified": signature_verified,
            },
        )
        return web.json_response(
            {
                "ok": True,
                "saml": {
                    "provider_id": provider_id,
                    "state": state,
                    "request_id": flow.request_id,
                    "signature_verified": signature_verified,
                },
                "user": self._enterprise_user_payload(user),
                "session": {"token": session.token, "expires_at": session.expires_at},
            }
        )

    def _enterprise_user_payload(self, user) -> dict:
        memberships = (
            self.identity_service.list_project_memberships(user_id=user.id)
            if self.identity_service is not None
            else []
        )
        return {
            "id": user.id,
            "org_id": user.org_id,
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
            "memberships": memberships,
        }

    def _enterprise_project_payload(self, project) -> dict:
        return {
            "id": project.id,
            "org_id": project.org_id,
            "name": project.name,
            "workspace_root": project.workspace_root,
            "created_at": project.created_at,
        }

    def _enterprise_api_token_payload(
        self, token, *, include_plaintext: bool = False
    ) -> dict:
        payload = {
            "id": token.id,
            "user_id": token.user_id,
            "scopes": list(token.scopes),
            "expires_at": token.expires_at,
            "created_at": token.created_at,
            "revoked_at": token.revoked_at,
        }
        if include_plaintext:
            payload["token"] = token.token
        return payload

    def _enterprise_channel_payload(
        self, channel, *, include_bindings: bool = True
    ) -> dict:
        bindings = []
        if include_bindings and self.channel_registry is not None:
            bindings = [
                {
                    "channel_id": binding.channel_id,
                    "scope_type": binding.scope_type,
                    "scope_id": binding.scope_id,
                    "permission": binding.permission,
                    "created_at": binding.created_at,
                }
                for binding in self.channel_registry.list_bindings(
                    channel_id=channel.id
                )
            ]
        return {
            "id": channel.id,
            "org_id": channel.org_id,
            "type": channel.type,
            "display_name": channel.display_name,
            "enabled": channel.enabled,
            "risk_tier": channel.risk_tier,
            "created_at": channel.created_at,
            "updated_at": channel.updated_at,
            "bindings": bindings,
        }

    def _enterprise_connector_credential_payload(self, credential) -> dict:
        return {
            "id": credential.id,
            "org_id": credential.org_id,
            "connector_type": credential.connector_type,
            "display_name": credential.display_name,
            "secret_ref": credential.secret_ref,
            "scopes": list(credential.scopes),
            "status": credential.status,
            "created_at": credential.created_at,
            "revoked_at": credential.revoked_at,
        }

    def _enterprise_approval_payload(self, approval) -> dict:
        return {
            "id": approval.id,
            "org_id": approval.org_id,
            "actor_id": approval.actor_id,
            "action": approval.action,
            "resource": approval.resource,
            "status": approval.status,
            "rule_id": approval.rule_id,
            "reason": approval.reason,
            "context": approval.context,
            "created_at": approval.created_at,
            "decided_by": approval.decided_by,
            "decided_at": approval.decided_at,
            "decision_reason": approval.decision_reason,
        }

    def _enterprise_policy_rule_payload(self, rule) -> dict:
        return {
            "id": rule.id,
            "org_id": rule.org_id,
            "scope_type": rule.scope_type,
            "scope_id": rule.scope_id,
            "action": rule.action,
            "resource": rule.resource,
            "effect": rule.effect.value,
            "conditions": rule.conditions,
            "priority": rule.priority,
            "created_at": rule.created_at,
        }

    def _enterprise_admin_error_response(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {"ok": False, "error": "enterprise API requires governed profile"},
                status=404,
            )
        if self.identity_service is None:
            return web.json_response(
                {"ok": False, "error": "identity service unavailable"}, status=503
            )
        if not self._check_admin_auth(request):
            self._append_enterprise_audit(
                event_type="admin_api",
                action="admin_auth",
                status="denied",
                context={"path": getattr(request, "path", "")},
            )
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )
        return None

    def _enterprise_audit_read_error_response(self, request):
        if not self._is_governed_profile():
            return web.json_response(
                {"ok": False, "error": "enterprise API requires governed profile"},
                status=404,
            )
        if self.identity_service is None:
            return web.json_response(
                {"ok": False, "error": "identity service unavailable"}, status=503
            )
        user = self._enterprise_user_from_request(request)
        if user is None or not self._enterprise_user_has_audit_reader_role(user.id):
            self._append_enterprise_audit(
                event_type="admin_api",
                action="audit_read_auth",
                status="denied",
                actor_id=user.id if user else None,
                context={"path": getattr(request, "path", "")},
            )
            return web.json_response(
                {"ok": False, "error": "audit read auth failed"}, status=403
            )
        return None

    async def handle_enterprise_users(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        users = [
            self._enterprise_user_payload(user)
            for user in self.identity_service.list_users(org_id=org_id)
        ]
        return web.json_response({"ok": True, "users": users})

    async def handle_enterprise_users_create(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        email = str(payload.get("email") or "").strip()
        display_name = str(payload.get("display_name") or "").strip()
        password = str(payload.get("password") or "")
        if not org_id or not email or not display_name or not password:
            return web.json_response(
                {
                    "ok": False,
                    "error": "org_id, email, display_name, and password are required",
                },
                status=400,
            )
        try:
            user = self.identity_service.create_user(
                org_id=org_id,
                email=email,
                display_name=display_name,
                password=password,
                user_id=payload.get("user_id"),
            )
            project_id = str(payload.get("project_id") or "").strip()
            role = str(payload.get("role") or "").strip()
            if project_id and role:
                self.identity_service.assign_project_role(
                    user_id=user.id, project_id=project_id, role=role
                )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="user_create",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="user_create",
            status="success",
            actor_id=actor.id if actor else None,
            context={"target_user_id": user.id, "org_id": org_id},
        )
        return web.json_response(
            {"ok": True, "user": self._enterprise_user_payload(user)}, status=201
        )

    async def handle_enterprise_scim_users_upsert(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        scim_payload = (
            payload.get("scim") if isinstance(payload.get("scim"), dict) else payload
        )
        default_project_id = (
            str(payload.get("default_project_id") or "").strip() or None
        )
        default_role = (
            str(payload.get("default_role") or "").strip() or "individual_user"
        )
        try:
            result = ScimProvisioningService(self.identity_service).upsert_user(
                org_id=org_id,
                payload=scim_payload,
                default_project_id=default_project_id,
                default_role=default_role,
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="scim_user_upsert",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc), "org_id": org_id},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="scim_user_upsert",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "target_user_id": result.user.id,
                "org_id": org_id,
                "created": result.created,
                "provisioning_action": result.action,
                "external_id": result.external_id,
            },
        )
        return web.json_response(
            {
                "ok": True,
                "scim": result.to_dict(),
                "user": self._enterprise_user_payload(result.user),
            },
            status=201 if result.created else 200,
        )

    async def handle_enterprise_scim_users_deactivate(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        user_name = str(payload.get("userName") or payload.get("email") or "").strip()
        if not user_name:
            return web.json_response(
                {"ok": False, "error": "userName or email is required"}, status=400
            )
        try:
            result = ScimProvisioningService(self.identity_service).deactivate_user(
                org_id=org_id, user_name=user_name
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="scim_user_deactivate",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc), "org_id": org_id},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="scim_user_deactivate",
            status="success",
            actor_id=actor.id if actor else None,
            context={"target_user_id": result.user.id, "org_id": org_id},
        )
        return web.json_response(
            {
                "ok": True,
                "scim": result.to_dict(),
                "user": self._enterprise_user_payload(result.user),
            }
        )

    async def handle_enterprise_scim_v2_users_list(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        query = getattr(request, "query", {}) or {}
        org_id = str(
            query.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        filter_expression = str(query.get("filter") or "").strip() or None
        try:
            start_index = max(1, int(str(query.get("startIndex") or "1")))
            count = max(0, min(500, int(str(query.get("count") or "100"))))
            payload = ScimProvisioningService(
                self.identity_service
            ).list_user_resources(
                org_id=org_id,
                filter_expression=filter_expression,
                start_index=start_index,
                count=count,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(payload)

    async def handle_enterprise_scim_v2_users_create(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        try:
            result = ScimProvisioningService(self.identity_service).upsert_user(
                org_id=org_id, payload=payload
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="scim_v2_user_create",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc), "org_id": org_id},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="scim_v2_user_create",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "target_user_id": result.user.id,
                "org_id": org_id,
                "created": result.created,
                "provisioning_action": result.action,
            },
        )
        return web.json_response(
            scim_user_resource(result.user), status=201 if result.created else 200
        )

    async def handle_enterprise_scim_v2_users_get(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        user_id = str(getattr(request, "match_info", {}).get("user_id") or "").strip()
        try:
            payload = ScimProvisioningService(self.identity_service).get_user_resource(
                user_id=user_id
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_enterprise_scim_v2_users_patch(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        user_id = str(getattr(request, "match_info", {}).get("user_id") or "").strip()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        try:
            result = ScimProvisioningService(self.identity_service).patch_user(
                user_id=user_id, payload=payload
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="scim_v2_user_patch",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc), "target_user_id": user_id},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="scim_v2_user_patch",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "target_user_id": result.user.id,
                "provisioning_action": result.action,
            },
        )
        return web.json_response(scim_user_resource(result.user))

    async def handle_enterprise_scim_v2_groups_list(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        query = getattr(request, "query", {}) or {}
        org_id = str(
            query.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        filter_expression = str(query.get("filter") or "").strip() or None
        try:
            start_index = max(1, int(str(query.get("startIndex") or "1")))
            count = max(0, min(500, int(str(query.get("count") or "100"))))
            payload = ScimProvisioningService(
                self.identity_service
            ).list_group_resources(
                org_id=org_id,
                filter_expression=filter_expression,
                start_index=start_index,
                count=count,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(payload)

    async def handle_enterprise_scim_v2_groups_get(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        query = getattr(request, "query", {}) or {}
        org_id = str(
            query.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        group_id = str(getattr(request, "match_info", {}).get("group_id") or "").strip()
        try:
            payload = ScimProvisioningService(self.identity_service).get_group_resource(
                org_id=org_id,
                group_id=group_id,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_enterprise_scim_v2_service_provider_config(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        return web.json_response(scim_service_provider_config())

    async def handle_enterprise_scim_v2_resource_types(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        return web.json_response(scim_resource_types())

    async def handle_enterprise_scim_v2_resource_type_get(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        resource_type = str(
            getattr(request, "match_info", {}).get("resource_type") or ""
        ).strip()
        try:
            payload = scim_resource_type(resource_type)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_enterprise_scim_v2_schemas(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        return web.json_response(scim_schemas())

    async def handle_enterprise_scim_v2_schema_get(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        schema_id = str(
            getattr(request, "match_info", {}).get("schema_id") or ""
        ).strip()
        try:
            payload = scim_schema(schema_id)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_enterprise_scim_v2_bulk(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"schemas": [], "detail": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        try:
            result = ScimProvisioningService(self.identity_service).bulk(
                org_id=org_id, payload=payload
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="scim_v2_bulk",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc), "org_id": org_id},
            )
            return web.json_response({"schemas": [], "detail": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="scim_v2_bulk",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "org_id": org_id,
                "operation_count": len(result.get("Operations") or []),
            },
        )
        return web.json_response(result)

    async def handle_public_scim_v2_users_list(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        _, actor = self._enterprise_scim_actor_from_request(
            request, required_scope="scim:read"
        )
        query = getattr(request, "query", {}) or {}
        filter_expression = str(query.get("filter") or "").strip() or None
        try:
            start_index = max(1, int(str(query.get("startIndex") or "1")))
            count = max(0, min(500, int(str(query.get("count") or "100"))))
            payload = ScimProvisioningService(
                self.identity_service
            ).list_user_resources(
                org_id=actor.org_id,
                filter_expression=filter_expression,
                start_index=start_index,
                count=count,
            )
        except Exception as exc:
            return web.json_response({"schemas": [], "detail": str(exc)}, status=400)
        return web.json_response(payload)

    async def handle_public_scim_v2_users_create(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:write"
        )
        if error is not None:
            return error
        api_token, actor = self._enterprise_scim_actor_from_request(
            request, required_scope="scim:write"
        )
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"schemas": [], "detail": "invalid JSON body"}, status=400
            )
        try:
            result = ScimProvisioningService(self.identity_service).upsert_user(
                org_id=actor.org_id, payload=payload
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="scim",
                action="scim_v2_user_create",
                status="failed",
                actor_id=actor.id,
                context={
                    "error": str(exc),
                    "api_token_id": getattr(api_token, "id", None),
                },
            )
            return web.json_response({"schemas": [], "detail": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="scim",
            action="scim_v2_user_create",
            status="success",
            actor_id=actor.id,
            context={
                "target_user_id": result.user.id,
                "created": result.created,
                "provisioning_action": result.action,
                "api_token_id": getattr(api_token, "id", None),
            },
        )
        return web.json_response(
            scim_user_resource(result.user), status=201 if result.created else 200
        )

    async def handle_public_scim_v2_users_get(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        _, actor = self._enterprise_scim_actor_from_request(
            request, required_scope="scim:read"
        )
        user_id = str(getattr(request, "match_info", {}).get("user_id") or "").strip()
        try:
            target = self.identity_service.get_user(user_id)
            if target is None or target.org_id != actor.org_id:
                raise ValueError(f"SCIM user not found: {user_id!r}")
            payload = ScimProvisioningService(self.identity_service).get_user_resource(
                user_id=user_id
            )
        except Exception as exc:
            return web.json_response({"schemas": [], "detail": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_public_scim_v2_users_patch(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:write"
        )
        if error is not None:
            return error
        api_token, actor = self._enterprise_scim_actor_from_request(
            request, required_scope="scim:write"
        )
        user_id = str(getattr(request, "match_info", {}).get("user_id") or "").strip()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"schemas": [], "detail": "invalid JSON body"}, status=400
            )
        try:
            target = self.identity_service.get_user(user_id)
            if target is None or target.org_id != actor.org_id:
                raise ValueError(f"SCIM user not found: {user_id!r}")
            result = ScimProvisioningService(self.identity_service).patch_user(
                user_id=user_id, payload=payload
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="scim",
                action="scim_v2_user_patch",
                status="failed",
                actor_id=actor.id,
                context={
                    "error": str(exc),
                    "target_user_id": user_id,
                    "api_token_id": getattr(api_token, "id", None),
                },
            )
            return web.json_response({"schemas": [], "detail": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="scim",
            action="scim_v2_user_patch",
            status="success",
            actor_id=actor.id,
            context={
                "target_user_id": result.user.id,
                "provisioning_action": result.action,
                "api_token_id": getattr(api_token, "id", None),
            },
        )
        return web.json_response(scim_user_resource(result.user))

    async def handle_public_scim_v2_groups_list(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        _, actor = self._enterprise_scim_actor_from_request(
            request, required_scope="scim:read"
        )
        query = getattr(request, "query", {}) or {}
        filter_expression = str(query.get("filter") or "").strip() or None
        try:
            start_index = max(1, int(str(query.get("startIndex") or "1")))
            count = max(0, min(500, int(str(query.get("count") or "100"))))
            payload = ScimProvisioningService(
                self.identity_service
            ).list_group_resources(
                org_id=actor.org_id,
                filter_expression=filter_expression,
                start_index=start_index,
                count=count,
            )
        except Exception as exc:
            return web.json_response({"schemas": [], "detail": str(exc)}, status=400)
        return web.json_response(payload)

    async def handle_public_scim_v2_groups_get(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        _, actor = self._enterprise_scim_actor_from_request(
            request, required_scope="scim:read"
        )
        group_id = str(getattr(request, "match_info", {}).get("group_id") or "").strip()
        try:
            payload = ScimProvisioningService(self.identity_service).get_group_resource(
                org_id=actor.org_id,
                group_id=group_id,
            )
        except Exception as exc:
            return web.json_response({"schemas": [], "detail": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_public_scim_v2_service_provider_config(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        return web.json_response(scim_service_provider_config())

    async def handle_public_scim_v2_resource_types(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        return web.json_response(scim_resource_types())

    async def handle_public_scim_v2_resource_type_get(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        resource_type = str(
            getattr(request, "match_info", {}).get("resource_type") or ""
        ).strip()
        try:
            payload = scim_resource_type(resource_type)
        except Exception as exc:
            return web.json_response({"schemas": [], "detail": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_public_scim_v2_schemas(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        return web.json_response(scim_schemas())

    async def handle_public_scim_v2_schema_get(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:read"
        )
        if error is not None:
            return error
        schema_id = str(
            getattr(request, "match_info", {}).get("schema_id") or ""
        ).strip()
        try:
            payload = scim_schema(schema_id)
        except Exception as exc:
            return web.json_response({"schemas": [], "detail": str(exc)}, status=404)
        return web.json_response(payload)

    async def handle_public_scim_v2_bulk(self, request):
        error = self._enterprise_scim_scope_error_response(
            request, required_scope="scim:write"
        )
        if error is not None:
            return error
        api_token, actor = self._enterprise_scim_actor_from_request(
            request, required_scope="scim:write"
        )
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"schemas": [], "detail": "invalid JSON body"}, status=400
            )
        try:
            result = ScimProvisioningService(self.identity_service).bulk(
                org_id=actor.org_id, payload=payload
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="scim",
                action="scim_v2_bulk",
                status="failed",
                actor_id=actor.id,
                context={
                    "error": str(exc),
                    "api_token_id": getattr(api_token, "id", None),
                },
            )
            return web.json_response({"schemas": [], "detail": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="scim",
            action="scim_v2_bulk",
            status="success",
            actor_id=actor.id,
            context={
                "operation_count": len(result.get("Operations") or []),
                "api_token_id": getattr(api_token, "id", None),
            },
        )
        return web.json_response(result)

    async def handle_enterprise_api_tokens(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        query = getattr(request, "query", {}) or {}
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        user_id = str(query.get("user_id") or "").strip() or None
        include_revoked = str(query.get("include_revoked") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            tokens = self.identity_service.list_api_tokens(
                org_id=org_id,
                user_id=user_id,
                include_revoked=include_revoked,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(
            {
                "ok": True,
                "api_tokens": [
                    self._enterprise_api_token_payload(token) for token in tokens
                ],
                "count": len(tokens),
            }
        )

    async def handle_enterprise_api_tokens_create(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        user_id = str(payload.get("user_id") or "").strip()
        scopes = payload.get("scopes")
        expires_at = str(payload.get("expires_at") or "").strip() or None
        if not user_id or not isinstance(scopes, list):
            return web.json_response(
                {"ok": False, "error": "user_id and scopes list are required"},
                status=400,
            )
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        try:
            target_user = self.identity_service.get_user(user_id)
            if target_user is None or target_user.org_id != org_id:
                raise ValueError("target user is not in this organization")
            token = self.identity_service.create_api_token(
                user_id=user_id,
                scopes=tuple(
                    str(scope).strip() for scope in scopes if str(scope).strip()
                ),
                expires_at=expires_at,
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="api_token_create",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"target_user_id": user_id, "error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="api_token_create",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "target_user_id": user_id,
                "token_id": token.id,
                "scopes": list(token.scopes),
                "expires_at": token.expires_at,
            },
        )
        return web.json_response(
            {
                "ok": True,
                "api_token": self._enterprise_api_token_payload(
                    token, include_plaintext=True
                ),
            },
            status=201,
        )

    async def handle_enterprise_api_token_revoke(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        token_id = str(request.match_info.get("token_id") or "").strip()
        if not token_id:
            return web.json_response(
                {"ok": False, "error": "token_id is required"}, status=400
            )
        tokens = self.identity_service.list_api_tokens(
            org_id=str(
                getattr(self.global_config, "organization_id", "") or ""
            ).strip(),
            include_revoked=True,
        )
        if token_id not in {token.id for token in tokens}:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="api_token_revoke",
                status="failed",
                actor_id=actor.id if actor else None,
                context={
                    "token_id": token_id,
                    "error": "token not found in organization",
                },
            )
            return web.json_response(
                {"ok": False, "error": "token not found in organization"}, status=404
            )
        revoked = self.identity_service.revoke_api_token_by_id(token_id)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="api_token_revoke",
            status="success" if revoked else "noop",
            actor_id=actor.id if actor else None,
            context={"token_id": token_id},
        )
        return web.json_response({"ok": True, "token_id": token_id, "revoked": revoked})

    async def handle_enterprise_projects(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        projects = [
            self._enterprise_project_payload(project)
            for project in self.identity_service.list_projects(org_id=org_id)
        ]
        return web.json_response({"ok": True, "projects": projects})

    async def handle_enterprise_projects_create(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        name = str(payload.get("name") or "").strip()
        if not org_id or not name:
            return web.json_response(
                {"ok": False, "error": "org_id and name are required"}, status=400
            )
        try:
            project = self.identity_service.create_project(
                org_id=org_id,
                name=name,
                workspace_root=payload.get("workspace_root"),
                project_id=payload.get("project_id"),
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="project_create",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="project_create",
            status="success",
            actor_id=actor.id if actor else None,
            context={"project_id": project.id, "org_id": org_id},
        )
        return web.json_response(
            {"ok": True, "project": self._enterprise_project_payload(project)},
            status=201,
        )

    async def handle_enterprise_channels(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        if self.channel_registry is None:
            return web.json_response(
                {"ok": False, "error": "channel registry unavailable"}, status=503
            )
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        channels = [
            self._enterprise_channel_payload(channel)
            for channel in self.channel_registry.ensure_default_channels(org_id=org_id)
        ]
        return web.json_response({"ok": True, "channels": channels})

    async def handle_enterprise_channels_register(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        if self.channel_registry is None:
            return web.json_response(
                {"ok": False, "error": "channel registry unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        channel_type = str(payload.get("type") or "").strip()
        if not org_id or not channel_type:
            return web.json_response(
                {"ok": False, "error": "org_id and type are required"}, status=400
            )
        try:
            channel = self.channel_registry.register_channel(
                org_id=org_id,
                channel_type=channel_type,
                display_name=payload.get("display_name"),
                config=payload.get("config")
                if isinstance(payload.get("config"), dict)
                else {},
                enabled=bool(payload.get("enabled", False)),
                risk_tier=str(payload.get("risk_tier") or "medium"),
                channel_id=payload.get("channel_id"),
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="channel_register",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"channel_type": channel_type, "error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="channel_register",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "channel_id": channel.id,
                "channel_type": channel.type,
                "enabled": channel.enabled,
                "risk_tier": channel.risk_tier,
            },
        )
        return web.json_response(
            {"ok": True, "channel": self._enterprise_channel_payload(channel)},
            status=201,
        )

    async def handle_enterprise_channels_bind(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        if self.channel_registry is None:
            return web.json_response(
                {"ok": False, "error": "channel registry unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        org_id = str(
            payload.get("org_id")
            or getattr(self.global_config, "organization_id", "")
            or ""
        ).strip()
        channel_type = str(payload.get("type") or "").strip()
        scope_type = str(payload.get("scope_type") or "").strip()
        scope_id = str(payload.get("scope_id") or "").strip()
        permission = str(payload.get("permission") or "both").strip()
        if not org_id or not channel_type or not scope_type or not scope_id:
            return web.json_response(
                {
                    "ok": False,
                    "error": "org_id, type, scope_type, and scope_id are required",
                },
                status=400,
            )
        try:
            binding = self.channel_registry.bind_channel(
                org_id=org_id,
                channel_type=channel_type,
                scope_type=scope_type,
                scope_id=scope_id,
                permission=permission,
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="channel_bind",
                status="failed",
                actor_id=actor.id if actor else None,
                context={
                    "channel_type": channel_type,
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "error": str(exc),
                },
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="channel_bind",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "channel_id": binding.channel_id,
                "channel_type": channel_type,
                "scope_type": binding.scope_type,
                "scope_id": binding.scope_id,
                "permission": binding.permission,
            },
        )
        return web.json_response(
            {
                "ok": True,
                "binding": {
                    "channel_id": binding.channel_id,
                    "scope_type": binding.scope_type,
                    "scope_id": binding.scope_id,
                    "permission": binding.permission,
                    "created_at": binding.created_at,
                },
            },
            status=201,
        )

    def _enterprise_audit_filters(self, request) -> dict:
        query = getattr(request, "query", {}) or {}
        filters = {
            "event_type": str(query.get("event_type") or "").strip() or None,
            "actor_id": str(query.get("actor_id") or "").strip() or None,
            "project_id": str(query.get("project_id") or "").strip() or None,
            "task_id": str(query.get("task_id") or "").strip() or None,
            "request_id": str(query.get("request_id") or "").strip() or None,
            "correlation_id": str(query.get("correlation_id") or "").strip() or None,
        }
        try:
            filters["limit"] = max(1, min(int(query.get("limit") or 100), 1000))
        except (TypeError, ValueError):
            filters["limit"] = 100
        return filters

    async def handle_enterprise_audit(self, request):
        error = self._enterprise_audit_read_error_response(request)
        if error is not None:
            return error
        if self.audit_ledger is None:
            return web.json_response(
                {"ok": False, "error": "audit ledger unavailable"}, status=503
            )
        filters = self._enterprise_audit_filters(request)
        events = self.audit_ledger.query(**filters)
        return web.json_response(
            {
                "ok": True,
                "events": [event.to_dict() for event in events],
                "count": len(events),
                "limit": filters["limit"],
            }
        )

    async def handle_enterprise_audit_export(self, request):
        error = self._enterprise_audit_read_error_response(request)
        if error is not None:
            return error
        if self.audit_ledger is None:
            return web.json_response(
                {"ok": False, "error": "audit ledger unavailable"}, status=503
            )
        filters = self._enterprise_audit_filters(request)
        export_format = (
            str((getattr(request, "query", {}) or {}).get("format") or "ndjson")
            .strip()
            .lower()
        )
        events = self.audit_ledger.query(**filters)
        if export_format in {"ndjson", "ledger"}:
            records = [event.to_dict() for event in events]
        elif export_format in {"siem", "ecs"}:
            records = [format_siem_event(event) for event in events]
        elif export_format in {"otel", "opentelemetry"}:
            records = [format_otel_log(event) for event in events]
        else:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"unsupported audit export format: {export_format}",
                },
                status=400,
            )
        lines = [
            json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records
        ]
        body = "\n".join(lines)
        if body:
            body += "\n"
        return web.Response(text=body, content_type="application/x-ndjson")

    async def handle_enterprise_policies(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        evaluator = self._enterprise_policy_evaluator()
        if evaluator is None:
            return web.json_response(
                {"ok": False, "error": "policy evaluator unavailable"}, status=503
            )
        rules = [
            self._enterprise_policy_rule_payload(rule)
            for rule in evaluator.list_rules()
        ]
        return web.json_response({"ok": True, "policies": rules, "count": len(rules)})

    async def handle_enterprise_policies_create(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        evaluator = self._enterprise_policy_evaluator()
        if evaluator is None:
            return web.json_response(
                {"ok": False, "error": "policy evaluator unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        conditions = (
            payload.get("conditions")
            if isinstance(payload.get("conditions"), dict)
            else {}
        )
        try:
            rule = evaluator.add_rule(
                action=payload.get("action"),
                resource=payload.get("resource") or "*",
                effect=payload.get("effect"),
                scope_type=payload.get("scope_type") or "org",
                scope_id=payload.get("scope_id"),
                conditions=conditions,
                priority=int(payload.get("priority") or 100),
                rule_id=payload.get("rule_id"),
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="policy_rule_create",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="policy_rule_create",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "policy_rule_id": rule.id,
                "effect": rule.effect.value,
                "action": rule.action,
            },
        )
        return web.json_response(
            {"ok": True, "policy": self._enterprise_policy_rule_payload(rule)},
            status=201,
        )

    async def handle_enterprise_policies_install_defaults(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        evaluator = self._enterprise_policy_evaluator()
        if evaluator is None:
            return web.json_response(
                {"ok": False, "error": "policy evaluator unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        rules = install_default_connector_policy(evaluator)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="policy_defaults_install",
            status="success",
            actor_id=actor.id if actor else None,
            context={"rule_ids": [rule.id for rule in rules]},
        )
        return web.json_response(
            {
                "ok": True,
                "policies": [
                    self._enterprise_policy_rule_payload(rule) for rule in rules
                ],
                "count": len(rules),
            }
        )

    async def handle_enterprise_approvals(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        evaluator = self._enterprise_policy_evaluator()
        if evaluator is None:
            return web.json_response(
                {"ok": False, "error": "policy evaluator unavailable"}, status=503
            )
        query = getattr(request, "query", {}) or {}
        status = str(query.get("status") or "pending").strip() or None
        approvals = evaluator.list_approval_requests(status=status)
        return web.json_response(
            {
                "ok": True,
                "approvals": [
                    self._enterprise_approval_payload(approval)
                    for approval in approvals
                ],
                "count": len(approvals),
            }
        )

    async def _handle_enterprise_approval_decision(self, request, *, status: str):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        evaluator = self._enterprise_policy_evaluator()
        if evaluator is None:
            return web.json_response(
                {"ok": False, "error": "policy evaluator unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        request_id = str(
            getattr(request, "match_info", {}).get("request_id") or ""
        ).strip()
        if not request_id:
            return web.json_response(
                {"ok": False, "error": "request_id is required"}, status=400
            )
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            approval = evaluator.decide_approval_request(
                request_id,
                status=status,
                decided_by=actor.id if actor else "unknown",
                reason=payload.get("reason") if isinstance(payload, dict) else None,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response(
            {"ok": True, "approval": self._enterprise_approval_payload(approval)}
        )

    async def handle_enterprise_approval_approve(self, request):
        return await self._handle_enterprise_approval_decision(
            request, status="approved"
        )

    async def handle_enterprise_approval_deny(self, request):
        return await self._handle_enterprise_approval_decision(request, status="denied")

    async def handle_enterprise_agent_capabilities(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        query = getattr(request, "query", {}) or {}
        project_id = str(query.get("project_id") or "").strip() or None
        registry = AgentCapabilityRegistry(
            agent_rows=self._load_agent_rows(),
            capability_rows=self._load_agent_capability_rows(),
        )
        summaries = [
            summary.to_dict() for summary in registry.list_agents(project_id=project_id)
        ]
        return web.json_response(
            {
                "ok": True,
                "agent_capabilities": summaries,
                "count": len(summaries),
                "project_id": project_id,
            }
        )

    async def handle_enterprise_connector_credentials(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        if self.connector_credentials is None:
            return web.json_response(
                {"ok": False, "error": "connector credentials unavailable"}, status=503
            )
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        query = getattr(request, "query", {}) or {}
        connector_type = str(query.get("connector_type") or "").strip() or None
        include_revoked = str(query.get("include_revoked") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        credentials = self.connector_credentials.list_credentials(
            org_id=org_id,
            connector_type=connector_type,
            include_revoked=include_revoked,
        )
        return web.json_response(
            {
                "ok": True,
                "credentials": [
                    self._enterprise_connector_credential_payload(item)
                    for item in credentials
                ],
                "count": len(credentials),
            }
        )

    async def handle_enterprise_connector_credentials_create(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        if self.connector_credentials is None:
            return web.json_response(
                {"ok": False, "error": "connector credentials unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        org_id = str(getattr(self.global_config, "organization_id", "") or "").strip()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        scopes = _connector_scopes_from_payload(payload.get("scopes"))
        validation_error = _validate_connector_credential_payload(payload, scopes)
        if validation_error:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="connector_credential_create",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": validation_error},
            )
            return web.json_response(
                {"ok": False, "error": validation_error}, status=400
            )
        try:
            credential = self.connector_credentials.create_credential(
                org_id=org_id,
                connector_type=payload.get("connector_type"),
                display_name=payload.get("display_name"),
                secret_ref=payload.get("secret_ref"),
                scopes=scopes,
                credential_id=payload.get("credential_id"),
            )
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="connector_credential_create",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="connector_credential_create",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "credential_id": credential.id,
                "connector_type": credential.connector_type,
            },
        )
        self._refresh_connector_registry()
        return web.json_response(
            {
                "ok": True,
                "credential": self._enterprise_connector_credential_payload(credential),
            },
            status=201,
        )

    async def handle_enterprise_connector_credential_revoke(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        if self.connector_credentials is None:
            return web.json_response(
                {"ok": False, "error": "connector credentials unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        credential_id = str(
            getattr(request, "match_info", {}).get("credential_id") or ""
        ).strip()
        if not credential_id:
            return web.json_response(
                {"ok": False, "error": "credential_id is required"}, status=400
            )
        try:
            credential = self.connector_credentials.revoke_credential(credential_id)
        except Exception as exc:
            self._append_enterprise_audit(
                event_type="admin_api",
                action="connector_credential_revoke",
                status="failed",
                actor_id=actor.id if actor else None,
                context={"credential_id": credential_id, "error": str(exc)},
            )
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._append_enterprise_audit(
            event_type="admin_api",
            action="connector_credential_revoke",
            status="success",
            actor_id=actor.id if actor else None,
            context={
                "credential_id": credential.id,
                "connector_type": credential.connector_type,
            },
        )
        self._refresh_connector_registry()
        return web.json_response(
            {
                "ok": True,
                "credential": self._enterprise_connector_credential_payload(credential),
            }
        )

    async def handle_enterprise_connector_health(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        health = [
            summary.to_dict()
            for summary in self.connector_registry.health_checks(
                ledger=self.audit_ledger
            )
        ]
        return web.json_response(
            {
                "ok": True,
                "healthy": all(item["ok"] for item in health),
                "connectors": health,
                "registry_errors": list(self.connector_registry_errors),
                "count": len(health),
            }
        )

    async def handle_enterprise_connector_schemas(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        schemas = connector_action_schemas()
        return web.json_response(
            {"ok": True, "schemas": schemas, "count": len(schemas)}
        )

    async def handle_enterprise_connector_execute(self, request):
        error = self._enterprise_admin_error_response(request)
        if error is not None:
            return error
        evaluator = self._enterprise_policy_evaluator()
        if evaluator is None or self.connector_credentials is None:
            return web.json_response(
                {"ok": False, "error": "connector execution unavailable"}, status=503
            )
        actor = self._enterprise_user_from_request(request)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON body"}, status=400
            )
        credential_id = str(payload.get("credential_id") or "").strip()
        if not credential_id:
            return web.json_response(
                {"ok": False, "error": "credential_id is required"}, status=400
            )
        parameters = (
            payload.get("parameters")
            if isinstance(payload.get("parameters"), dict)
            else {}
        )
        action = ConnectorAction(
            connector_type=str(payload.get("connector_type") or "").strip(),
            action=str(payload.get("action") or "").strip(),
            resource=str(payload.get("resource") or "*").strip() or "*",
            actor_id=actor.id if actor else None,
            project_id=str(payload.get("project_id") or "").strip() or None,
            task_id=str(payload.get("task_id") or "").strip() or None,
            request_id=str(payload.get("request_id") or "").strip() or None,
            correlation_id=str(payload.get("correlation_id") or "").strip() or None,
            dry_run=bool(payload.get("dry_run")),
            parameters=parameters,
        )
        if not action.connector_type or not action.action:
            return web.json_response(
                {"ok": False, "error": "connector_type and action are required"},
                status=400,
            )
        validation_error = validate_connector_action(action)
        if validation_error:
            return web.json_response(
                {"ok": False, "error": validation_error}, status=400
            )
        service = ConnectorExecutionService(
            registry=self.connector_registry,
            credential_store=self.connector_credentials,
            policy_evaluator=evaluator,
            ledger=self.audit_ledger,
        )
        execution = service.execute(action, credential_id=credential_id)
        return web.json_response(
            {
                "ok": execution.result.ok,
                "result": {
                    "ok": execution.result.ok,
                    "status": execution.result.status,
                    "message": execution.result.message,
                    "data": dict(execution.result.data or {}),
                },
                "gate": {
                    "allowed": execution.gate.allowed,
                    "reason": execution.gate.reason,
                    "credential_id": execution.gate.credential_id,
                    "policy_rule_id": execution.gate.policy_rule_id,
                    "approval_request_id": execution.gate.approval_request_id,
                },
            }
        )

    async def handle_agents(self, request):
        runtime_map = self._runtime_map()
        agent_rows = self._load_agent_rows()
        if self._is_governed_profile():
            user = self._enterprise_user_from_request(request)
            if user is None:
                return web.json_response(
                    {"ok": False, "error": "not authenticated"}, status=401
                )
            agent_rows = self._filter_enterprise_agent_rows_for_user(user, agent_rows)
        agents = [
            self._metadata_for_agent(agent_row, runtime_map.get(agent_row["name"]))
            for agent_row in agent_rows
        ]
        return web.json_response({"ok": True, "agents": agents})

    async def handle_agent_overview(self, request):
        name = str(request.match_info.get("name") or "").strip()
        runtime_map = self._runtime_map()
        agent_rows = self._load_agent_rows()
        if self._is_governed_profile():
            user = self._enterprise_user_from_request(request)
            if user is None:
                return web.json_response(
                    {"ok": False, "error": "not authenticated"}, status=401
                )
            agent_rows = self._filter_enterprise_agent_rows_for_user(user, agent_rows)
        agent_row = next((row for row in agent_rows if row["name"] == name), None)
        if agent_row is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )

        runtime = runtime_map.get(name)
        metadata = self._metadata_for_agent(agent_row, runtime)
        overview = build_agent_overview(
            metadata=metadata,
            workspace_dir=Path(metadata["workspace_dir"]),
            runtime=runtime,
        )
        response = web.json_response({"ok": True, "overview": overview})
        response.headers["Cache-Control"] = "no-store"
        return response

    async def handle_transcript_recent(self, request):
        name = request.match_info["name"]
        limit = max(1, min(int(request.query.get("limit", 50)), 200))
        runtime_map = self._runtime_map()
        agent_row = next(
            (row for row in self._load_agent_rows() if row["name"] == name), None
        )
        if agent_row is None:
            return web.json_response({"error": "agent not found"}, status=404)
        transcript_path = self._resolve_transcript_path(
            agent_row, runtime_map.get(name)
        )
        return web.json_response(_read_jsonl_recent(transcript_path, limit=limit))

    async def handle_transcript_poll(self, request):
        name = request.match_info["name"]
        offset = int(request.query.get("offset", 0))
        runtime_map = self._runtime_map()
        agent_row = next(
            (row for row in self._load_agent_rows() if row["name"] == name), None
        )
        if agent_row is None:
            return web.json_response({"error": "agent not found"}, status=404)
        transcript_path = self._resolve_transcript_path(
            agent_row, runtime_map.get(name)
        )
        return web.json_response(_read_jsonl_increment(transcript_path, offset=offset))

    async def handle_request_activity(self, request):
        """Return an owner-scoped live stream with a durable Run fallback."""

        name = str(request.match_info.get("name") or "").strip()
        request_id = str(request.match_info.get("request_id") or "").strip()
        runtime = self._runtime_map().get(name)
        if runtime is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "agent not found",
                    "error_code": "agent_not_found",
                },
                status=404,
            )
        store = getattr(runtime, "request_activity", None)
        if store is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "request activity unavailable",
                    "error_code": "request_activity_unavailable",
                },
                status=501,
            )
        try:
            after_sequence = max(0, int(request.query.get("after_sequence", 0)))
            limit = max(1, min(int(request.query.get("limit", 100)), 256))
        except (TypeError, ValueError):
            return web.json_response(
                {
                    "ok": False,
                    "error": "invalid activity cursor",
                    "error_code": "invalid_activity_cursor",
                },
                status=400,
            )
        owner_id = self._v1_owner_id(request)
        if owner_id is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "not authenticated",
                    "error_code": "not_authenticated",
                },
                status=401,
            )
        try:
            run = self.session_store.get_run_by_request(
                request_id,
                owner_id=owner_id,
                agent_id=name,
            )
        except SessionNotFound:
            return web.json_response(
                {
                    "ok": False,
                    "error": "request activity not found",
                    "error_code": "request_activity_not_found",
                },
                status=404,
            )
        result = store.poll(request_id, after_sequence=after_sequence, limit=limit)
        identity = {
            "session_id": run["session_id"],
            "run_id": run["run_id"],
            "request_id": run["request_id"],
            "agent_id": run["agent_id"],
            "context_generation": int(run["context_generation"]),
        }
        if result.get("ok"):
            result.update(identity)
            return web.json_response(result)

        def _epoch(value: object) -> float | None:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None

        state = str(run.get("state") or "queued")
        terminal = state in TERMINAL_RUN_STATES
        recovered = {
            "ok": True,
            **identity,
            "state": state,
            "terminal": terminal,
            "success": state == "completed" if terminal else None,
            "created_at": _epoch(run.get("created_at")),
            "started_at": _epoch(run.get("started_at")),
            "completed_at": _epoch(run.get("completed_at")),
            "latest_sequence": after_sequence,
            "events": [],
            "recovered_from": "session_store",
        }
        return web.json_response(recovered)

    async def handle_project_chat_log(self, request):
        name = request.match_info["name"]
        project = request.match_info["project"]
        limit = int(request.query.get("limit", 100))
        agent_row = next(
            (row for row in self._load_agent_rows() if row["name"] == name), None
        )
        if agent_row is None:
            return web.json_response({"error": "agent not found"}, status=404)
        import re

        slug = re.sub(r"['\"]", "", project.lower())
        slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_") or "default"
        workspace_dir = Path(
            agent_row.get("workspace_dir")
            or (self.global_config.project_root / "workspaces" / name)
        )
        chat_log = workspace_dir / "projects" / slug / "chat_log.jsonl"
        if not chat_log.exists():
            return web.json_response({"entries": [], "count": 0})
        entries = []
        with open(chat_log, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        return web.json_response({"entries": entries[-limit:], "count": len(entries)})

    def _v1_owner_id(self, request) -> str | None:
        if self._is_governed_profile():
            user = self._enterprise_user_from_request(request)
            return f"enterprise:{user.id}" if user is not None else None
        return SessionStore.owner_id_for(self.global_config)

    def _persistent_session_v1_ready(self) -> bool:
        return bool(
            PERSISTENT_SESSION_V1_QUALIFIED
            and getattr(self.global_config, "persistent_session_v1", False)
        )

    async def handle_v1_session_not_ready(self, request):
        del request
        return web.json_response(
            {
                "ok": False,
                "code": "session_api_not_ready",
                "error": "Persistent Session API v1 has not passed qualification.",
            },
            status=503,
        )

    @staticmethod
    def _v1_error(error: Exception, *, status: int | None = None):
        resolved_status = status
        if resolved_status is None:
            if isinstance(error, SessionNotFound):
                resolved_status = 404
            elif isinstance(error, (SessionConflict, IdempotencyConflict)):
                resolved_status = 409
            elif isinstance(error, (ValueError, TypeError)):
                resolved_status = 400
            else:
                resolved_status = 500
        return web.json_response(
            {
                "ok": False,
                "error": str(error),
                "code": str(getattr(error, "code", "invalid_request")),
            },
            status=resolved_status,
        )

    async def handle_v1_capabilities(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        capabilities: dict[str, Any] = {"ok": True}
        if self._persistent_session_v1_ready():
            capabilities.update(
                {
                    "session_api_version": "1.0",
                    "event_schema_version": "1.0",
                    "control_schema_version": "1.0",
                    "attachment_schema_version": "1.0",
                    "approval_schema_version": "1.0",
                    "fencing_schema_version": "1.0",
                    "compatibility_policy": "capabilities-and-advertised-limits",
                    "durability": "sqlite-wal",
                    "controls": [
                        "fresh",
                        "archive",
                        "promotion",
                        "event_consumer_ack",
                        "cancel",
                        "attachments",
                        "approvals",
                        "fencing",
                        "restart_interruption",
                    ],
                    "event_delivery": "cursor-polling-at-least-once",
                    "max_message_chars": 200000,
                    "limits": {
                        "max_message_chars": 200000,
                        "max_sessions_page_size": 100,
                        "max_messages_page_size": 200,
                        "max_events_page_size": 2000,
                    },
                }
            )
        return web.json_response(capabilities)

    async def handle_v1_agents(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        agents = [
            {
                "agent_id": runtime.name,
                "display_name": runtime.get_display_name()
                if hasattr(runtime, "get_display_name")
                else runtime.name,
                "available": bool(getattr(runtime, "backend_ready", True)),
            }
            for runtime in self._runtime_list()
        ]
        return web.json_response({"ok": True, "agents": agents})

    async def handle_v1_sessions_create(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            agent_id = (
                str(payload.get("agent_id") or payload.get("agent") or "")
                .strip()
                .lower()
            )
            if agent_id not in self._runtime_map():
                raise SessionNotFound("agent not found")
            session = self.session_store.create_session(
                owner_id=owner,
                agent_id=agent_id,
                title=str(payload.get("title") or "New session"),
            )
            surface = str(payload.get("surface") or "").strip().lower()
            channel_key = str(
                payload.get("channel_key") or payload.get("client_id") or ""
            ).strip()
            if surface and channel_key:
                self.session_store.bind_channel(
                    owner_id=owner,
                    agent_id=agent_id,
                    surface=surface,
                    channel_key=channel_key,
                    session_id=session["session_id"],
                )
            return web.json_response({"ok": True, "session": session}, status=201)
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_sessions_list(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            rows = self.session_store.list_sessions(
                owner_id=owner,
                agent_id=str(request.query.get("agent_id") or "") or None,
                include_archived=str(
                    request.query.get("include_archived") or ""
                ).lower()
                in {"1", "true", "yes"},
                limit=int(request.query.get("limit") or 100),
            )
            return web.json_response(
                {"ok": True, "sessions": rows, "next_cursor": None}
            )
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_get(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            session = self.session_store.get_session(
                request.match_info["session_id"], owner_id=owner
            )
            return web.json_response({"ok": True, "session": session})
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_patch(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            expected = payload.get("expected_revision")
            if expected is None:
                expected = str(request.headers.get("If-Match") or "").strip('"')
            if expected in {None, ""}:
                raise ValueError("expected_revision or If-Match is required")
            session = self.session_store.update_session(
                request.match_info["session_id"],
                owner_id=owner,
                expected_revision=int(expected),
                title=payload.get("title"),
            )
            response = web.json_response({"ok": True, "session": session})
            response.headers["ETag"] = f'"{session["revision"]}"'
            return response
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_delete(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            self.session_store.get_session(
                request.match_info["session_id"], owner_id=owner
            )
            session = self.session_store.archive_session(
                request.match_info["session_id"], deleted=True
            )
            return web.json_response(
                {"ok": True, "session": session, "records_deleted": False}
            )
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_snapshot(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            return web.json_response(
                {
                    "ok": True,
                    "snapshot": self.session_store.snapshot(
                        request.match_info["session_id"], owner_id=owner
                    ),
                }
            )
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_messages(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            rows = self.session_store.messages(
                request.match_info["session_id"],
                owner_id=owner,
                after_ordinal=int(request.query.get("after_ordinal") or 0),
                limit=int(request.query.get("limit") or 200),
            )
            return web.json_response(
                {"ok": True, "messages": rows, "next_cursor": None}
            )
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_runs_create(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            session = self.session_store.get_session(
                request.match_info["session_id"], owner_id=owner, include_deleted=False
            )
            runtime = self._runtime_map().get(session["agent_id"])
            if runtime is None:
                raise SessionNotFound("agent runtime not found")
            message = payload.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                raise ValueError("message.content must be a list")
            text_blocks = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "\n".join(part for part in text_blocks if part).strip()
            if not text:
                raise ValueError("a non-empty text content block is required")
            idempotency_key = str(
                payload.get("idempotency_key")
                or request.headers.get("Idempotency-Key")
                or ""
            ).strip()
            if not idempotency_key:
                raise ValueError("idempotency_key is required")
            client_id = str(
                request.headers.get("X-Client-Id")
                or payload.get("client_id")
                or "default"
            )
            surface = str(payload.get("surface") or "session-api").strip().lower()
            if (
                not surface
                or len(surface) > 80
                or any(
                    character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                    for character in surface
                )
            ):
                raise ValueError("surface must be a lowercase protocol identifier")
            request_id = await runtime.enqueue_request(
                runtime._primary_chat_id(),
                text,
                "session-api",
                text[:160],
                deliver_to_telegram=False,
                idempotency_key=idempotency_key,
                request_metadata={
                    "session_id": session["session_id"],
                    "owner_id": owner,
                    "session_surface": surface,
                    "session_channel_key": client_id,
                    "execution_mode": payload.get("execution_mode"),
                    "parent_run_id": payload.get("parent_run_id"),
                },
            )
            if not request_id:
                raise SessionConflict("request was not accepted")
            run = self.session_store.get_run_by_request(request_id)
            return web.json_response(
                {
                    "ok": True,
                    "session_id": session["session_id"],
                    "run_id": run["run_id"],
                    "message_id": run["user_message_id"],
                    "request_id": request_id,
                    "state": run["state"],
                },
                status=202,
            )
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_run_get(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            run = self.session_store.get_run(
                request.match_info["run_id"], owner_id=owner
            )
            if run["session_id"] != request.match_info["session_id"]:
                raise SessionNotFound("run not found in Session")
            return web.json_response({"ok": True, "run": run})
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_run_cancel(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            run = self.session_store.get_run(
                request.match_info["run_id"], owner_id=owner
            )
            if run["session_id"] != request.match_info["session_id"]:
                raise SessionNotFound("run not found in Session")
            stopped = self.session_store.cancel_run(
                run["run_id"],
                owner_id=owner,
                reason=str(payload.get("reason") or "cancelled_by_user"),
            )
            return web.json_response({"ok": True, "run": stopped})
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_events(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            consumer_id = str(request.query.get("consumer_id") or "").strip()
            if consumer_id:
                delivery = self.session_store.poll_event_consumer(
                    session_id=request.match_info["session_id"],
                    owner_id=owner,
                    consumer_id=consumer_id,
                    limit=int(request.query.get("limit") or 500),
                )
                return web.json_response({"ok": True, **delivery})
            rows = self.session_store.events(
                request.match_info["session_id"],
                owner_id=owner,
                after_sequence=int(request.query.get("after_sequence") or 0),
                limit=int(request.query.get("limit") or 500),
            )
            snapshot = self.session_store.snapshot(
                request.match_info["session_id"], owner_id=owner
            )
            return web.json_response(
                {
                    "ok": True,
                    "events": rows,
                    "earliest_available_sequence": int(
                        snapshot["earliest_available_sequence"]
                    ),
                    "latest_sequence": int(snapshot["latest_sequence"]),
                }
            )
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_event_consumer_create(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            consumer = self.session_store.create_event_consumer(
                session_id=request.match_info["session_id"],
                owner_id=owner,
                consumer_id=str(payload.get("consumer_id") or "") or None,
            )
            return web.json_response({"ok": True, "consumer": consumer}, status=201)
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_event_consumer_ack(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            if "sequence" not in payload:
                raise ValueError("sequence is required")
            consumer = self.session_store.acknowledge_event_consumer(
                session_id=request.match_info["session_id"],
                owner_id=owner,
                consumer_id=request.match_info["consumer_id"],
                sequence=int(payload["sequence"]),
            )
            return web.json_response({"ok": True, "consumer": consumer})
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_session_fresh(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            self.session_store.get_session(
                request.match_info["session_id"], owner_id=owner
            )
            session = self.session_store.start_fresh_generation(
                request.match_info["session_id"], reason="session_api"
            )
            return web.json_response({"ok": True, "session": session})
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_attachment_stage(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            attachment = self.session_store.stage_attachment(
                session_id=request.match_info["session_id"],
                owner_id=owner,
                filename=str(payload.get("filename") or "attachment"),
                media_type=str(payload.get("media_type") or "application/octet-stream"),
                size_bytes=int(payload.get("size_bytes") or 0),
                sha256=str(payload.get("sha256") or ""),
            )
            return web.json_response({"ok": True, "attachment": attachment}, status=201)
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_attachment_commit(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            attachment = self.session_store.commit_attachment(
                session_id=request.match_info["session_id"],
                owner_id=owner,
                attachment_id=request.match_info["attachment_id"],
            )
            return web.json_response({"ok": True, "attachment": attachment})
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_approval_decide(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            payload = await request.json()
            approval = self.session_store.decide_approval(
                approval_id=request.match_info["approval_id"],
                owner_id=owner,
                decision=str(payload.get("decision") or ""),
            )
            return web.json_response({"ok": True, "approval": approval})
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_promotion_get(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            agent_id = str(request.match_info["agent_id"]).lower()
            if agent_id not in self._runtime_map():
                raise SessionNotFound("agent not found")
            return web.json_response(
                {
                    "ok": True,
                    "promotion": self.session_store.promotion_status(agent_id=agent_id),
                }
            )
        except Exception as exc:
            return self._v1_error(exc)

    async def handle_v1_promotion_post(self, request):
        owner = self._v1_owner_id(request)
        if owner is None:
            return self._v1_error(ValueError("not authenticated"), status=401)
        try:
            from orchestrator import runtime_session

            payload = await request.json()
            agent_id = str(request.match_info["agent_id"]).lower()
            runtime = self._runtime_map().get(agent_id)
            if runtime is None:
                raise SessionNotFound("agent not found")
            action = str(payload.get("action") or "status").strip().lower()
            if action in {"now", "all_now"}:
                session_ids = None
                if action == "now":
                    session_id = str(payload.get("session_id") or "").strip()
                    if not session_id:
                        raise ValueError("session_id is required for action=now")
                    self.session_store.get_session(session_id, owner_id=owner)
                    session_ids = [session_id]
                result = runtime_session.promote_sessions(
                    runtime, session_ids=session_ids, trigger="session_api"
                )
                return web.json_response({"ok": True, "result": result})
            if action == "configure":
                schedule = self.session_store.set_promotion_schedule(
                    agent_id=agent_id,
                    enabled=payload.get("enabled"),
                    local_time=payload.get("local_time"),
                    timezone_name=payload.get("timezone"),
                )
                return web.json_response({"ok": True, "schedule": schedule})
            raise ValueError("action must be now, all_now, or configure")
        except Exception as exc:
            return self._v1_error(exc)

    def _classify_upload(
        self, filename: str, declared_media_type: str = "", content_type: str = ""
    ) -> str:
        if declared_media_type:
            return declared_media_type.lower()

        mime = content_type or mimetypes.guess_type(filename)[0] or ""
        suffix = Path(filename).suffix.lower()
        if mime.startswith("image/"):
            if suffix == ".webp":
                return "sticker"
            return "photo"
        if mime.startswith("audio/"):
            if suffix == ".ogg":
                return "voice"
            return "audio"
        if mime.startswith("video/"):
            return "video"
        return "document"

    def _save_upload(
        self,
        runtime,
        *,
        filename: str,
        payload: bytes,
    ) -> tuple[Path, str]:
        filename = filename or f"upload_{uuid4().hex}"
        safe_name = f"{uuid4().hex}_{Path(filename).name}"
        local_path = runtime.media_dir / safe_name
        local_path.write_bytes(payload)
        return local_path, filename

    async def handle_chat(self, request):
        runtime_map = self._runtime_map()

        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            fields = {}
            uploads = []
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.filename:
                    chunks = bytearray()
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        chunks.extend(chunk)
                    uploads.append(
                        {
                            "filename": part.filename,
                            "content_type": part.headers.get("Content-Type", ""),
                            "payload": bytes(chunks),
                        }
                    )
                else:
                    fields[part.name] = await part.text()

            agent_name = fields.get("agent") or fields.get("agentId")
            runtime = runtime_map.get(agent_name)
            if runtime is None:
                return web.json_response(
                    {"ok": False, "error": "agent not found"}, status=404
                )

            text = fields.get("text", "").strip()
            caption = fields.get("caption", "").strip()
            emoji = fields.get("sticker_emoji", "").strip()
            declared_media_type = fields.get("media_type", "").strip()
            session_metadata = {
                "session_id": fields.get("session_id") or None,
                "owner_id": self._v1_owner_id(request),
                "session_surface": fields.get("surface") or "workbench",
                "session_channel_key": fields.get("client_id") or "default",
            }
            base_idempotency_key = (
                str(fields.get("idempotency_key") or "").strip() or None
            )

            request_ids = []
            if text and not uploads:
                slash_result = await try_execute_slash_command_text(
                    runtime,
                    text,
                    source_channel="api_chat",
                    session_metadata=session_metadata,
                )
                if slash_result is not None:
                    slash_result["agent"] = agent_name
                    slash_result["slash_command"] = True
                    status = 200 if slash_result.get("ok") else 400
                    return web.json_response(slash_result, status=status)
                request_id = await runtime.enqueue_api_text(
                    text,
                    request_metadata=session_metadata,
                    idempotency_key=base_idempotency_key,
                )
                request_ids.append(request_id)

            for upload_index, upload in enumerate(uploads):
                local_path, original_name = self._save_upload(
                    runtime,
                    filename=upload["filename"],
                    payload=upload["payload"],
                )
                media_kind = self._classify_upload(
                    original_name,
                    declared_media_type,
                    upload["content_type"],
                )
                request_id = await runtime.enqueue_api_media(
                    local_path=local_path,
                    media_kind=media_kind,
                    filename=original_name,
                    caption=caption or text,
                    emoji=emoji,
                    request_metadata=session_metadata,
                    idempotency_key=(
                        f"{base_idempotency_key}:{upload_index}"
                        if base_idempotency_key
                        else None
                    ),
                )
                request_ids.append(request_id)

            if not request_ids:
                return web.json_response(
                    {"ok": False, "error": "empty payload"}, status=400
                )

            return web.json_response(
                {"ok": True, "request_id": request_ids[0], "request_ids": request_ids}
            )

        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        text = (payload.get("text") or "").strip()
        runtime = runtime_map.get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        if not text:
            return web.json_response(
                {"ok": False, "error": "text is required"}, status=400
            )

        # Auto-learn reply route from hchat messages (updates contacts.json)
        reply_route = payload.get("reply_route")
        if reply_route and isinstance(reply_route, dict):
            self._learn_reply_route(text, reply_route)

        session_metadata = {
            "session_id": payload.get("session_id") or None,
            "owner_id": self._v1_owner_id(request),
            "session_surface": payload.get("surface") or "workbench",
            "session_channel_key": payload.get("client_id") or "default",
        }
        slash_result = await try_execute_slash_command_text(
            runtime,
            text,
            source_channel="api_chat",
            session_metadata=session_metadata,
        )
        if slash_result is not None:
            slash_result["agent"] = agent_name
            slash_result["slash_command"] = True
            status = 200 if slash_result.get("ok") else 400
            return web.json_response(slash_result, status=status)

        request_id = await runtime.enqueue_api_text(
            text,
            request_metadata=session_metadata,
            idempotency_key=str(payload.get("idempotency_key") or "").strip() or None,
        )
        response_payload = {"ok": True, "request_id": request_id}
        if request_id:
            try:
                run = self.session_store.get_run_by_request(request_id)
                response_payload.update(
                    {
                        "session_id": run["session_id"],
                        "run_id": run["run_id"],
                        "message_id": run["user_message_id"],
                    }
                )
            except SessionNotFound:
                pass
        return web.json_response(response_payload)

    async def handle_hchat_exchange(self, request):
        payload = await request.json()
        to_agent = (payload.get("to_agent") or "").strip().lower()
        to_instance = (payload.get("to_instance") or "").strip().upper()
        from_agent = (payload.get("from_agent") or "").strip().lower()
        from_instance = (payload.get("from_instance") or "").strip().upper()
        text = (payload.get("text") or "").strip()
        reply_route = payload.get("reply_route")

        if (
            not to_agent
            or not to_instance
            or not from_agent
            or not from_instance
            or not text
        ):
            return web.json_response(
                {"ok": False, "error": "missing required fields"}, status=400
            )

        local_instance = str(
            getattr(self.global_config, "instance_id", None) or "HASHI"
        ).upper()

        if to_instance == local_instance:
            gate_result = self._enterprise_channel_gate().check_ingress(
                "hchat",
                actor_id=from_agent,
                agent_id=to_agent,
                audit_context={
                    "from_agent": from_agent,
                    "from_instance": from_instance,
                    "to_agent": to_agent,
                    "to_instance": to_instance,
                    "exchange": True,
                },
            )
            if not gate_result.allowed:
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"hchat ingress denied: {gate_result.reason}",
                    },
                    status=403,
                )
            # Target is this instance: deliver directly to avoid blocking the event loop
            # with a synchronous HTTP self-call (which would deadlock for 10s and cause
            # the sender to fall back to Remote, resulting in duplicate delivery).
            runtime_map = self._runtime_map()
            runtime = runtime_map.get(to_agent)
            if runtime is None:
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"agent '{to_agent}' not found on {local_instance}",
                    },
                    status=404,
                )
            from tools.hchat_send import format_hchat_message

            message_text = format_hchat_message(from_agent, from_instance, text)
            if reply_route and isinstance(reply_route, dict):
                self._learn_reply_route(message_text, reply_route)
            await runtime.enqueue_api_text(message_text)
            return web.json_response({"ok": True, "relayed": True, "exchange": True})

        gate_result = self._enterprise_channel_gate().check_egress(
            "hchat",
            actor_id=from_agent,
            agent_id=from_agent,
            audit_context={
                "from_agent": from_agent,
                "from_instance": from_instance,
                "to_agent": to_agent,
                "to_instance": to_instance,
                "exchange": True,
            },
        )
        if not gate_result.allowed:
            return web.json_response(
                {"ok": False, "error": f"hchat egress denied: {gate_result.reason}"},
                status=403,
            )

        # Target is a different instance: relay via send_hchat
        try:
            from tools.hchat_send import send_hchat

            ok = send_hchat(
                to_agent,
                from_agent,
                text,
                target_instance=to_instance,
                source_instance=from_instance,
                reply_route_override=reply_route
                if isinstance(reply_route, dict)
                else None,
            )
            if ok:
                return web.json_response(
                    {"ok": True, "relayed": True, "exchange": True}
                )
            return web.json_response(
                {"ok": False, "error": "exchange delivery failed"}, status=502
            )
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_browser_chat_send(self, request):
        payload = await request.json()
        runtime_map = self._runtime_map()

        agent_name = (payload.get("agent") or payload.get("agentId") or "").strip()
        text = (payload.get("text") or "").strip()
        source = (payload.get("source") or "browser-api").strip()
        runtime = runtime_map.get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        if not text:
            return web.json_response(
                {"ok": False, "error": "text is required"}, status=400
            )

        loop = asyncio.get_running_loop()
        completion_future = loop.create_future()

        async def _listener(result: dict) -> None:
            if completion_future.done():
                return
            completion_future.set_result(result)

        request_id = await runtime.enqueue_api_text(
            text,
            source=source,
            deliver_to_telegram=False,
        )
        if request_id is None:
            return web.json_response(
                {"ok": False, "error": "failed to enqueue browser request"}, status=500
            )

        runtime.register_request_listener(request_id, _listener)

        result = await completion_future

        response_payload = {
            "ok": bool(result.get("success")),
            "request_id": request_id,
            "status": "completed" if result.get("success") else "failed",
            "text": result.get("text"),
            "error": result.get("error"),
            "source": result.get("source") or source,
            "summary": result.get("summary"),
        }
        return web.json_response(
            response_payload, status=200 if response_payload["ok"] else 502
        )

    async def handle_bridge_message(self, request):
        self._refresh_bridge_router()
        try:
            payload = await request.json()
            result = await self.bridge_router.send_message(payload)
            return web.json_response(result)
        except PermissionError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=403)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=409)

    async def handle_bridge_reply(self, request):
        self._refresh_bridge_router()
        try:
            payload = await request.json()
            result = await self.bridge_router.submit_reply(payload)
            return web.json_response(result)
        except PermissionError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=403)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    def _validate_transfer_payload(self, payload: dict) -> dict:
        required = [
            "transfer_id",
            "source_agent",
            "source_instance",
            "target_agent",
            "target_instance",
            "created_at",
            "recent_context_block",
            "last_user_message",
            "last_assistant_message",
        ]
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError(f"missing transfer fields: {', '.join(missing)}")
        mode = str(payload.get("mode") or "transfer").strip().lower()
        if mode not in {"transfer", "fork"}:
            raise ValueError("mode must be transfer or fork")
        return {
            "transfer_id": str(payload["transfer_id"]).strip(),
            "mode": mode,
            "source_agent": str(payload["source_agent"]).strip(),
            "source_instance": str(payload["source_instance"]).strip(),
            "target_agent": str(payload["target_agent"]).strip(),
            "target_instance": str(payload["target_instance"]).strip(),
            "created_at": str(payload["created_at"]).strip(),
            "exchange_count": int(payload.get("exchange_count") or 0),
            "word_count": int(payload.get("word_count") or 0),
            "recent_context_block": str(payload["recent_context_block"]).strip(),
            "recent_rounds": payload.get("recent_rounds")
            if isinstance(payload.get("recent_rounds"), list)
            else [],
            "last_user_message": str(payload["last_user_message"]).strip(),
            "last_assistant_message": str(payload["last_assistant_message"]).strip(),
            "source_runtime": payload.get("source_runtime")
            if isinstance(payload.get("source_runtime"), dict)
            else {},
            "source_workspace_dir": str(
                payload.get("source_workspace_dir") or ""
            ).strip(),
            "source_transcript_path": str(
                payload.get("source_transcript_path") or ""
            ).strip(),
            "transfer_guidance": payload.get("transfer_guidance")
            if isinstance(payload.get("transfer_guidance"), dict)
            else {},
            "task_state": payload.get("task_state")
            if isinstance(payload.get("task_state"), dict)
            else {},
            "handoff_summary": str(payload.get("handoff_summary") or "").strip(),
            "memory_files": payload.get("memory_files")
            if isinstance(payload.get("memory_files"), dict)
            else {},
        }

    def _accept_prefix_for_mode(self, mode: str) -> str:
        return (
            self.FORK_ACCEPT_PREFIX if mode == "fork" else self.TRANSFER_ACCEPT_PREFIX
        )

    def _build_transfer_prompt(self, package: dict) -> str:
        mode = str(package.get("mode") or "transfer").strip().lower()
        action_label = "fork" if mode == "fork" else "transfer"
        intro = (
            "SYSTEM: This is a bridge-managed fork.\n"
            "You are receiving a parallel context branch. The source session remains active.\n"
            if mode == "fork"
            else "SYSTEM: This is a bridge-managed transfer.\n"
        )
        guidance = package.get("transfer_guidance") or {}
        task_state = package.get("task_state") or {}
        memory_files = package.get("memory_files") or {}
        memory_sections = []
        for name in ("project.md", "decisions.md", "tasks.md"):
            text = str(memory_files.get(name) or "").strip()
            if text:
                memory_sections.append(f"[{name}]\n{text}")
        memory_block = "\n\n".join(memory_sections).strip()
        handoff_summary = str(package.get("handoff_summary") or "").strip()
        return (
            intro
            + f"You are NOT {package['source_agent']}. Do not imitate that agent's identity, persona, or relationship style.\n"
            "Keep your own identity, permissions, and system instructions.\n\n"
            f"Continue the {action_label}ed work using the operational context below.\n\n"
            "--- TRANSFER METADATA ---\n"
            f"Mode: {mode}\n"
            f"Transfer ID: {package['transfer_id']}\n"
            f"From: {package['source_agent']}@{package['source_instance']}\n"
            f"To: {package['target_agent']}@{package['target_instance']}\n"
            f"Created at: {package['created_at']}\n"
            f"Recent exchanges captured: {package['exchange_count']}\n"
            f"Recent words captured: {package['word_count']}\n\n"
            "--- CONTEXT WEIGHTING RULES ---\n"
            f"- {guidance.get('recent_turn_weighting') or 'Prefer the newest exchanges for current intent, task state, and next actions.'}\n"
            f"- {guidance.get('older_turn_weighting') or 'Treat older exchanges as background only.'}\n"
            f"- {guidance.get('conflict_rule') or 'If there is any conflict, prefer the newer context.'}\n\n"
            "--- STRUCTURED TASK STATE ---\n"
            f"Latest user request: {task_state.get('latest_user_request') or package['last_user_message']}\n"
            f"Latest source reply: {task_state.get('latest_source_reply') or package['last_assistant_message']}\n"
            f"Recent exchange count retained: {task_state.get('recent_exchange_count') or package['exchange_count']}\n"
            f"Memory files available: {', '.join(task_state.get('memory_files_available') or []) or 'none'}\n\n"
            "--- HANDOFF SUMMARY ---\n"
            f"{handoff_summary or 'No handoff summary was available.'}\n\n"
            "--- MEMORY FILE CONTEXT ---\n"
            f"{memory_block or 'No project/decision/task memory files were available.'}\n\n"
            "--- CONTINUITY CONTEXT ---\n"
            "Last user message:\n"
            f"{package['last_user_message']}\n\n"
            "Last assistant message from source:\n"
            f"{package['last_assistant_message']}\n\n"
            f"{package['recent_context_block']}\n\n"
            "--- REQUIRED FIRST RESPONSE ---\n"
            f"Start your first reply with: {self._accept_prefix_for_mode(mode)}{package['transfer_id']}\n"
            "Then, in your own voice:\n"
            "1. State what task is currently in progress.\n"
            "2. Continue directly from the next unfinished step.\n"
            "3. Do not ask the user to restate context unless a critical field is missing."
        )

    async def _notify_transfer_chat(
        self, runtime, text: str, *, purpose: str
    ) -> dict[str, Any]:
        if not getattr(runtime, "telegram_connected", False):
            runtime.logger.warning(
                "Transfer system notification not delivered because Telegram is disconnected.",
                extra={"purpose": purpose},
            )
            return {"delivered": False, "reason": "telegram_disconnected", "chunks": 0}
        try:
            _, chunk_count = await runtime.send_long_message(
                chat_id=runtime._primary_chat_id(),
                text=text,
                request_id=f"transfer-{uuid4().hex[:8]}",
                purpose=purpose,
            )
            return {
                "delivered": chunk_count > 0,
                "reason": "sent" if chunk_count > 0 else "telegram_send_skipped",
                "chunks": chunk_count,
            }
        except Exception:
            runtime.logger.warning(
                "Failed to deliver transfer system notification.", exc_info=True
            )
            return {"delivered": False, "reason": "send_exception", "chunks": 0}

    def _classify_transfer_ack(
        self, transfer_id: str, result: dict[str, Any], *, mode: str = "transfer"
    ) -> dict[str, Any]:
        if not result.get("success"):
            return {
                "ok": False,
                "error": result.get("error") or "transfer bootstrap failed",
            }
        raw_text = str(result.get("text") or "").strip()
        if not raw_text:
            return {
                "ok": False,
                "error": "target completed transfer bootstrap without a visible reply",
            }
        expected = f"{self._accept_prefix_for_mode(mode)}{transfer_id}"
        if raw_text.startswith(expected):
            return {"ok": True, "raw_text": raw_text, "ack_mode": "explicit"}
        return {"ok": True, "raw_text": raw_text, "ack_mode": "implicit"}

    def _finalize_transfer_status(
        self, *notifications: dict[str, Any]
    ) -> tuple[str, str]:
        if any(not note.get("delivered") for note in notifications):
            return "accepted_but_chat_offline", "offline"
        return "accepted", "online"

    async def handle_bridge_transfer(self, request):
        return await self._handle_bridge_handoff(request, mode="transfer")

    async def handle_bridge_fork(self, request):
        return await self._handle_bridge_handoff(request, mode="fork")

    async def _handle_bridge_handoff(self, request, *, mode: str):
        payload = await request.json()
        payload["mode"] = mode
        try:
            package = self._validate_transfer_payload(payload)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

        current_instance = str(
            getattr(self.global_config, "instance_id", None) or "HASHI"
        ).upper()
        if str(package["target_instance"]).upper() != str(current_instance).upper():
            return web.json_response(
                {
                    "ok": False,
                    "error": f"target instance mismatch: this endpoint is {current_instance}",
                },
                status=409,
            )
        runtime = self._runtime_map().get(package["target_agent"])
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "target agent not found"}, status=404
            )
        if (
            package["source_agent"] == package["target_agent"]
            and package["source_instance"] == package["target_instance"]
        ):
            return web.json_response(
                {"ok": False, "error": f"cannot {mode} to the same agent"}, status=400
            )
        if not getattr(runtime, "startup_success", False):
            return web.json_response(
                {"ok": False, "error": "target runtime is offline"}, status=409
            )
        if (
            mode == "transfer"
            and getattr(runtime, "has_active_transfer", None)
            and runtime.has_active_transfer()
        ):
            return web.json_response(
                {"ok": False, "error": "target already has an active transfer"},
                status=409,
            )

        transfer_id = package["transfer_id"]
        self.transfer_store.create_transfer(package, status="received")
        self.transfer_store.append_event(
            transfer_id, "received", {"target_agent": package["target_agent"]}
        )
        incoming_notice = await self._notify_transfer_chat(
            runtime,
            (
                f"Incoming {'fork' if mode == 'fork' else 'transfer'} received from "
                f"{package['source_agent']}@{package['source_instance']}.\n"
                f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}"
            ),
            purpose=f"{mode}-incoming",
        )
        self.transfer_store.append_event(
            transfer_id, "incoming_notice", incoming_notice
        )
        bridge_prompt = self._build_transfer_prompt(package)
        request_id = await runtime.enqueue_api_text(
            bridge_prompt,
            source=f"bridge-{mode}:{transfer_id}",
            deliver_to_telegram=True,
        )
        if request_id is None:
            self.transfer_store.update_transfer(
                transfer_id,
                status="failed",
                error_code="enqueue_failed",
                error_text=f"failed to enqueue {mode} request on target",
            )
            self.transfer_store.append_event(
                transfer_id, "failed", {"reason": "enqueue_failed"}
            )
            await self._notify_transfer_chat(
                runtime,
                (
                    f"{'Fork' if mode == 'fork' else 'Transfer'} failed before enqueue.\n"
                    f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}"
                ),
                purpose=f"{mode}-failed",
            )
            return web.json_response(
                {"ok": False, "error": f"failed to enqueue {mode} request on target"},
                status=409,
            )

        self.transfer_store.update_transfer(
            transfer_id, status="queued_on_target", request_id=request_id
        )
        self.transfer_store.append_event(
            transfer_id, "queued_on_target", {"request_id": request_id}
        )
        loop = asyncio.get_running_loop()
        ack_future = loop.create_future()

        async def _listener(result: dict) -> None:
            if ack_future.done():
                return
            ack_future.set_result(
                self._classify_transfer_ack(transfer_id, result, mode=mode)
            )

        runtime.register_request_listener(request_id, _listener)
        ack_result = await ack_future

        if not ack_result.get("ok"):
            error_text = str(ack_result.get("error") or f"{mode} failed")
            self.transfer_store.update_transfer(
                transfer_id,
                status="failed",
                error_code="target_ack_failed",
                error_text=error_text,
            )
            self.transfer_store.append_event(
                transfer_id, "failed", {"reason": error_text}
            )
            await self._notify_transfer_chat(
                runtime,
                (
                    f"{'Fork' if mode == 'fork' else 'Transfer'} failed.\n"
                    f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}\n"
                    f"Reason: {error_text}"
                ),
                purpose=f"{mode}-failed",
            )
            return web.json_response(
                {"ok": False, "error": error_text, "transfer_id": transfer_id},
                status=409,
            )

        ack_text = str(ack_result.get("raw_text") or "")
        ack_mode = str(ack_result.get("ack_mode") or "explicit")
        accepted_notice = await self._notify_transfer_chat(
            runtime,
            (
                f"{'Fork' if mode == 'fork' else 'Transfer'} accepted from "
                f"{package['source_agent']}@{package['source_instance']}.\n"
                f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}"
            ),
            purpose=f"{mode}-accepted",
        )
        self.transfer_store.append_event(
            transfer_id, "accepted_notice", accepted_notice
        )
        final_status, target_chat_status = self._finalize_transfer_status(
            incoming_notice, accepted_notice
        )
        self.transfer_store.update_transfer(
            transfer_id, status=final_status, ack_text=ack_text
        )
        self.transfer_store.append_event(
            transfer_id,
            "accepted",
            {
                "request_id": request_id,
                "ack_mode": ack_mode,
                "target_chat_status": target_chat_status,
            },
        )
        return web.json_response(
            {
                "ok": True,
                "transfer_id": transfer_id,
                "request_id": request_id,
                "status": final_status,
                "ack_mode": ack_mode,
                "target_chat_status": target_chat_status,
            }
        )

    async def handle_bridge_cos(self, request):
        """Handle cross-instance Chief of Staff query. Routes to Lily for precedent-based decision support."""
        payload = await request.json()
        question = str(payload.get("question") or "").strip()
        from_agent = str(payload.get("from_agent") or "").strip()
        if not question or not from_agent:
            return web.json_response(
                {"ok": False, "error": "question and from_agent required"}, status=400
            )

        lily_runtime = self._runtime_map().get("lily")
        if lily_runtime is None or not getattr(lily_runtime, "startup_success", False):
            return web.json_response(
                {"ok": False, "error": "lily is offline", "reason": "lily_offline"},
                status=503,
            )

        cos_result = await lily_runtime.cos_query(question)
        return web.json_response(
            {"ok": cos_result.get("answered", False), **cos_result}
        )

    async def handle_bridge_transfer_get(self, request):
        transfer_id = request.match_info["transfer_id"]
        record = self.transfer_store.get_transfer(transfer_id)
        if record is None:
            return web.json_response(
                {"ok": False, "error": "transfer not found"}, status=404
            )
        return web.json_response({"ok": True, "transfer": record})

    async def handle_bridge_spawn(self, request):
        return web.json_response(
            {
                "ok": False,
                "error": "spawn is reserved for a later phase and is not implemented in Phase 1",
            },
            status=501,
        )

    async def handle_bridge_thread(self, request):
        self._refresh_bridge_router()
        thread_id = request.match_info["thread_id"]
        thread = self.bridge_router.get_thread(thread_id)
        if thread is None:
            return web.json_response(
                {"ok": False, "error": "thread not found"}, status=404
            )
        return web.json_response({"ok": True, "thread": thread})

    async def handle_bridge_message_get(self, request):
        self._refresh_bridge_router()
        message_id = request.match_info["message_id"]
        message = self.bridge_router.get_message(message_id)
        if message is None:
            return web.json_response(
                {"ok": False, "error": "message not found"}, status=404
            )
        return web.json_response({"ok": True, "message": message})

    async def handle_bridge_capabilities(self, request):
        self._refresh_bridge_router()
        agent_name = request.match_info["agent"]
        capability = self.bridge_router.get_capability(agent_name)
        if capability is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        return web.json_response({"ok": True, "capability": capability})

    async def handle_admin_commands(self, request):
        if not self._check_admin_auth(request):
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )

        name = request.match_info["name"]
        runtime = self._runtime_map().get(name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        return web.json_response(
            {"ok": True, "agent": name, "commands": supported_commands(runtime)}
        )

    async def handle_admin_command(self, request):
        if not self._check_admin_auth(request):
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )

        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        command = (payload.get("command") or "").strip()
        chat_id = payload.get("chat_id")

        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        if not command:
            return web.json_response(
                {"ok": False, "error": "command is required"}, status=400
            )

        result = await execute_local_command(runtime, command, chat_id=chat_id)
        status = 200 if result.get("ok") else 400
        result["agent"] = agent_name
        return web.json_response(result, status=status)

    async def handle_agent_command(self, request):
        """Handle /api/agents/{name}/command - simpler endpoint for frontend."""
        agent_name = request.match_info.get("name")
        payload = await request.json()
        command = (payload.get("command") or "").strip()

        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        if not command:
            return web.json_response(
                {"ok": False, "error": "command is required"}, status=400
            )

        result = await execute_local_command(runtime, command)
        status_code = 200 if result.get("ok") else 400
        result["agent"] = agent_name
        return web.json_response(result, status=status_code)

    def _task_scheduler(self):
        kernel = getattr(self.orchestrator, "kernel", None)
        return getattr(self.orchestrator, "scheduler", None) or getattr(
            kernel, "scheduler", None
        )

    @staticmethod
    def _scheduler_job_kind_from_summary(summary: str) -> tuple[str, str]:
        value = str(summary or "").strip()
        for label, kind in (
            ("Cron Task [", "cron"),
            ("Cron Recovery [", "cron"),
            ("Heartbeat Task [", "heartbeat"),
            ("Heartbeat Recovery [", "heartbeat"),
            ("Nudge Task [", "nudge"),
        ):
            if value.startswith(label) and value.endswith("]"):
                return kind, value[len(label) : -1]
        return "", ""

    @staticmethod
    def _scheduler_last_run_at(value) -> str | None:
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value)).astimezone().isoformat()
            except (OSError, OverflowError, ValueError):
                return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _scheduler_job_record(self, *, agent_name: str, kind: str, job: dict) -> dict:
        from orchestrator.her_v2.request_policy import job_effort_policy

        task_id = str(job.get("id") or "")
        scheduler = self._task_scheduler()
        state = getattr(scheduler, "state", {}) if scheduler is not None else {}
        state_key = {"cron": "crons", "heartbeat": "heartbeats", "nudge": "nudges"}[
            kind
        ]
        last_run = (
            (state.get(state_key) or {}).get(task_id)
            if isinstance(state, dict)
            else None
        )
        pending_recovery = []
        recovery_batches = (
            (state.get("recovery_batches") or {}) if isinstance(state, dict) else {}
        )
        for batch in recovery_batches.values():
            if not isinstance(batch, dict) or batch.get("agent") != agent_name:
                continue
            if batch.get("status") not in {"pending", "running"}:
                continue
            for item in batch.get("items") or []:
                if (
                    isinstance(item, dict)
                    and str(item.get("task_id") or "") == task_id
                    and str(item.get("kind") or "") == kind
                ):
                    pending_recovery.append(
                        {
                            "batch_id": batch.get("batch_id"),
                            "status": batch.get("status"),
                            "missed_count": item.get("missed_count", 1),
                            "replayable_count": min(
                                max(1, int(item.get("missed_count", 1) or 1)),
                                max(1, int(item.get("replay_limit", 1) or 1)),
                                len(item.get("due_at") or []) or 1,
                            ),
                        }
                    )
        effort_policy = None
        if kind in {"cron", "heartbeat"}:
            try:
                effort_policy = job_effort_policy(job)
            except ValueError as exc:
                effort_policy = {
                    "effective": None,
                    "source": "invalid",
                    "applies_to": "her-v2",
                    "error": str(exc),
                }
        return {
            "kind": kind,
            "job_id": task_id,
            "enabled": bool(job.get("enabled", False)),
            "job": dict(job),
            "last_run": last_run,
            "last_run_at": self._scheduler_last_run_at(last_run),
            "pending_recovery": pending_recovery,
            "her_v2_effort_policy": effort_policy,
        }

    async def handle_agent_scheduler_jobs(self, request):
        """List this agent's authoritative HASHI Scheduler jobs."""
        agent_name = request.match_info.get("name")
        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        skill_manager = getattr(runtime, "skill_manager", None)
        if skill_manager is None:
            return web.json_response(
                {"ok": False, "error": "skill manager unavailable"}, status=503
            )

        kind = str(request.query.get("kind") or "all").strip().lower()
        if kind not in {"all", "cron", "heartbeat", "nudge"}:
            return web.json_response(
                {"ok": False, "error": "kind must be all, cron, heartbeat, or nudge"},
                status=400,
            )
        enabled_filter = request.query.get("enabled")
        if enabled_filter is not None and enabled_filter not in {"true", "false"}:
            return web.json_response(
                {"ok": False, "error": "enabled must be true or false"}, status=400
            )
        enabled = None if enabled_filter is None else enabled_filter == "true"

        kinds = ("cron", "heartbeat", "nudge") if kind == "all" else (kind,)
        records = []
        for job_kind in kinds:
            for job in skill_manager.list_jobs(job_kind, agent_name=agent_name):
                if enabled is not None and bool(job.get("enabled", False)) != enabled:
                    continue
                records.append(
                    self._scheduler_job_record(
                        agent_name=agent_name,
                        kind=job_kind,
                        job=job,
                    )
                )
        return web.json_response(
            {
                "ok": True,
                "authority": "HASHI Scheduler",
                "agent": agent_name,
                "jobs": records,
                "count": len(records),
            }
        )

    async def handle_agent_scheduler_status(self, request):
        """Return one authoritative job definition and persisted scheduler state."""
        agent_name = request.match_info.get("name")
        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        kind = str(request.query.get("kind") or "").strip().lower()
        job_id = str(request.query.get("job_id") or "").strip()
        if kind not in {"cron", "heartbeat", "nudge"}:
            return web.json_response(
                {"ok": False, "error": "kind must be cron, heartbeat, or nudge"},
                status=400,
            )
        if not job_id:
            return web.json_response(
                {"ok": False, "error": "job_id is required"}, status=400
            )
        skill_manager = getattr(runtime, "skill_manager", None)
        if skill_manager is None:
            return web.json_response(
                {"ok": False, "error": "skill manager unavailable"}, status=503
            )
        job = skill_manager.get_job(kind, job_id)
        if not job or job.get("agent") != agent_name:
            return web.json_response(
                {"ok": False, "error": "job not found for agent"}, status=404
            )
        return web.json_response(
            {
                "ok": True,
                "authority": "HASHI Scheduler",
                "agent": agent_name,
                "status": self._scheduler_job_record(
                    agent_name=agent_name,
                    kind=kind,
                    job=job,
                ),
            }
        )

    async def handle_agent_scheduler_runs(self, request):
        """Return recent scheduler execution receipts for this agent."""
        from orchestrator import runtime_cross_session

        agent_name = request.match_info.get("name")
        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        kind = str(request.query.get("kind") or "all").strip().lower()
        job_id = str(request.query.get("job_id") or "").strip()
        if kind not in {"all", "cron", "heartbeat", "nudge"}:
            return web.json_response(
                {"ok": False, "error": "kind must be all, cron, heartbeat, or nudge"},
                status=400,
            )
        try:
            limit = max(1, min(50, int(request.query.get("limit") or 10)))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "limit must be an integer"}, status=400
            )

        runs = []
        for receipt in reversed(runtime_cross_session.load_receipts(runtime)):
            if not str(receipt.get("source") or "").startswith("scheduler"):
                continue
            receipt_kind, receipt_job_id = self._scheduler_job_kind_from_summary(
                str(receipt.get("summary") or "")
            )
            if not receipt_kind:
                continue
            if kind != "all" and receipt_kind != kind:
                continue
            if job_id and receipt_job_id != job_id:
                continue
            runs.append(
                {
                    "kind": receipt_kind,
                    "job_id": receipt_job_id,
                    "request_id": receipt.get("request_id"),
                    "source": receipt.get("source"),
                    "status": receipt.get("status"),
                    "completion_status": receipt.get("completion_status"),
                    "stop_reason": receipt.get("stop_reason"),
                    "delivered": bool(receipt.get("delivered")),
                    "created_at": receipt.get("created_at"),
                    "updated_at": receipt.get("updated_at"),
                    "result": receipt.get("assistant_text"),
                    "error": receipt.get("error"),
                }
            )
            if len(runs) >= limit:
                break
        return web.json_response(
            {
                "ok": True,
                "authority": "HASHI Scheduler",
                "agent": agent_name,
                "runs": runs,
                "count": len(runs),
            }
        )

    async def handle_agent_run_job(self, request):
        """Run one cron/heartbeat job immediately for a specific agent."""
        agent_name = request.match_info.get("name")
        payload = await request.json()
        kind = str(payload.get("kind") or "cron").strip().lower()
        job_id = str(payload.get("job_id") or payload.get("id") or "").strip()

        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        if kind not in {"cron", "heartbeat"}:
            return web.json_response(
                {"ok": False, "error": "kind must be cron or heartbeat"}, status=400
            )
        if not job_id:
            return web.json_response(
                {"ok": False, "error": "job_id is required"}, status=400
            )
        if (
            payload.get("requested_by") == "hashi_tool_gateway"
            and payload.get("authorization") != "explicit_user_authorization"
        ):
            return web.json_response(
                {"ok": False, "error": "explicit single-job authorization is required"},
                status=403,
            )

        skill_manager = getattr(runtime, "skill_manager", None)
        if skill_manager is None:
            return web.json_response(
                {"ok": False, "error": "skill manager unavailable"}, status=503
            )
        job = skill_manager.get_job(kind, job_id)
        if not job or job.get("agent") != agent_name:
            return web.json_response(
                {"ok": False, "error": "job not found for agent"}, status=404
            )

        try:
            ok, message = await runtime._run_job_now(job, kind=kind)
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500
            )
        status_code = 200 if ok else 409
        return web.json_response(
            {
                "ok": ok,
                "agent": agent_name,
                "kind": kind,
                "job_id": job_id,
                "message": message,
            },
            status=status_code,
        )

    def _background_job_manager(self):
        kernel = getattr(self.orchestrator, "kernel", None)
        return getattr(kernel, "background_job_manager", None) or getattr(
            self.orchestrator, "background_job_manager", None
        )

    def _background_job_not_running_response(self):
        return web.json_response(
            {"ok": False, "error": "BackgroundJobManager is not running"}, status=503
        )

    def _runtime_for_background_job_agent(self, manager, agent: str):
        runtime = self._runtime_map().get(agent)
        if runtime is not None:
            return runtime
        kernel = getattr(manager, "kernel", None)
        for candidate in getattr(kernel, "runtimes", []) if kernel is not None else []:
            if getattr(candidate, "name", None) == agent:
                return candidate
        return None

    async def handle_background_jobs_start(self, request):
        manager = self._background_job_manager()
        if manager is None:
            return self._background_job_not_running_response()
        payload = await request.json()
        argv = payload.get("argv")
        raw_command = payload.get("command")
        if argv is None and isinstance(raw_command, list):
            argv = raw_command
            command = None
        else:
            command = str(raw_command or "").strip() or None
        if argv is not None:
            if not isinstance(argv, list) or not all(
                isinstance(item, str) and item for item in argv
            ):
                return web.json_response(
                    {"ok": False, "error": "argv must be a non-empty string array"},
                    status=400,
                )
            if not argv:
                return web.json_response(
                    {"ok": False, "error": "argv must not be empty"}, status=400
                )
        if not argv and not command:
            return web.json_response(
                {"ok": False, "error": "argv or command is required"}, status=400
            )
        if argv and command:
            return web.json_response(
                {"ok": False, "error": "provide argv or command, not both"}, status=400
            )

        cwd = str(
            payload.get("cwd")
            or getattr(self.global_config, "project_root", "")
            or self.config_path.parent
        ).strip()
        agent = str(payload.get("agent") or "unknown").strip() or "unknown"
        origin = (
            payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
        )
        origin.setdefault("source", "workbench_api:background_jobs")
        origin.setdefault("api_path", "/api/background-jobs")
        runtime = self._runtime_for_background_job_agent(manager, agent)
        meta = getattr(runtime, "current_request_meta", None) or {}
        if origin.get("chat_id") is None and meta.get("chat_id") is not None:
            origin["chat_id"] = meta.get("chat_id")
        if origin.get("chat_id") is None and runtime is not None:
            primary_chat_id = getattr(runtime, "_primary_chat_id", None)
            if callable(primary_chat_id):
                try:
                    origin["chat_id"] = primary_chat_id()
                except Exception:
                    pass
        if not origin.get("request_id") and meta.get("request_id"):
            origin["request_id"] = meta.get("request_id")
        if not origin.get("summary") and meta.get("summary"):
            origin["summary"] = meta.get("summary")
        try:
            record = await manager.start_job(
                agent=agent,
                cwd=cwd,
                argv=argv,
                command=command,
                origin=origin,
                notify_on_complete=bool(payload.get("notify_on_complete", True)),
                notify_on_failure=bool(payload.get("notify_on_failure", True)),
                trigger_agent_on_complete=bool(
                    payload.get("trigger_agent_on_complete", True)
                ),
                trigger_agent_on_failure=bool(
                    payload.get("trigger_agent_on_failure", True)
                ),
            )
        except FileNotFoundError as exc:
            return web.json_response(
                {"ok": False, "error": f"cwd not found: {exc}"}, status=400
            )
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500
            )
        return web.json_response({"ok": True, "job": record.to_dict()}, status=201)

    async def handle_background_jobs_list(self, request):
        manager = self._background_job_manager()
        if manager is None:
            return self._background_job_not_running_response()
        query = getattr(request, "query", {}) or {}
        agent = str(query.get("agent") or "").strip() or None
        state = str(query.get("state") or "").strip()
        states = {item.strip() for item in state.split(",") if item.strip()} or None
        try:
            limit = max(1, min(int(query.get("limit") or 50), 200))
        except Exception:
            limit = 50
        jobs = [
            record.to_dict()
            for record in manager.list(agent=agent, states=states, limit=limit)
        ]
        return web.json_response({"ok": True, "jobs": jobs})

    async def handle_background_jobs_get(self, request):
        manager = self._background_job_manager()
        if manager is None:
            return self._background_job_not_running_response()
        job_id = str(request.match_info.get("job_id") or "").strip()
        record = manager.get(job_id)
        if record is None:
            return web.json_response(
                {"ok": False, "error": "job not found"}, status=404
            )
        return web.json_response({"ok": True, "job": record.to_dict()})

    async def handle_background_jobs_tail(self, request):
        manager = self._background_job_manager()
        if manager is None:
            return self._background_job_not_running_response()
        query = getattr(request, "query", {}) or {}
        job_id = str(request.match_info.get("job_id") or "").strip()
        stream = str(query.get("stream") or "stdout").strip().lower()
        if stream not in {"stdout", "stderr"}:
            return web.json_response(
                {"ok": False, "error": "stream must be stdout or stderr"}, status=400
            )
        try:
            lines = max(1, min(int(query.get("lines") or 80), 1000))
            text = manager.tail(job_id, stream=stream, lines=lines)
        except KeyError:
            return web.json_response(
                {"ok": False, "error": "job not found"}, status=404
            )
        return web.json_response(
            {"ok": True, "job_id": job_id, "stream": stream, "tail": text}
        )

    async def handle_background_jobs_cancel(self, request):
        manager = self._background_job_manager()
        if manager is None:
            return self._background_job_not_running_response()
        job_id = str(request.match_info.get("job_id") or "").strip()
        try:
            record = await manager.cancel(job_id)
        except KeyError:
            return web.json_response(
                {"ok": False, "error": "job not found"}, status=404
            )
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500
            )
        return web.json_response({"ok": True, "job": record.to_dict()})

    async def handle_admin_smoke(self, request):
        if not self._check_admin_auth(request):
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )

        payload = await request.json()
        runtime_map = self._runtime_map()

        requested_agent = payload.get("agent")
        requested_agents = payload.get("agents")
        if requested_agents:
            target_names = [name for name in requested_agents if name in runtime_map]
        elif requested_agent:
            target_names = [requested_agent] if requested_agent in runtime_map else []
        else:
            target_names = [rt.name for rt in self._runtime_list()]

        if not target_names:
            return web.json_response(
                {"ok": False, "error": "no matching agents"}, status=404
            )

        include_commands = bool(payload.get("include_commands", True))
        include_chat = bool(payload.get("include_chat", True))
        chat_text = (
            payload.get("chat_text") or "Smoke test ping. Reply with one short line."
        ).strip()
        timeout_s = float(payload.get("timeout_s", 45))
        timeout_s = max(5.0, min(timeout_s, 180.0))

        command_plan = payload.get("commands")
        results = []

        for name in target_names:
            runtime = runtime_map[name]
            agent_result = {"agent": name, "commands": [], "chat": None}

            if include_commands:
                commands = (
                    command_plan
                    if isinstance(command_plan, list) and command_plan
                    else self._default_smoke_commands(runtime)
                )
                for command in commands:
                    cmd_result = await execute_local_command(runtime, str(command))
                    agent_result["commands"].append(cmd_result)

            if include_chat:
                agent_row = next(
                    (row for row in self._load_agent_rows() if row["name"] == name),
                    None,
                )
                transcript_path = (
                    self._resolve_transcript_path(agent_row, runtime)
                    if agent_row
                    else Path(runtime.get_runtime_metadata()["transcript_path"])
                )
                start_offset = (
                    transcript_path.stat().st_size if transcript_path.exists() else 0
                )
                request_id = await runtime.enqueue_api_text(
                    chat_text, source="api-smoke"
                )
                wait_result = await self._wait_for_assistant_reply(
                    transcript_path,
                    start_offset,
                    timeout_s,
                    expected_source="api-smoke",
                    expected_prompt=chat_text,
                )
                wait_result["request_id"] = request_id
                wait_result["prompt"] = chat_text
                agent_result["chat"] = wait_result

            results.append(agent_result)

        all_ok = True
        for result in results:
            for cmd in result["commands"]:
                if not cmd.get("ok"):
                    all_ok = False
            if include_chat and result["chat"] and not result["chat"].get("received"):
                all_ok = False

        return web.json_response({"ok": all_ok, "results": results})

    async def handle_health(self, request):
        running_agents = [
            runtime.name for runtime in self._runtime_list() if runtime.startup_success
        ]
        orchestrator = self.orchestrator
        payload = {
            "ok": True,
            "instance_id": getattr(self.global_config, "instance_id", None)
            or getattr(orchestrator, "instance_id", None)
            or "HASHI",
            "workbench_port": getattr(self.global_config, "workbench_port", None),
            "api_gateway_port": getattr(self.global_config, "api_gateway_port", None),
            "api_gateway_enabled": bool(getattr(orchestrator, "api_gateway", None)),
            "api_gateway_default_model": getattr(
                getattr(orchestrator, "api_gateway", None), "default_model", None
            ),
            "agents": running_agents,
        }
        if self._is_governed_profile():
            payload["enterprise"] = self._enterprise_health_payload()
        return web.json_response(payload)

    def _enterprise_health_payload(self) -> dict:
        policy_evaluator = self._enterprise_policy_evaluator()
        services = {
            "identity": self.identity_service is not None,
            "channel_registry": self.channel_registry is not None,
            "audit_ledger": self.audit_ledger is not None,
            "policy_evaluator": policy_evaluator is not None,
        }
        return {
            "profile": str(getattr(self.global_config, "deployment_profile", "") or ""),
            "organization_id": str(
                getattr(self.global_config, "organization_id", "") or ""
            ).strip()
            or None,
            "services": services,
            "ok": all(services.values()),
        }

    async def handle_jobs_import(self, request):
        """Import a job from a remote instance (cross-instance job transfer).

        Payload: {"kind": "cron"|"heartbeat", "job": {...}, "from_instance": "<INSTANCE>", "from_agent": "akane"}
        The job is imported as disabled so the recipient can review before enabling.
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        kind = payload.get("kind", "")
        job = payload.get("job")
        if kind not in ("cron", "heartbeat") or not isinstance(job, dict):
            return web.json_response(
                {"ok": False, "error": "kind and job are required"}, status=400
            )

        # Find skill_manager from any running runtime
        skill_manager = None
        for runtime in self._runtime_list():
            sm = getattr(runtime, "skill_manager", None)
            if sm is not None:
                skill_manager = sm
                break
        if skill_manager is None:
            return web.json_response(
                {"ok": False, "error": "skill_manager unavailable"}, status=503
            )

        job["enabled"] = False  # always import as disabled
        ok, message = skill_manager.import_job(kind, job)
        return web.json_response(
            {"ok": ok, "message": message, "job_id": job.get("id")}
        )

    async def handle_admin_start_agent(self, request):
        if not self._check_admin_auth(request):
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )
        if self.orchestrator is None:
            return web.json_response(
                {"ok": False, "error": "orchestrator unavailable"}, status=503
            )
        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        if not agent_name:
            return web.json_response(
                {"ok": False, "error": "agent is required"}, status=400
            )
        ok, message = await self.orchestrator.start_agent(str(agent_name))
        status = 200 if ok else 400
        return web.json_response(
            {"ok": ok, "agent": agent_name, "message": message}, status=status
        )

    async def handle_admin_stop_agent(self, request):
        if not self._check_admin_auth(request):
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )
        if self.orchestrator is None:
            return web.json_response(
                {"ok": False, "error": "orchestrator unavailable"}, status=503
            )
        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        if not agent_name:
            return web.json_response(
                {"ok": False, "error": "agent is required"}, status=400
            )
        ok, message = await self.orchestrator.stop_agent(str(agent_name))
        status = 200 if ok else 400
        return web.json_response(
            {"ok": ok, "agent": agent_name, "message": message}, status=status
        )

    async def handle_admin_shutdown(self, request):
        if not self._check_admin_auth(request):
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )
        if self.orchestrator is None:
            return web.json_response(
                {"ok": False, "error": "orchestrator unavailable"}, status=503
            )
        payload = await request.json() if request.can_read_body else {}
        reason = str((payload or {}).get("reason") or "admin-api")
        self.orchestrator.request_shutdown(reason=reason)
        return web.json_response(
            {"ok": True, "message": f"Shutdown requested ({reason})."}
        )

    async def handle_admin_notify(self, request):
        if not self._check_admin_auth(request):
            return web.json_response(
                {"ok": False, "error": "admin auth failed"}, status=403
            )
        payload = await request.json() if request.can_read_body else {}
        agent_name = str(payload.get("agent") or payload.get("agentId") or "").strip()
        text = str(payload.get("text") or payload.get("message") or "").strip()
        if not agent_name:
            return web.json_response(
                {"ok": False, "error": "agent is required"}, status=400
            )
        if not text:
            return web.json_response(
                {"ok": False, "error": "text is required"}, status=400
            )
        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response(
                {"ok": False, "error": "agent not found"}, status=404
            )
        chat_id = payload.get("chat_id")
        if chat_id is None:
            primary_chat_id = getattr(runtime, "_primary_chat_id", None)
            if callable(primary_chat_id):
                chat_id = primary_chat_id()
        if chat_id is None:
            return web.json_response(
                {"ok": False, "error": "chat_id is required"}, status=400
            )
        await runtime._send_text(int(chat_id), text)
        return web.json_response(
            {"ok": True, "agent": agent_name, "chat_id": int(chat_id)}
        )
