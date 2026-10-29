"""Essential finishing boundaries, using injected transport/kernel observations."""
import base64
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import io
from types import SimpleNamespace
from uuid import uuid4
import wave

import pytest

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import PermissionLevel, PermissionSettings, RuntimeExecutionBinding, normalize_scope
from tests.test_permission_service_application import environment as environment, Endpoint
from tests.test_permission_pao_fence import setup_run


@pytest.mark.asyncio
async def test_shared_native_command_authenticates_exact_link_without_an_api_actor_shortcut(environment, monkeypatch, tmp_path):
    from telegram import Update, User, Chat, Message
    from orchestrator.commands.safety import COMMANDS
    from orchestrator.permissions.clients import PermissionControlClient
    from orchestrator.permissions.frontend_native import original_subject
    from orchestrator.permissions.wire import decode, request_frame

    service, authority, identity, arguments, subject = environment
    binding = await service.open_execution(**arguments)
    scope = normalize_scope("file", str(tmp_path / "report"), ("read",), cwd=tmp_path)
    request = authority.request_access(binding.context, [scope], purpose="Read report")
    class Transport:
        def call(self, method, arguments):
            frame = service.control_router.dispatch(request_frame(method, arguments, request_id=uuid4().hex))
            assert "result" in frame, (method, frame.get("code"))
            return decode(frame["result"])
    control = PermissionControlClient(Transport())
    replies = []
    async def reply(update, text, **kwargs):
        replies.append(text)
    runtime = SimpleNamespace(
        name=subject.agent_id, global_config=SimpleNamespace(instance_id=subject.instance_id),
        session_store=service.store, permission_authority=control, _permission_native_control=control,
        _is_authorized_user=lambda actor: actor == 731, _reply_text=reply,
    )
    user = User(731, "Fixture human", False)
    message = Message(1, datetime.now(timezone.utc), Chat(731, "private"), from_user=user, text="/safety")
    update = Update(8001, message=message)
    session = service.store.get_session(subject.session_id, owner_id=subject.owner_id)
    monkeypatch.setattr("orchestrator.runtime_session.current_session_for_update", lambda *a, **k: session)
    context = SimpleNamespace(args=["approve", request.request_id, "task", "--revision", "1"])
    assert original_subject(runtime, update, context) == (subject, "telegram", "731")
    from orchestrator.commands.safety import _subject_for_update
    assert _subject_for_update(runtime, update)[1] == subject
    result = await COMMANDS[0].callback(runtime, update, context)
    assert result["ok"], replies
    assert authority.evaluate(binding.context, [scope])
    assert authority.decision_count(request.request_id) == 1 and replies
    with pytest.raises(PermissionDenied):
        original_subject(runtime, update, SimpleNamespace(frontend_connector_id="backend_api"))
    with pytest.raises(PermissionDenied):
        service.native_login(subject=subject, connector_id="telegram", actor_id="732", invocation_id="forged")
    with pytest.raises(PermissionError):
        service.pao_router.dispatch(request_frame("native_login", {}, request_id="forged"))
    with pytest.raises(PermissionDenied):
        service.native_login(subject=replace(subject, context_generation="2"), connector_id="telegram",
                             actor_id="731", invocation_id="stale")
    with identity.store.connect() as db:
        assert db.execute("SELECT count(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0] == 0


@pytest.mark.parametrize("level,native,file_state", [
    (PermissionLevel.L1, False, "enforced"),
    (PermissionLevel.L2, False, "unknown"),
    (PermissionLevel.L2, True, "partial"),
])
def test_observed_controls_never_turn_settings_or_mixed_approval_into_full_l2(tmp_path, level, native, file_state):
    from orchestrator.permissions.observed_controls import execution_report
    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    supervisor = SimpleNamespace(supports=lambda name: name == "file_read", credential_names=())
    registry = SimpleNamespace(_permission_gate=SimpleNamespace(_supervisor=supervisor))
    channel = SimpleNamespace(_current=lambda: None, registry=registry,
                              tool_definitions=lambda: [{"function": {"name": name}} for name in ("file_read", "desktop_click")])
    observation = {"lpac": True, "capability_count": 0, "elevated": False, "atomic_job": True, "parent_job_count": 1}
    factory = SimpleNamespace(authority=SimpleNamespace(observed_time=lambda: 1000, _service_generation="observed"),
                              helper=SimpleNamespace(entry_digest="a" * 64, executable_digest="b" * 64),
                              runner=SimpleNamespace(_prepared={context.execution_epoch: observation} if native else {}))
    execution = SimpleNamespace(factory=factory, binding=binding, channel=channel, _closed=False, _inference=None,
                                policy=SimpleNamespace(settings=PermissionSettings(level), revision=1))
    report = execution_report(execution)
    assert report["controls"]["files"]["state"] == file_state
    assert report["controls"]["devices"]["state"] == "unavailable"
    assert report["controls"]["approval"]["state"] == ("enforced" if level is PermissionLevel.L1 else "partial")
    assert report["inventory"] == [{"tool": "desktop_click", "available": False}, {"tool": "file_read", "available": True}]


@pytest.mark.asyncio
async def test_audio_output_is_durable_in_original_owner_and_bound_to_the_selected_profile(tmp_path):
    from orchestrator.audio_assets import AudioAssetStore, AudioAssetUnauthorized, asset_root_from_global_config
    from orchestrator.permissions.media_bridge import RunMedia
    from orchestrator.permissions.media_output import invoke_audio
    from tests.test_permission_media_bridge import content

    _, _, accepted, fence, context = setup_run(tmp_path)
    raw = io.BytesIO()
    with wave.open(raw, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 160)
    payload = raw.getvalue()
    policy = {"mime_type": "audio/wav", "asset_format": "wav", "retention_seconds": 3600, "retention_indefinite": False}
    config = SimpleNamespace(bridge_home=tmp_path)
    provider = SimpleNamespace(runtime_context=SimpleNamespace(global_config=config),
                               backend_manager=SimpleNamespace(create_herv3_provider_backend=lambda *args: SimpleNamespace(
                                   _native_audio_output_profile=lambda content: policy)))
    turn = SimpleNamespace(binding=RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent"),
                           media=RunMedia(content(tmp_path / "accepted.ogg", b"accepted"), "Request"),
                           executor=SimpleNamespace(_stage_provider=provider))
    metadata = {"mime_type": "audio/wav", "audio_format": "wav", "retention_seconds": 3600, "retention_indefinite": False}
    declaration = {"engine": "openrouter-api", "model": "qualified-audio", "metadata": metadata,
                   "size": len(payload), "digest": sha256(payload).hexdigest()}
    with pytest.raises(PermissionDenied):
        await invoke_audio(turn, "audio_begin", [], {**declaration, "metadata": {**metadata, "retention_indefinite": True}})
    token = await invoke_audio(turn, "audio_begin", [], declaration)
    chunk = {"token": token, "offset": 0, "data": base64.b64encode(payload).decode()}
    await invoke_audio(turn, "audio_chunk", [], chunk)
    await invoke_audio(turn, "audio_chunk", [], chunk)
    first = await invoke_audio(turn, "audio_complete", [], {"token": token})
    assert await invoke_audio(turn, "audio_complete", [], {"token": token}) == first
    store = AudioAssetStore(asset_root_from_global_config(config))
    _, stored = store.read_bytes(first["asset_id"], owner_id=context.owner_id, session_id=context.session_id)
    assert stored == payload
    with pytest.raises(AudioAssetUnauthorized):
        store.describe(first["asset_id"], owner_id="another", session_id=context.session_id)


def test_worker_usage_is_committed_before_observer_and_recovered_once_with_original_phase(tmp_path):
    from orchestrator.permissions.model_receipts import ModelReceiptJournal
    from orchestrator.permissions.worker_usage import WorkerAccounting
    from tests.test_permission_installed_boundaries import receiver, usage_response
    from tools.meter_cost import PerCallUsageLineItem

    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    journal = ModelReceiptJournal(tmp_path / "usage.sqlite")
    receipt = {**deepcopy(usage_response()["provider_evidence"][0]), "origin": "worker",
               "execution_epoch": context.execution_epoch}
    journal.reserve(binding, {**receipt, "status": "unknown"})
    journal.complete(binding, receipt)
    class Client:
        def __init__(self, binding):
            self.binding = binding
        def call(self, method, args):
            if method == "worker_usage_receipts":
                return journal.observations(self.binding, origin="worker")
            assert method == "project_model_usage"
            return journal.commit_projections(self.binding, args["projections"])
    first = receiver()
    def lost_observer(item):
        raise OSError("canonical meter receipt not acknowledged")
    first.usage_observer = lost_observer
    accounting = WorkerAccounting(first, Client(binding), binding)
    line = PerCallUsageLineItem(request_id="original-worker", phase="final_style",
                                engine=receipt["engine"], model=receipt["model"])
    with pytest.raises(OSError):
        accounting.observe(line)
    assert journal.observations(binding, origin="worker")[0]["projection"]["phase"] == "final_style"
    renewed = replace(binding, context=replace(context, execution_epoch="renewed"))
    delivered = []
    second = receiver()
    second.usage_observer = lambda item: delivered.append(item.request_id)
    WorkerAccounting(second, Client(renewed), renewed)
    WorkerAccounting(second, Client(renewed), renewed)
    assert delivered == ["permission-provider:" + receipt["id"]]
    assert second.usage_line_items[0].phase == "final_style"
    assert second.usage.input_tokens == receipt["input_tokens"]


def test_stale_physical_snapshot_after_known_cleanup_does_not_need_deleted_plan(tmp_path):
    from tests.test_permission_physical_journal import interrupted
    _, _, _, journal, _, _ = interrupted(tmp_path)
    snapshot = journal.pending()[0]
    journal.reconcile()
    for path in journal.root.rglob("*.json"):
        path.unlink()
    journal._reconcile_stopped(snapshot[1], snapshot[2])
    assert not journal.pending()


def test_one_physical_row_failure_does_not_skip_an_independent_settlement(tmp_path, monkeypatch):
    from tests.test_permission_physical_journal import interrupted
    authority, _, _, journal, _, _ = interrupted(tmp_path)
    good = journal.pending()[0]
    bad = deepcopy(good)
    bad[1]["descriptor"]["arguments_digest"] = "0" * 64
    monkeypatch.setattr(journal, "pending", lambda: [bad, good])
    with pytest.raises(OSError):
        journal.reconcile()
    assert authority.operation_state("original-operation") == "succeeded"


@pytest.mark.asyncio
async def test_old_recovery_failure_keeps_human_controls_and_independent_service_available(environment):
    service, _, _, _, _ = environment
    async def reconcile():
        raise OSError("one old owner is unavailable")
    service._factory.reconcile = reconcile
    await service.start((Endpoint(), Endpoint()))
    assert service.health(instance_id=service.store.instance_id)["recovery_pending"]
    await service.close()


@pytest.mark.asyncio
async def test_script_original_result_survives_profile_cleanup_failure_without_rerun(tmp_path):
    from pathlib import Path
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.result_store import EncryptedResultStore
    from orchestrator.permissions.script_runner import RestrictedScriptRuns
    from tests.test_permission_runner_records import environment as runner_environment

    binding, records, _, profiles = runner_environment(tmp_path)
    declaration = records.reserve(binding, operation_id="script-result")
    scratch = Path(declaration["scratch"])
    scratch.mkdir()
    (scratch / "temporary.txt").write_text("will be retired")
    results = EncryptedResultStore(tmp_path / "runner-results.sqlite", key=bytes(range(32)))
    args = {"script": "print('saved')", "inputs": [], "outputs": []}
    descriptor = describe_tool("run_script", args, cwd=tmp_path, read_roots=(tmp_path,))
    original = results.put(binding.context, "script-result", descriptor, outcome="succeeded", payload="original script result")
    scripts = RestrictedScriptRuns.__new__(RestrictedScriptRuns)
    scripts.runner, scripts.results, scripts._owned = SimpleNamespace(records=records), results, {}
    def fail(name):
        raise OSError("profile cleanup not acknowledged")
    records._remove_profile = fail
    with pytest.raises(OSError):
        await scripts.result(binding=binding, operation_id="script-result", descriptor=asdict(descriptor))
    assert scratch.exists() and results.get(binding.context, "script-result", descriptor) == original
    records._remove_profile = profiles.append
    recovered = await scripts.result(binding=binding, operation_id="script-result", descriptor=asdict(descriptor))
    assert recovered["payload"] == "original script result" and not scratch.exists()
    assert await scripts.result(binding=binding, operation_id="script-result", descriptor=asdict(descriptor)) == recovered
    assert profiles == [declaration["profile"]]


@pytest.mark.asyncio
async def test_backend_change_fences_locally_even_when_the_safety_service_is_offline(tmp_path):
    from orchestrator.permissions.her_binding import PermissionAgentController
    from orchestrator.permissions.pao_run_fence import PaoRunFence

    store, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    notes = []
    async def unavailable(binding):
        raise OSError("service is offline")
    service = SimpleNamespace(store=store, open_execution=lambda: None, execution_registry=lambda: None,
                              settle_run=lambda: None, close_execution=lambda: None,
                              fence_for_backend_switch=unavailable)
    runtime = SimpleNamespace(name=context.agent_id, error_logger=SimpleNamespace(warning=notes.append))
    controller = PermissionAgentController(runtime, service)
    controller._owned_bindings[context.execution_epoch] = binding
    await controller.before_backend_switch("codex-cli")
    guard = PaoRunFence(store, context=context, request_id=accepted.request_id, fencing_token=fence, worker_id="INSTANCE:agent")
    with pytest.raises(PermissionDenied):
        with guard(context):
            pytest.fail("old execution may not continue")
    assert notes and controller._owned_bindings
    assert store.get_run(context.run_id, owner_id=context.owner_id)["error_text"] == "backend_changed_to_unmanaged"
