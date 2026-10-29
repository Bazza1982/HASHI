"""Central service composition, human management and inert L0 entry behavior."""
from dataclasses import replace
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from orchestrator.enterprise import IdentityService
from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied, RevisionConflict
from orchestrator.permissions.clients import service_clients
from orchestrator.permissions.management import PermissionManagement
from orchestrator.permissions.models import PermissionSubject, PermissionLevel, PermissionSettings, normalize_scope
from orchestrator.permissions.run_service import ManagedRunPolicy
from orchestrator.permissions.service import PermissionServiceApplication
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier
from orchestrator.permissions.wire import request_frame, decode
from tests.test_permission_pao_fence import setup_run
from tests.test_permission_run_service import ExecutorFactory


class Endpoint:
    async def start(self):
        self.started = True

    async def close(self):
        self.started = False
        return True


@pytest.fixture
def environment(tmp_path):
    store, _, accepted, fence, context = setup_run(tmp_path)
    identity = IdentityService.from_path(tmp_path / "humans.sqlite")
    identity.create_organization(org_id="people", name="People")
    identity.create_user(org_id="people", email="owner@example.test", display_name="Owner",
                         password="only-a-fixture-password", user_id="human")
    identity.create_user(org_id="people", email="other@example.test", display_name="Other",
                         password="only-a-fixture-password", user_id="other")
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    authority = PermissionAuthority(tmp_path / "permissions.sqlite",
                                    verifier=SignedReceiptVerifier({"approval": public}))
    factory = ExecutorFactory(authority)
    policy = ManagedRunPolicy(PermissionSettings(PermissionLevel.L1), 1, tmp_path, (), (), {})
    service = PermissionServiceApplication(
        authority, store, identity, policy_resolver=lambda *args: policy, executor_factory=factory,
        key_id="approval", signing_key=key, owner_bindings={"human": context.owner_id}, levels=("L1",),
        native_identities=[{"connector_id": "telegram", "actor_id": "731", "user_id": "human"}],
    )
    arguments = dict(request_id=accepted.request_id, run_id=accepted.run_id, owner_id=context.owner_id,
                     agent_id=context.agent_id, session_id=context.session_id, context_generation=1,
                     fencing_token=fence, worker_id="INSTANCE:agent", worker_generation="worker",
                     turn_id="request", backend_id="her-v2")
    subject = PermissionSubject(context.instance_id, context.owner_id, context.agent_id,
                                context.session_id, context.context_generation)
    return service, authority, identity, arguments, subject


@pytest.mark.asyncio
async def test_service_routes_keep_decisions_out_of_the_worker_surface(environment, tmp_path):
    service, authority, _, arguments, subject = environment
    with pytest.raises(PermissionDenied):
        service.health(instance_id=service.store.instance_id)
    await service.start((Endpoint(), Endpoint()))
    assert service.health(instance_id="INSTANCE")["levels"] == ["L1"]
    result = await service.pao_router.dispatch_async(request_frame("open_execution", arguments, request_id="open"))
    binding = decode(result["result"])
    assert binding.context.owner_id == subject.owner_id
    scope = normalize_scope("file", str(tmp_path / "report"), ("read",), cwd=tmp_path)
    request = authority.request_access(binding.context, [scope], purpose="Read report")
    login = service.login(org_id="people", email="owner@example.test", password="only-a-fixture-password")
    token = login["session"]["token"]
    with pytest.raises(PermissionError):
        service.pao_router.dispatch(request_frame("submit_session_decision", {}, request_id="forged"))
    with pytest.raises(PermissionError):
        service.control_router.dispatch(request_frame("open_execution", arguments, request_id="forged"))
    approved = service.submit_session_decision(
        subject, ["approve", request.request_id, "task", "--revision", "1"],
        session_token=token, invocation_id="human-command", connection_binding="private-fc",
    )
    assert approved.choice == "approve" and authority.evaluate(binding.context, [scope], operation_id="probe")
    summary = service.session_summary(subject=subject)
    assert summary["tasks"][0]["leases"][0].lease_id == approved.lease.lease_id
    assert service.session_summary(subject=replace(subject, owner_id="foreign"))["tasks"] == []
    assert token not in repr(summary) and token.encode() not in authority.path.read_bytes()
    assert service.logout(session_token=token)["revoked"]
    with pytest.raises(ValueError):
        service.submit_session_decision(
            subject, ["deny", request.request_id, "--revision", "1"],
            session_token=token, invocation_id="late", connection_binding="private-fc",
        )
    await service.close()


@pytest.mark.asyncio
async def test_management_narrows_without_granting_and_revokes_after_epoch_fence(environment, tmp_path):
    service, authority, identity, arguments, subject = environment
    binding = await service.open_execution(**arguments)
    original = normalize_scope("file", str(tmp_path), ("read", "write"), cwd=tmp_path)
    request = authority.request_access(binding.context, [original], purpose="Review report")
    session = identity.create_session(user_id="human")
    manager = PermissionManagement(authority, identity, owner_bindings=service.owners, config_admin=None,
                                    run_service=service.runs, level_transition=None)
    result = await manager.manage(
        subject, ["narrow", request.request_id, "--revision", "1", "--actions", "read"],
        session_token=session.token, invocation_id="narrow",
    )
    narrowed = result["request"].request
    assert narrowed.revision == 2 and narrowed.scopes[0].actions == ("read",)
    assert not authority.evaluate(binding.context, narrowed.scopes, operation_id="probe")
    with pytest.raises(RevisionConflict):
        service.submit_session_decision(
            subject, ["approve", request.request_id, "task", "--revision", "1"],
            session_token=session.token, invocation_id="stale", connection_binding="fc",
        )
    approval = service.submit_session_decision(
        subject, ["approve", request.request_id, "task", "--revision", "2"],
        session_token=session.token, invocation_id="approve", connection_binding="fc",
    )
    authority.fence_execution(binding.context)
    revoked = await manager.manage(subject, ["revoke", approval.lease.lease_id],
                                    session_token=session.token, invocation_id="revoke")
    assert revoked["kind"] == "revoked" and not revoked["cleanup_pending"]
    assert revoked["task"]["leases"][0].status == "revoked"
    assert authority.session_task_context(subject, binding.context.task_id) == binding.context


@pytest.mark.asyncio
async def test_other_human_and_ordinary_answer_cannot_manage(environment):
    service, authority, identity, arguments, subject = environment
    binding = await service.open_execution(**arguments)
    manager = PermissionManagement(authority, identity, owner_bindings=service.owners, config_admin=None,
                                    run_service=service.runs, level_transition=None)
    other = identity.create_session(user_id="other")
    for token in ("yes", other.token):
        with pytest.raises(ValueError):
            await manager.manage(subject, ["revoke-task", binding.context.task_id],
                                 session_token=token, invocation_id="wrong-owner")


@pytest.mark.asyncio
async def test_l0_bootstrap_does_not_construct_or_contact_a_service(monkeypatch):
    from orchestrator.permissions import bootstrap

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy L0 must not construct a permission client")
    monkeypatch.setattr(bootstrap, "service_clients", forbidden)
    for settings in (None, PermissionSettings()):
        runtime = SimpleNamespace(backend_manager=SimpleNamespace(_agent_permission_settings=lambda: (settings, {})))
        assert await bootstrap.bind_agent(runtime) is None


def test_installation_descriptor_rejects_unpinned_wrong_instance_and_merged_roles():
    key = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    import base64
    descriptor = {
        "version": 1, "instance_id": "INSTANCE",
        "pao_endpoint": r"\\.\pipe\LOCAL\HASHI.Permissions.pao",
        "control_endpoint": r"\\.\pipe\LOCAL\HASHI.Permissions.fc",
        "public_key": base64.b64encode(key).decode(),
    }
    config = SimpleNamespace(instance_id="INSTANCE", permission_service=descriptor)
    pao, fc = service_clients(config)
    assert pao.endpoint != fc.endpoint and "submit_session_decision" not in pao.methods
    for changed in (
        {**descriptor, "instance_id": "FOREIGN"},
        {**descriptor, "control_endpoint": descriptor["pao_endpoint"]},
        {**descriptor, "public_key": ""},
        {**descriptor, "pao_endpoint": "http://localhost:8080"},
    ):
        with pytest.raises(PermissionDenied):
            service_clients(SimpleNamespace(instance_id="INSTANCE", permission_service=changed))
