from __future__ import annotations

import json

from remote.live_endpoints import (
    LIVE_ENDPOINTS_PATH_ENV,
    live_endpoints_path,
    read_live_endpoints,
    write_live_endpoint,
)
from remote.peer.base import PeerInfo
from tui.instances import InstanceResolver


def _peer(*, port: int = 18767) -> PeerInfo:
    return PeerInfo(
        instance_id="HASHI-PORTABLE",
        display_name="HASHI Portable",
        host="192.168.0.211",
        port=port,
        workbench_port=18800,
        platform="windows",
    )


def test_live_endpoint_override_keeps_derived_state_off_slow_root(
    tmp_path, monkeypatch
):
    removable_root = tmp_path / "removable"
    host_path = tmp_path / "host-state" / "remote_live_endpoints.json"
    removable_root.mkdir()
    monkeypatch.setenv(LIVE_ENDPOINTS_PATH_ENV, str(host_path))

    write_live_endpoint(removable_root, _peer())

    assert live_endpoints_path(removable_root) == host_path.resolve()
    assert host_path.is_file()
    assert not (removable_root / "state" / "remote_live_endpoints.json").exists()
    assert read_live_endpoints(removable_root)["hashi-portable"]["port"] == 18767


def test_tui_reads_remote_port_from_shared_live_endpoint_override(
    tmp_path, monkeypatch
):
    bridge_home = tmp_path / "bridge"
    bridge_home.mkdir()
    (bridge_home / "agents.json").write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": "HASHI-PORTABLE",
                    "workbench_port": 18800,
                }
            }
        ),
        encoding="utf-8",
    )
    host_path = tmp_path / "host-state" / "remote_live_endpoints.json"
    monkeypatch.setenv(LIVE_ENDPOINTS_PATH_ENV, str(host_path))
    write_live_endpoint(bridge_home, _peer(port=32123))

    resolver = InstanceResolver(bridge_home, ["http://127.0.0.1:18800"])

    assert resolver._remote_ports()[0] == 32123
