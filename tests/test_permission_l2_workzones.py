"""L2's installed scope ceiling comes from PAO Workzones, independently of grants.

The existing installed-policy fixture injects native launch/IPC and config
inputs. These are local integration checks, not Windows isolation qualification.
"""
import asyncio
import json
import os
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.execution_gate import GateResult
from orchestrator.permissions.models import PermissionLevel, PermissionSubject, normalize_scope, uncovered_scope_actions
from tests.test_permission_baseline_execution import execute, installed
from tools.registry import ToolResult


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["L1", "L2"])
@pytest.mark.parametrize("source", ["baseline", "requestable"])
async def test_workzone_ceiling_cannot_be_expanded_by_an_installed_grant(installed, level, source):
    actions = ("create", "write")
    env = await installed(actions if source == "baseline" else (),
                          actions if source == "requestable" else (),
                          level=level, workzone=True, broad=True)
    try:
        target = env.root.parent / "outside.txt"
        args = {"path": str(target), "content": "owned test canary"}
        result = await execute(env, "file_write", args, "outside")
        if level == "L2":
            assert isinstance(result, ToolResult) and result.details["control_disposition"] == "denied"
            assert not target.exists() and not env.supervisor.writes
            proactive = await execute(env, "request_access", {
                "reason": "cannot approve outside the hard Workzone",
                "resources": [{"kind": "filesystem", "path": str(target),
                               "actions": ["create", "write"]}],
                "duration": {"mode": "task", "max_seconds": 60},
                "request_key": "outside-workzone",
            }, "outside-request")
            assert isinstance(proactive, ToolResult)
            assert json.loads(proactive.output)["status"] == "unavailable"
            assert env.authority.list_requests(env.context) == ()
        else:
            # L1's reviewed scope and ordinary approval behavior is preserved.
            if source == "requestable":
                assert isinstance(result, GateResult) and result.state == "waiting"
                assert not target.exists() and not env.supervisor.writes
            else:
                assert isinstance(result, ToolResult) and not result.is_error
                assert target.read_text(encoding="utf-8") == args["content"]
                assert len(env.supervisor.writes) == 1

        preview = env.policies.preview("agent", "owner")
        expected = [normalize_scope("file", str(env.root if level == "L2" else env.root.parent), ["read"], cwd=env.root).resource]
        scope_key = source + "_scopes"
        assert [item["resource"] for item in preview[scope_key]] == expected
        assert [scope.resource for scope in getattr(env.policy, scope_key)] == expected
        if level == "L2":
            assert preview["workzone_boundary"]["roots"] == [str(env.root)]
            assert preview["workzone_boundary"]["revision"] == 1

        # An in-zone operation still needs the same baseline or real approval.
        args = {"path": "inside.txt", "content": "inside canary"}
        result = await execute(env, "file_write", args, "inside")
        if source == "requestable":
            assert isinstance(result, GateResult) and result.state == "waiting"
            request = env.authority.get_request(env.context, result.request_id)
            operation = env.authority.operation_status_for_task(env.context, result.operation_id)
            env.issuer.approve(env.authority, env.context, request, operation.arguments_digest)
            result = await execute(env, "file_write", args, "inside")
        assert isinstance(result, ToolResult) and not result.is_error
        assert (env.root / "inside.txt").read_text(encoding="utf-8") == "inside canary"
    finally:
        await env.executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("workzone", [False, "unavailable"])
async def test_l2_requires_explicit_available_workzones(installed, workzone):
    try:
        env = await installed(("read",), (), level="L2", workzone=workzone, broad=True)
    except PermissionDenied:
        return
    try:
        pytest.fail("L2 must not fall back to a broad access_root or Agent home")
    finally:
        await env.executor.close()


@pytest.mark.asyncio
async def test_l2_attached_only_workzones_do_not_add_agent_home(installed):
    env = await installed(("read",), ("write", "create"), level="L2",
                          workzone="attached", broad=True)
    try:
        zone = env.root.parent / "attached"
        assert env.policy.cwd == zone
        assert env.policy.registry_options["access_roots"] == [zone]
        for scopes in (env.policy.baseline_scopes, env.policy.requestable_scopes):
            assert [Path(scope.resource) for scope in scopes] == [zone]
        home_file = normalize_scope("file", str(env.root / "home.txt"), ["read"], cwd=zone)
        assert not env.policies.validate(env.context, home_file)
    finally:
        await env.executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["L1", "L2"])
@pytest.mark.parametrize("stage", ["admission", "queued", "prepared"])
async def test_workzone_aba_cannot_revive_an_old_l2_epoch(installed, level, stage):
    env = await installed(("create", "write"), (), level=level, workzone=True)
    args = {"path": "revision.txt", "content": "revision canary"}
    try:
        task = None
        if stage != "admission":
            (env.supervisor.queue_release if stage == "queued" else env.supervisor.start_release).clear()
            task = asyncio.create_task(execute(env, "file_write", args, "aba"))
            await asyncio.wait_for(
                (env.supervisor.queued if stage == "queued" else env.supervisor.prepared).wait(), 3)
        try:
            env.store.set_agent_workzone_slot(
                owner_id="owner", agent_id="agent", slot_id="main", enabled=False, expected_revision=1)
            env.store.set_agent_workzone_slot(
                owner_id="owner", agent_id="agent", slot_id="main", enabled=True, expected_revision=2)
        finally:
            env.supervisor.queue_release.set()
            env.supervisor.start_release.set()
        current = env.policies("agent", "owner", env.context.session_id)
        result = await asyncio.wait_for(task, 3) if task else await execute(env, "file_write", args, "aba")
        if level == "L2":
            assert current.revision != env.policy.revision
            assert (isinstance(result, GateResult) and
                    result.state in {"unavailable", "permission_changed", "unknown"}
                    or isinstance(result, ToolResult) and result.is_error)
            assert not env.supervisor.writes and not (env.root / "revision.txt").exists()
        else:
            assert current.revision == env.policy.revision
            assert isinstance(result, ToolResult) and not result.is_error
            assert (env.root / "revision.txt").read_text(encoding="utf-8") == args["content"]
    finally:
        env.supervisor.queue_release.set()
        env.supervisor.start_release.set()
        if task is not None and not task.done():
            await asyncio.wait_for(task, 3)
        await env.executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["L1", "L2"])
async def test_script_inputs_and_unrestricted_execution_share_the_l2_ceiling(installed, level):
    env = await installed(("read", "list"), (), level=level, workzone=True, broad=True)
    try:
        declared = env.installation["agents"]["agent"]["baseline_scopes"]
        declared.extend([
            {"kind": "capability", "resource": "process.python.isolated", "actions": ["execute"]},
            {"kind": "capability", "resource": "process.unrestricted", "actions": ["execute"]},
            {"kind": "network", "resource": "https://example.invalid", "actions": ["fetch", "send"]},
        ])
        policy = env.policies("agent", "owner", env.context.session_id)
        scopes = (*policy.baseline_scopes, *policy.requestable_scopes)
        def covered(name, args):
            descriptor = describe_tool(name, args, cwd=policy.cwd,
                                       read_roots=tuple(policy.registry_options["access_roots"]))
            return not uncovered_scope_actions(scopes, descriptor.scopes)

        script = {"script": "print('canary')",
                  "inputs": [{"path": str(env.root / "inside.txt"), "name": "inside.txt"}]}
        assert covered("run_script", script)
        # Changing entrypoints or spelling does not create filesystem authority.
        for path in (env.root.parent / "outside.txt",
                     env.root / ".." / "outside.txt",
                     env.root.parent / "business-sibling" / "outside.txt"):
            script["inputs"][0]["path"] = str(path)
            assert covered("run_script", script) is (level == "L1")
            assert covered("file_read", {"path": str(path)}) is (level == "L1")
            assert covered("file_search", {"roots": [str(path.parent)]}) is (level == "L1")
        assert covered("shell", {"command": "unused"}) is (level == "L1")
        assert covered("verification_run", {"operation": "run", "argv": ["unused"]}) is (level == "L1")
        assert covered("file_search", {})
        # Workzone narrowing neither invents nor removes network grants.
        assert covered("http_request", {"url": "https://example.invalid/data"})
        assert not covered("http_request", {"url": "https://unapproved.invalid/data"})
    finally:
        await env.executor.close()


@pytest.mark.asyncio
async def test_l2_keeps_separate_roots_and_preserves_narrower_action_scopes(installed):
    env = await installed(("read",), ("create", "write"), level="L2", workzone=True, broad=True)
    try:
        attached = env.root.parent / "attached"
        attached.mkdir()
        env.store.set_agent_workzone_slot(
            owner_id="owner", agent_id="agent", slot_id="1", path=str(attached), expected_revision=1)
        policy = env.policies("agent", "owner", env.context.session_id)
        assert {Path(scope.resource) for scope in policy.baseline_scopes} == {env.root, attached}
        assert {Path(scope.resource) for scope in policy.requestable_scopes} == {env.root, attached}
        gap = normalize_scope("file", str(env.root.parent / "gap.txt"), ["read"], cwd=env.root)
        assert uncovered_scope_actions(policy.baseline_scopes, (gap,))

        declared = env.installation["agents"]["agent"]
        declared["baseline_scopes"] = [
            {"kind": "file", "resource": str(env.root / "narrow.txt"), "actions": ["read"]},
            {"kind": "file", "resource": str(env.root.parent / "gap.txt"), "actions": ["read"]},
        ]
        env.store.set_agent_workzone_slot(
            owner_id="owner", agent_id="agent", slot_id="1", enabled=False, expected_revision=2)
        policy = env.policies("agent", "owner", env.context.session_id)
        assert len(policy.baseline_scopes) == 1
        assert Path(policy.baseline_scopes[0].resource) == env.root / "narrow.txt"
        assert policy.baseline_scopes[0].actions == ("read",)
        assert {Path(scope.resource) for scope in policy.requestable_scopes} == {env.root}
        assert policy.requestable_scopes[0].actions == ("create", "write")
    finally:
        await env.executor.close()


def level_transition(env):
    from orchestrator.permissions.ipc_rpc import PermissionPipeClient
    from orchestrator.permissions.level_transition import RemoteLevelTransition

    class NoNetwork(PermissionPipeClient):
        def __init__(self):
            pass
        async def call_async(self, *args):
            raise AssertionError("a review cannot start an operational transition")
    return RemoteLevelTransition(
        NoNetwork(), run_service=SimpleNamespace(), policies=env.policies,
        generation="qualification-not-asserted", levels=("L1", "L2"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("current,target", [("L1", "L2"), ("L2", "L1")])
async def test_level_review_uses_the_target_workzone_boundary(installed, current, target):
    env = await installed(("read",), ("create", "write"), level=current, workzone=True, broad=True)
    try:
        context = env.context
        subject = PermissionSubject(context.instance_id, context.owner_id, context.agent_id,
                                    context.session_id, context.context_generation)
        snapshot = env.policies.config.read_agent_permission_settings("agent")
        requested = replace(snapshot.settings, requested_level=PermissionLevel(target))
        transition = level_transition(env)
        review = transition.review(subject, snapshot, requested)
        path = env.root if target == "L2" else env.root.parent
        assert {Path(s["resource"]) for s in review["preview"]["baseline_scopes"]} == {path}
        assert ("workzone_boundary" in review["preview"]) is (target == "L2")
        assert env.policies.settings("agent").requested_level.value == current
        if target == "L2":
            env.store.set_agent_workzone_slot(
                owner_id="owner", agent_id="agent", slot_id="main", enabled=False, expected_revision=1)
            env.store.set_agent_workzone_slot(
                owner_id="owner", agent_id="agent", slot_id="main", enabled=True, expected_revision=2)
            assert transition.review(subject, snapshot, requested)["revision"] != review["revision"]
    finally:
        await env.executor.close()


@pytest.mark.asyncio
async def test_l2_review_without_workzones_refuses_before_changing_configuration(installed):
    env = await installed(("read",), (), level="L1", workzone=False, broad=True)
    try:
        context = env.context
        subject = PermissionSubject(context.instance_id, context.owner_id, context.agent_id,
                                    context.session_id, context.context_generation)
        snapshot = env.policies.config.read_agent_permission_settings("agent")
        with pytest.raises(PermissionDenied):
            level_transition(env).review(
                subject, snapshot, replace(snapshot.settings, requested_level=PermissionLevel.L2))
        assert env.policies.settings("agent").requested_level is PermissionLevel.L1
    finally:
        await env.executor.close()


@pytest.mark.asyncio
async def test_l2_rejects_redirecting_the_selected_workzone(installed):
    env = await installed(("read",), (), level="L2", workzone=True, broad=True)
    saved = env.root.parent / "original-business"
    other = env.root.parent / "other"
    other.mkdir()
    try:
        os.replace(env.root, saved)
        try:
            os.symlink(other, env.root, target_is_directory=True)
        except OSError:
            pytest.skip("Windows account cannot create disposable test symlinks")
        with pytest.raises(PermissionDenied):
            env.policies("agent", "owner", env.context.session_id)
        with pytest.raises(PermissionDenied):
            env.policies.preview("agent", "owner")
    finally:
        if env.root.is_symlink():
            env.root.unlink()
        if saved.exists():
            os.replace(saved, env.root)
        await env.executor.close()
# HASHI-Fingerprint: hfp_91577caddc5f484d8245c0eff61ed01e
