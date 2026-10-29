"""Configured baseline grants never masquerade as human leases."""
from dataclasses import replace

import pytest

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied
from orchestrator.permissions.models import normalize_scope
from tests.test_permission_pao_fence import setup_run
from tests.test_permission_runtime_context import NoApprovals


def test_baseline_dispatch_and_removal_between_prepare_and_start(tmp_path):
    _, _, _, _, context = setup_run(tmp_path)
    own = normalize_scope("file", str(tmp_path), ("read", "list"), cwd=tmp_path)
    baseline = [own]

    def policy(c):
        if c.owner_id != context.owner_id or c.policy_revision != context.policy_revision:
            raise PermissionDenied("wrong policy")
        return tuple(baseline)

    authority = PermissionAuthority(tmp_path / "permissions.db", verifier=NoApprovals(),
                                    baseline_resolver=policy)
    authority.open_task(context)
    target = normalize_scope("file", str(tmp_path / "report"), ("read",), cwd=tmp_path)
    assert authority.evaluate(context, [target])
    claim = authority.prepare(context, "read", "a" * 64, [target])
    assert authority.dispatch_guarded(context, claim.claim_id, operation_id="read",
                                      arguments_digest="a" * 64, scopes=[target], effect=lambda: b"ok") == b"ok"
    with authority._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM permission_leases").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0
    authority.finish(context, claim.claim_id, outcome="succeeded", result_digest="b" * 64)
    other = authority.prepare(context, "second", "c" * 64, [target])
    baseline.clear()
    with pytest.raises(PermissionDenied):
        authority.start(context, other.claim_id)
    assert not authority.evaluate(context, [target])
    with pytest.raises(PermissionDenied):
        authority.evaluate(replace(context, policy_revision=context.policy_revision + 1), [target])
