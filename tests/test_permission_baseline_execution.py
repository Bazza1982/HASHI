"""Installed policy -> factory -> registry -> ledger integration.

Only the native launcher/IPC and backend-configuration input are injected. The
policy reader, approval ledger, gate, start rechecks and temporary file effects
are real; no HASHI instance or installed service is started.
"""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied
from orchestrator.permissions.execution_gate import GateResult, SupervisedResult
from orchestrator.permissions.installation import InstancePolicies
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.native_factory import InstalledExecutorFactory
from tests.test_permission_execution_gate import Issuer
from tests.test_permission_pao_fence import setup_run
from tools.registry import ToolResult


class Installation(dict):
    @property
    def revision(self):
        return sha256(json.dumps(self, sort_keys=True).encode()).hexdigest()


class FileEffects:
    """In-process physical effect; uses the real prepare/start authority fence."""

    credential_names = ()

    def __init__(self, authority, *, cwd, validate_resource, **kwargs):
        self.authority, self.cwd, self.validate_resource = authority, cwd, validate_resource
        self.queued, self.prepared = asyncio.Event(), asyncio.Event()
        self.queue_release, self.start_release = asyncio.Event(), asyncio.Event()
        self.queue_release.set()
        self.start_release.set()
        self.writes = []

    def supports(self, name):
        return name == "file_write"

    async def execute(self, context, operation_id, descriptor, arguments):
        self.queued.set()
        await self.queue_release.wait()
        if not all(self.validate_resource(context, scope) for scope in descriptor.scopes):
            raise PermissionDenied("policy changed before preparation")
        claim = self.authority.prepare(context, operation_id, descriptor.arguments_digest, descriptor.scopes)
        self.prepared.set()
        try:
            await self.start_release.wait()
            # Independently re-reads baseline and lease facts at the last start.
            self.authority.start(context, claim.claim_id)
        except BaseException:
            self.authority.cancel_prepared(context, claim.claim_id)
            self.authority._verifier.confirm_stopped(self.authority, context, claim.claim_id)
            raise
        target = self.cwd / arguments["path"]
        target.write_text(arguments["content"], encoding="utf-8")
        self.writes.append(operation_id)
        digest = sha256(target.read_bytes()).hexdigest()
        self.authority.finish(context, claim.claim_id, outcome="succeeded", result_digest=digest)
        self.authority._verifier.confirm_stopped(self.authority, context, claim.claim_id)
        return SupervisedResult(operation_id, descriptor.arguments_digest, "succeeded", digest, arguments["content"])

    async def close(self):
        return True


@pytest.fixture
def installed(tmp_path, monkeypatch):
    async def build(baseline, requestable):
        store, _, accepted, fence, original = setup_run(tmp_path)
        business = tmp_path / "business"
        business.mkdir()
        config_path = tmp_path / "agents.json"
        config_path.write_text(json.dumps({"agents": [
            {"name": "agent", "permission_settings": {"requested_level": "L1"}},
        ]}), encoding="utf-8")
        def declared(actions):
            return ([{"kind": "file", "resource": str(business), "actions": list(actions)}]
                    if actions else [])
        installation = Installation({
            "agents": {"agent": {"owner_id": "owner", "baseline_scopes": declared(baseline),
                                  "requestable_scopes": declared(requestable)}},
            "helper": {"private_root": str(tmp_path / "helper-private"),
                       "executable": str(tmp_path / "runtime/python.exe"),
                       "entry": str(tmp_path / "runtime/file_worker.py")},
            "protected_roots": [], "config_path": str(config_path),
            **{name: str(tmp_path / "private" / name) for name in (
                "identity_path", "approval_key", "transport_key", "supervisor_key", "result_key",
            )},
        })
        global_config = SimpleNamespace(project_root=tmp_path / "code", bridge_home=tmp_path / "bridge",
                                        secrets_path=tmp_path / "private/secrets.json", instance_id="INSTANCE")
        agent = SimpleNamespace(name="agent", workspace_dir=business, resolve_access_root=lambda: business)
        manager = SimpleNamespace(config_path=config_path, load=lambda: (global_config, [agent], {}))
        class BackendOptions:
            def __init__(self, *args):
                pass
            def _select_backend_cfg(self, name):
                return object()
            def _resolve_tools_config(self, backend):
                return {"allowed": ["file_write"]}
        monkeypatch.setattr("orchestrator.flexible_backend_manager.FlexibleBackendManager", BackendOptions)
        policies = InstancePolicies(manager, installation, private_path=tmp_path / "private", run_store=store)
        policy = policies("agent", "owner", original.session_id)
        policy.registry_options["audit_context"]["safety_mode"] = "read_write"
        context = replace(original, policy_revision=policy.revision)
        binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
        issuer = Issuer()
        authority = PermissionAuthority(tmp_path / "permissions.sqlite", verifier=issuer,
                                        baseline_resolver=policies.baseline,
                                        clock=lambda: 1000.0, monotonic=lambda: 1000.0)
        authority.open_task(context)
        class Endpoint:
            def __init__(self, *args, **kwargs):
                pass
            async def start(self):
                pass
            async def close(self):
                return True
        monkeypatch.setattr("orchestrator.permissions.native_factory.WindowsFileProcessSupervisor", FileEffects)
        monkeypatch.setattr("orchestrator.permissions.native_factory.WindowsPipeListener", lambda *a, **k: None)
        monkeypatch.setattr("orchestrator.permissions.native_factory.PermissionEndpoint", Endpoint)
        monkeypatch.setattr("orchestrator.permissions.native_factory.current_process_user_sid", lambda: "S-1-5-21-1")
        helper = SimpleNamespace(verify=lambda: None, executable=tmp_path / "runtime/python.exe",
                                 entry=tmp_path / "runtime/file_worker.py", executable_digest="a" * 64,
                                 entry_digest="b" * 64, private_root=tmp_path / "private",
                                 environment={})
        factory = InstalledExecutorFactory(
            authority, store, helper=helper, results=None,
            stop_issuer=SimpleNamespace(key_id=context.executor_id, issue_stop=lambda *a: "unused"),
            signing_key=Ed25519PrivateKey.generate(), worker_sid="S-1-5-21-2",
            validate_resource=policies.validate, checkpoint_writer=lambda *a: None,
            audit_writer=lambda *a: None, policy_observer=policies.freeze,
        )
        executor = await factory.create(binding, policy)
        registry = executor.channel.registry
        return SimpleNamespace(authority=authority, issuer=issuer, context=context, registry=registry,
                               supervisor=registry._permission_gate._supervisor, policy=policy,
                               policies=policies, installation=installation, root=business, executor=executor)
    return build


async def execute(env, name, arguments, call_id):
    from orchestrator.permissions.continuation import PermissionPark

    try:
        return await env.registry.execute(name, arguments, call_id)
    except PermissionPark as parked:
        return parked.result


SCOPES = [
    pytest.param(("create", "write"), (), "existing", id="baseline-only"),
    pytest.param((), ("create", "write"), "request", id="requestable-only"),
    pytest.param(("create", "write"), ("create", "write"), "existing", id="overlap"),
    pytest.param(("create",), ("write",), "request", id="split-actions"),
    pytest.param(("read",), ("read",), "forbidden", id="forbidden"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline,requestable,expected", SCOPES)
@pytest.mark.parametrize("proactive", [False, True], ids=["execute", "request-access"])
async def test_installed_scope_authorities_remain_separate(installed, baseline, requestable, expected, proactive):
    env = await installed(baseline, requestable)
    try:
        args = {"path": "report.txt", "content": "single effect"}
        if proactive:
            result = await execute(env, "request_access", {
                "reason": "save this report", "resources": [{
                    "kind": "filesystem", "path": str(env.root), "actions": ["create", "write"],
                }], "duration": {"mode": "task", "max_seconds": 60}, "request_key": "report",
            }, "proactive")
            if isinstance(result, GateResult):
                assert result.state == "waiting"
                payload = {"status": "pending"}
            else:
                assert isinstance(result, ToolResult) and not result.is_error
                payload = json.loads(result.output)
            assert payload["status"] == {"existing": "granted_existing", "request": "pending",
                                          "forbidden": "unavailable"}[expected]
            assert not env.supervisor.writes
        else:
            result = await execute(env, "file_write", args, "write-once")
            if expected == "forbidden":
                assert isinstance(result, ToolResult) and result.details["control_disposition"] == "denied"
                assert not env.supervisor.writes
            else:
                if expected == "request":
                    assert isinstance(result, GateResult) and result.state == "waiting"
                    request = env.authority.get_request(env.context, result.request_id)
                    operation = env.authority.operation_status_for_task(env.context, result.operation_id)
                    assert not env.supervisor.writes and not (env.root / "report.txt").exists()
                    if baseline == ("create",):
                        assert request.scopes[0].actions == ("write",)
                    env.issuer.approve(env.authority, env.context, request, operation.arguments_digest)
                    result = await execute(env, "file_write", args, "write-once")
                assert isinstance(result, ToolResult) and not result.is_error
                assert result.details["control_disposition"] == "allowed"
                assert (env.root / "report.txt").read_text(encoding="utf-8") == "single effect"
                assert len(env.supervisor.writes) == 1
        requests = env.authority.list_requests(env.context)
        assert len(requests) == (1 if expected == "request" else 0)
        if expected == "request":
            # Baseline create may not become part of an approval.
            assert requests[0].scopes[0].actions == tuple(sorted(requestable))
        denied = await execute(env, "file_read", {"path": "report.txt"}, "not-allowed-tool")
        assert isinstance(denied, ToolResult) and denied.is_error
    finally:
        await env.executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "queued", "prepared"])
@pytest.mark.parametrize("source", ["baseline", "requestable"])
async def test_policy_change_before_execution_cannot_use_frozen_eligibility(installed, stage, source):
    env = await installed(("create", "write") if source == "baseline" else (),
                          ("create", "write") if source == "requestable" else ())
    try:
        if source == "requestable":
            parked = await execute(env, "file_write", {"path": "changed.txt", "content": "must not write"}, "changed-policy")
            assert isinstance(parked, GateResult) and parked.state == "waiting"
            request = env.authority.get_request(env.context, parked.request_id)
            operation = env.authority.operation_status_for_task(env.context, parked.operation_id)
            env.issuer.approve(env.authority, env.context, request, operation.arguments_digest)
        if stage != "admission":
            (env.supervisor.queue_release if stage == "queued" else env.supervisor.start_release).clear()
            task = asyncio.create_task(execute(
                env, "file_write", {"path": "changed.txt", "content": "must not write"}, "changed-policy"))
            try:
                await asyncio.wait_for((env.supervisor.queued if stage == "queued" else env.supervisor.prepared).wait(), 1)
            finally:
                env.installation["agents"]["agent"][source + "_scopes"] = []
                env.supervisor.queue_release.set()
                env.supervisor.start_release.set()
            result = await asyncio.wait_for(task, 1)
        else:
            env.installation["agents"]["agent"][source + "_scopes"] = []
            result = await execute(env, "file_write", {"path": "changed.txt", "content": "must not write"}, "changed-policy")
        assert (isinstance(result, GateResult) and result.state in {"unavailable", "permission_changed", "unknown"}
                or isinstance(result, ToolResult) and result.details["control_disposition"] == "denied")
        assert not env.supervisor.writes and not (env.root / "changed.txt").exists()
        assert len(env.authority.list_requests(env.context)) == (1 if source == "requestable" else 0)
    finally:
        await env.executor.close()


@pytest.mark.asyncio
async def test_revocation_at_final_start_cannot_be_masked_by_partial_baseline(installed):
    env = await installed(("create",), ("write",))
    try:
        args = {"path": "revoked.txt", "content": "must not write"}
        parked = await execute(env, "file_write", args, "revoke-at-start")
        assert isinstance(parked, GateResult) and parked.state == "waiting"
        request = env.authority.get_request(env.context, parked.request_id)
        operation = env.authority.operation_status_for_task(env.context, parked.operation_id)
        lease = env.issuer.approve(env.authority, env.context, request, operation.arguments_digest)
        env.supervisor.start_release.clear()
        task = asyncio.create_task(execute(env, "file_write", args, "revoke-at-start"))
        try:
            await asyncio.wait_for(env.supervisor.prepared.wait(), 1)
            env.authority.revoke(env.context, lease.lease_id)
        finally:
            env.supervisor.start_release.set()
        result = await asyncio.wait_for(task, 1)
        assert isinstance(result, GateResult) and result.state in {"permission_changed", "unknown"}
        assert not env.supervisor.writes and not (env.root / "revoked.txt").exists()
        assert env.authority.get_lease_for_task(env.context, lease.lease_id).status == "revoked"
    finally:
        await env.executor.close()


@pytest.mark.asyncio
async def test_declared_baseline_is_not_a_grant_or_an_expanded_approval_ceiling(installed):
    env = await installed(("create", "write"), ())
    try:
        first = await execute(env, "file_write", {"path": "allowed.txt", "content": "existing baseline"}, "first")
        assert isinstance(first, ToolResult) and first.details["control_disposition"] == "allowed"
        # Eligibility remains frozen, but the authoritative grant source is lost.
        env.authority._baseline_resolver = lambda context: ()
        denied = await execute(env, "file_write", {"path": "forbidden.txt", "content": "must not write"}, "second")
        assert isinstance(denied, GateResult) and denied.state == "permission_changed"
        proactive = await execute(env, "request_access", {
            "reason": "cannot turn baseline into approval rights", "resources": [{
                "kind": "filesystem", "path": str(env.root), "actions": ["create", "write"],
            }], "duration": {"mode": "task", "max_seconds": 60}, "request_key": "no-expansion",
        }, "proactive")
        assert json.loads(proactive.output)["status"] == "unavailable"
        assert len(env.supervisor.writes) == 1 and not (env.root / "forbidden.txt").exists()
        assert env.authority.list_requests(env.context) == ()
    finally:
        await env.executor.close()
