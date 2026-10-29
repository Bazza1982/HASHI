"""Operator-owned service installation reader and explicit foreground entrypoint.

No default port, identity, root, key, account creation or service start is
inferred. The public instance descriptor contains endpoints and a pinned key;
this private document and all signing material remain outside agent access.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import signal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from orchestrator.config import ConfigManager
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import read_config_json
from orchestrator.enterprise import IdentityService
from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied
from orchestrator.permissions.clients import settings_revision
from orchestrator.permissions.configured_models import ConfiguredModelFactory
from orchestrator.permissions.ipc_server import PermissionEndpoint
from orchestrator.permissions.models import PermissionLevel, normalize_scope, scope_covers
from orchestrator.permissions.native_factory import InstalledExecutorFactory, InstalledFileHelper
from orchestrator.permissions.receipt_issuer import SupervisorReceiptIssuer
from orchestrator.permissions.result_store import EncryptedResultStore
from orchestrator.permissions.resource_policy import ProtectedResourcePolicy
from orchestrator.permissions.run_service import ManagedRunPolicy
from orchestrator.permissions.service import PermissionServiceApplication
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier
from orchestrator.permissions.windows_ipc import WindowsPipeListener, PipePeerPolicy, current_process_user_sid
from orchestrator.permissions.windows_security import InstallationLock, require_private_path, require_nonadmin_identity
from orchestrator.session_store import SessionStore
from orchestrator import workzone


def _path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("an absolute operator installation path is required")
    return Path(value)


def _scopes(values, cwd):
    if not isinstance(values, list):
        raise ValueError("an explicit installed scope list is required")
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"kind", "resource", "actions"}:
            raise ValueError("invalid installed scope")
        result.append(normalize_scope(value["kind"], value["resource"], value["actions"], cwd=cwd))
    return tuple(result)


class InstancePolicies:
    def __init__(self, manager, installation, *, private_path):
        self.manager, self.installation = manager, installation
        self._private_path = private_path
        self.config = ConfigAdmin(type("ConfigPaths", (), {"config_path": manager.config_path})())
        self._frozen, self._current = {}, {}
        self.authority = None

    def protected_policy(self, global_config):
        helper = self.installation["helper"]
        private = [self._private_path, _path(helper["private_root"])]
        private.extend(_path(self.installation[field]) for field in (
            "identity_path", "approval_key", "transport_key", "supervisor_key", "result_key",
        ))
        private.extend(map(_path, self.installation["protected_roots"]))
        return ProtectedResourcePolicy(
            global_config, private_paths=private,
            immutable_paths=(
                _path(self.installation["config_path"]), _path(helper["executable"]).parent,
                _path(helper["entry"]).parent, Path(__file__).resolve().parents[2],
            ),
        )

    def preview(self, agent_id, owner_id):
        global_config, agents, _ = self.manager.load()
        declared = self.installation["agents"].get(agent_id)
        matches = [agent for agent in agents if agent.name == agent_id]
        if len(matches) != 1 or not isinstance(declared, dict) or declared.get("owner_id") != owner_id:
            raise PermissionDenied("no installed policy exists for this Agent owner")
        agent = matches[0]
        cwd = agent.workspace_dir
        baseline = _scopes(declared["baseline_scopes"], cwd)
        return {
            "baseline_scopes": [asdict(scope) for scope in baseline],
            "requestable_scopes": [asdict(scope) for scope in _scopes(declared["requestable_scopes"], cwd)],
            "installation_revision": self.installation.revision,
            "protected_roots": [str(path) for path in self.protected_policy(global_config).policy.protected_write_paths],
            "native_tools": ["file_read", "file_write"],
        }

    def settings(self, agent_id):
        return self.config.read_agent_permission_settings(agent_id).settings

    def __call__(self, agent_id, owner_id, session_id):
        del session_id
        global_config, agents, _ = self.manager.load()
        matches = [agent for agent in agents if agent.name == agent_id]
        declared = self.installation["agents"].get(agent_id)
        if (len(matches) != 1 or not isinstance(declared, dict) or declared.get("owner_id") != owner_id):
            raise PermissionDenied("Agent/owner has no installed permission policy")
        agent = matches[0]
        snapshot = self.config.read_agent_permission_settings(agent_id)
        if snapshot.settings.requested_level is PermissionLevel.L0:
            raise PermissionDenied("this Agent has left managed execution")
        extra = dict(agent.extra or {})
        state = workzone.normalize_workzone_state(extra.get("workzone_state"))
        if not state["slots"] and extra.get("workzone_dir"):
            state = workzone.normalize_workzone_state({"slots": [
                {"slot_id": "main", "path": extra["workzone_dir"], "enabled": True},
            ]})
        cwd = workzone.primary_workzone_path(state) or agent.workspace_dir
        roots = workzone.access_roots_for_workzones(agent.resolve_access_root(), state,
                                                   workspace_dir=agent.workspace_dir)
        backend = next((raw for raw in agent.allowed_backends if raw.get("engine") == "her-v2"), {})
        tools = dict(backend.get("tools") or extra.get("tools") or {})
        allowed = tools.pop("allowed", [])
        if not isinstance(allowed, list) or any(not isinstance(name, str) for name in allowed):
            raise PermissionDenied("installed tool policy is invalid")
        baseline = _scopes(declared["baseline_scopes"], Path(cwd))
        requestable = _scopes(declared["requestable_scopes"], Path(cwd))
        protected = self.protected_policy(global_config)
        digest = sha256(json.dumps({
            "config_revision": snapshot.revision, "base_revision": self.installation.revision,
            "cwd": str(cwd), "roots": [str(root) for root in roots],
            "settings": asdict(snapshot.settings), "tools": allowed, "tool_options": tools,
            "protected": asdict(protected.policy),
        }, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        revision = int(digest[:15], 16) + settings_revision(snapshot.settings)
        self._current[agent_id] = (revision, baseline, requestable, digest, protected)
        return ManagedRunPolicy(
            snapshot.settings, revision, Path(cwd), requestable, tuple(allowed),
            {"access_roots": list(roots), "tool_options": tools,
             "audit_context": {"agent_name": agent_id, "owner_id": owner_id,
                               "permission_policy_digest": digest, "global_config": global_config}},
        )

    def freeze(self, binding, policy):
        self._frozen[binding.context.execution_epoch] = (
            policy.revision, policy.registry_options["audit_context"]["permission_policy_digest"],
        )

    def baseline(self, context):
        policy = self(context.agent_id, context.owner_id, context.session_id)
        if policy.revision != context.policy_revision:
            raise PermissionDenied("permission policy changed")
        return self._current[context.agent_id][1]

    def validate(self, context, scope):
        policy = self(context.agent_id, context.owner_id, context.session_id)
        frozen = self._frozen.get(context.execution_epoch)
        current = self._current[context.agent_id]
        if frozen != (policy.revision, current[3]) or policy.revision != context.policy_revision:
            return False
        if not current[4].permits(scope):
            return False
        return any(scope_covers(parent, scope) for parent in (*current[1], *current[2]))


class _CheckpointUnavailable:
    def __call__(self, *args, **kwargs):
        raise PermissionDenied("no restricted HER checkpoint owner is installed")


def build_service(path, *, runner=None, checkpoint_writer=None, audit_writer=None, level_transition=None):
    path = _path(str(path))
    installation = read_config_json(path)
    required = {
        "version", "instance_id", "service_sid", "worker_sid", "frontend_sid", "private_root",
        "config_path", "secrets_path", "bridge_home", "code_root", "identity_path",
        "approval_key", "transport_key", "supervisor_key", "supervisor_key_id",
        "result_key", "helper", "endpoints", "owner_bindings", "agents", "protected_roots", "capsule",
    }
    if not required.issubset(installation) or set(installation) - required - {"transition", "runner"} or installation["version"] != 1:
        raise ValueError("invalid private permission installation")
    sid = current_process_user_sid()
    if sid != installation["service_sid"]:
        raise PermissionDenied("permission service is running under the wrong account")
    # Native service owners must be ordinary accounts. L2 additionally requires
    # independent Safety, Runner, and agent identities at executor creation.
    require_nonadmin_identity(sid)
    private = _path(installation["private_root"])
    require_private_path(private, trusted_sids=(sid,))
    require_private_path(path, trusted_sids=(sid,))
    code_root = _path(installation["code_root"])
    from orchestrator.permissions.runner_installation import read_capsule

    capsule = read_capsule(installation["capsule"], trusted_sids=(sid,))
    runner_descriptor = installation.get("runner")
    if runner is None and runner_descriptor is not None:
        from orchestrator.permissions.ipc_rpc import PermissionPipeClient
        from orchestrator.permissions.runner import RestrictedHerRunner, RunnerClient

        if not isinstance(runner_descriptor, dict) or set(runner_descriptor) != {
            "endpoint", "public_key", "runner_sid",
        }:
            raise ValueError("invalid pinned private Runner endpoint")
        runner = RunnerClient(
            PermissionPipeClient(runner_descriptor["endpoint"], Ed25519PublicKey.from_public_bytes(
                base64.b64decode(runner_descriptor["public_key"], validate=True),
            ), methods=RestrictedHerRunner.METHODS),
            runner_sid=runner_descriptor["runner_sid"], safety_sid=sid,
            worker_sid=installation["worker_sid"], instance_id=installation["instance_id"],
        )
        runner.qualify_sync()
    for entry in installation["protected_roots"]:
        _path(entry)

    def key_bytes(field, size):
        source = _path(installation[field])
        require_private_path(source, trusted_sids=(sid,))
        data = source.read_bytes()
        if len(data) != size:
            raise ValueError("private permission key size changed")
        return data

    approval = Ed25519PrivateKey.from_private_bytes(key_bytes("approval_key", 32))
    transport = Ed25519PrivateKey.from_private_bytes(key_bytes("transport_key", 32))
    supervisor_key = Ed25519PrivateKey.from_private_bytes(key_bytes("supervisor_key", 32))
    issuer = SupervisorReceiptIssuer(key_id=installation["supervisor_key_id"], signing_key=supervisor_key)
    manager = ConfigManager(_path(installation["config_path"]), _path(installation["secrets_path"]),
                            bridge_home=_path(installation["bridge_home"]), code_root=code_root)
    global_config, _, _ = manager.load()
    if str(global_config.instance_id).upper() != str(installation["instance_id"]).upper():
        raise PermissionDenied("permission installation belongs to another HASHI instance")
    policies = InstancePolicies(manager, installation, private_path=private)
    verifier = SignedReceiptVerifier(
        {"approval": approval.public_key().public_bytes_raw()},
        trusted_supervisor_keys={issuer.key_id: issuer.public_key},
    )
    ownership = InstallationLock(private)
    try:
        authority = PermissionAuthority(private / "permissions.sqlite", verifier=verifier,
                                        baseline_resolver=policies.baseline,
                                        settings_resolver=lambda subject: policies.settings(subject.agent_id))
        policies.authority = authority
        store = SessionStore.from_global_config(global_config)
        identity_path = _path(installation["identity_path"])
        if not identity_path.is_file():
            raise PermissionDenied("human permission owners must be provisioned before service startup")
        require_private_path(identity_path, trusted_sids=(sid,))
        identity = IdentityService.from_path(identity_path)
        helper = installation["helper"]
        if not isinstance(helper, dict) or set(helper) != {
            "executable", "entry", "executable_digest", "entry_digest", "private_root", "environment",
        }:
            raise ValueError("invalid protected helper installation")
        for field in ("executable", "entry"):
            require_private_path(_path(helper[field]), trusted_sids=(sid,), read_only_public=True)
        file_helper = InstalledFileHelper(
            _path(helper["executable"]), _path(helper["entry"]), helper["executable_digest"],
            helper["entry_digest"], _path(helper["private_root"]), dict(helper["environment"]),
        )
        require_private_path(file_helper.private_root, trusted_sids=(sid,))
        expected_entry = capsule.root / "app/orchestrator/permissions/file_worker.py"
        if (file_helper.executable.resolve() != capsule.executable.resolve()
                or file_helper.entry.resolve() != expected_entry.resolve()
                or file_helper.executable_digest != capsule.file_digest(capsule.executable)
                or file_helper.entry_digest != capsule.file_digest(expected_entry)
                or file_helper.environment != capsule.environment):
            raise PermissionDenied("the fixed helper does not match the qualified runtime capsule")
        factory = InstalledExecutorFactory(
            authority, store, helper=file_helper,
            results=EncryptedResultStore(private / "results.sqlite", key=key_bytes("result_key", 32)),
            stop_issuer=issuer, signing_key=transport, worker_sid=installation["worker_sid"],
            validate_resource=policies.validate, checkpoint_writer=checkpoint_writer or _CheckpointUnavailable(),
            audit_writer=audit_writer or _CheckpointUnavailable(), runner=runner,
            policy_observer=policies.freeze,
            model_factory=ConfiguredModelFactory(manager, authority, private_workspace=private),
        )
        qualified_l2 = runner is not None and runner.qualified_for(sid, installation["worker_sid"])
        levels = ("L1", "L2") if qualified_l2 else ("L1",)
        application = PermissionServiceApplication(
            authority, store, identity, policy_resolver=policies, executor_factory=factory,
            key_id="approval", signing_key=approval, owner_bindings=dict(installation["owner_bindings"]),
            levels=levels, settings=policies.settings,
        )
        application._installation_lock = ownership
        from orchestrator.permissions.management import PermissionManagement

        transition = installation.get("transition")
        if transition is not None:
            from orchestrator.permissions.ipc_rpc import PermissionPipeClient
            from orchestrator.permissions.level_transition import FrontendLevelTransitions, RemoteLevelTransition

            if not isinstance(transition, dict) or set(transition) != {"endpoint", "public_key"}:
                raise ValueError("invalid pinned private FC transition endpoint")
            level_transition = RemoteLevelTransition(
                PermissionPipeClient(transition["endpoint"], Ed25519PublicKey.from_public_bytes(
                    base64.b64decode(transition["public_key"], validate=True),
                ), methods=FrontendLevelTransitions.METHODS),
                run_service=application.runs, policies=policies, generation=application.generation, levels=levels,
            )
        application._management = PermissionManagement(
            authority, identity, owner_bindings=application.owners, config_admin=policies.config,
            run_service=application.runs, level_transition=level_transition,
        )
        endpoints = installation["endpoints"]
        if not isinstance(endpoints, dict) or set(endpoints) != {"pao", "control"} or endpoints["pao"] == endpoints["control"]:
            raise ValueError("distinct PAO and FC endpoint names are required")
        prepared = tuple(PermissionEndpoint(
            WindowsPipeListener(endpoints[role], service_sid=sid, peer_policy=PipePeerPolicy(peer)),
            router, signing_key=transport, service_generation=application.generation,
        ) for role, peer, router in (
            ("pao", installation["worker_sid"], application.pao_router),
            ("control", installation["frontend_sid"], application.control_router),
        ))
        return application, prepared
    except BaseException:
        ownership.close()
        raise


async def serve(path):
    application, endpoints = build_service(path)
    loop, stopped = asyncio.get_running_loop(), asyncio.Event()
    previous = {}
    for kind in (signal.SIGINT, signal.SIGTERM):
        previous[kind] = signal.getsignal(kind)
        signal.signal(kind, lambda *args: loop.call_soon_threadsafe(stopped.set))
    try:
        await application.start(endpoints)
        await stopped.wait()
    finally:
        await application.close()
        for kind, handler in previous.items():
            signal.signal(kind, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run an explicitly installed HASHI permission service")
    parser.add_argument("installation", type=Path)
    arguments = parser.parse_args(argv)
    asyncio.run(serve(arguments.installation))


if __name__ == "__main__":
    main()
