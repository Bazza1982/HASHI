from __future__ import annotations

import pytest

from orchestrator.windows_service_acl import (
    _validated_service_name,
    insert_windows_service_restart_ace,
    remove_windows_service_restart_ace,
    windows_service_has_restart_access,
    windows_service_restart_ace,
)


SID = "S-1-5-21-1000"


def test_restart_ace_grants_only_start_and_stop():
    assert windows_service_restart_ace(SID) == "(A;;RPWP;;;S-1-5-21-1000)"


def test_restart_ace_is_inserted_before_sacl_and_is_idempotent():
    original = "D:(A;;CCLCSWLOCRRC;;;IU)S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)"

    updated = insert_windows_service_restart_ace(original, SID)

    assert updated == (
        "D:(A;;CCLCSWLOCRRC;;;IU)(A;;RPWP;;;S-1-5-21-1000)"
        "S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)"
    )
    assert insert_windows_service_restart_ace(updated, SID) == updated
    assert windows_service_has_restart_access(updated, SID)


def test_restart_ace_restore_removes_only_its_exact_entry():
    original = "D:(A;;CCLCSWLOCRRC;;;IU)(A;;RPWP;;;S-1-5-21-1000)(A;;FA;;;SY)"

    restored = remove_windows_service_restart_ace(original, SID)

    assert restored == "D:(A;;CCLCSWLOCRRC;;;IU)(A;;FA;;;SY)"
    assert not windows_service_has_restart_access(restored, SID)


@pytest.mark.parametrize("value", ["", "HASHI 3", "../HASHI3", "HASHI3;"])
def test_invalid_service_name_is_rejected(value):
    with pytest.raises(ValueError, match="service name"):
        _validated_service_name(value)
