"""A running once capability remains valid only for its exact claim and deadline."""
import pytest

from orchestrator.permissions.authority import PermissionDenied
from tests.test_permissions_guarded_dispatch import prepared

from tests.test_permissions_guarded_dispatch import environment as environment


@pytest.mark.parametrize("change", ["expiry", "revoke"])
def test_consumed_once_is_checked_without_requiring_another_lease(environment, change):
    authority, _, clock, context, scope, lease, claim = prepared(environment)
    authority.start(context, claim.claim_id)
    assert authority.get_lease(lease.lease_id).status == "consumed"
    arguments = dict(operation_id="operation", arguments_digest="a" * 64, scopes=[scope])
    authority.check_running_claim(context, claim.claim_id, **arguments)
    with pytest.raises(PermissionDenied):
        authority.check_running_claim(context, claim.claim_id,
                                       **{**arguments, "arguments_digest": "b" * 64})
    if change == "expiry":
        clock.value = lease.expires_at
    else:
        authority.revoke(context, lease.lease_id)
    with pytest.raises(PermissionDenied):
        authority.check_running_claim(context, claim.claim_id, **arguments)
