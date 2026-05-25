from remote.routing import build_route_candidates, same_machine_hint, validate_same_host_port_conflicts, wsl_unc_anchor


def test_same_host_wsl_siblings_keep_loopback_as_fallback_route():
    local = {
        "instance_id": "HASHI1",
        "platform": "wsl",
        "host_identity": "a9max",
        "wsl_root_from_windows": r"\\wsl$\\Ubuntu-22.04\\home\\lily\\projects\\hashi",
    }
    target = {
        "instance_id": "HASHI2",
        "platform": "wsl",
        "host_identity": "a9max",
        "remote_port": 8767,
        "lan_ip": "192.168.0.211",
        "wsl_root_from_windows": r"\\wsl$\\Ubuntu-22.04\\home\\lily\\projects\\hashi2",
    }

    same_host = same_machine_hint(local_entry=local, target_entry=target)
    candidates = build_route_candidates(target_entry=target, remote_port=8767, same_host=same_host)

    assert same_host is True
    assert candidates[0].host == "192.168.0.211"
    assert candidates[0].scope == "lan"
    assert [candidate.host for candidate in candidates] == ["192.168.0.211", "127.0.0.1"]


def test_cross_host_windows_routes_over_lan_not_loopback():
    target = {
        "instance_id": "INTEL",
        "platform": "windows",
        "remote_port": 8766,
        "lan_ip": "192.168.0.50",
        "api_host": "127.0.0.1",
    }

    candidates = build_route_candidates(target_entry=target, remote_port=8766, same_host=False)

    assert [candidate.host for candidate in candidates] == ["192.168.0.50"]


def test_wsl_unc_anchor_supports_wsl_localhost_form():
    assert (
        wsl_unc_anchor(r"\\wsl.localhost\\Ubuntu-22.04\\home\\lily\\projects\\hashi")
        == "\\\\wsl.localhost\\ubuntu-22.04\\"
    )


def test_same_host_port_conflict_is_actionable():
    conflicts = validate_same_host_port_conflicts(
        {
            "hashi1": {
                "instance_id": "HASHI1",
                "platform": "wsl",
                "host_identity": "a9max",
                "remote_port": 8766,
            },
            "hashi2": {
                "instance_id": "HASHI2",
                "platform": "wsl",
                "host_identity": "a9max",
                "remote_port": 8766,
            },
            "intel": {
                "instance_id": "INTEL",
                "platform": "windows",
                "host_identity": "intel",
                "remote_port": 8766,
            },
        }
    )

    assert conflicts == [
        {
            "level": "error",
            "type": "same_host_remote_port_conflict",
            "instances": ["HASHI1", "HASHI2"],
            "remote_port": 8766,
            "message": "same-host instances share Remote port 8766",
        }
    ]


def test_same_host_port_conflict_ignores_inactive_offline_entries():
    conflicts = validate_same_host_port_conflicts(
        {
            "hashi1": {
                "instance_id": "HASHI1",
                "platform": "wsl",
                "host_identity": "a9max",
                "remote_port": 8766,
            },
            "hashi9": {
                "instance_id": "HASHI9",
                "platform": "windows",
                "host_identity": "a9max",
                "remote_port": 8766,
                "active": False,
                "live_status": "offline",
                "handshake_state": "handshake_timed_out",
            },
        }
    )

    assert conflicts == []
