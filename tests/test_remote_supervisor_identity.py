from __future__ import annotations

import json

from remote.supervisor_identity import (
    normalise_instance_slug,
    resolve_supervisor_identity,
)


def test_supervisor_identity_uses_configured_instance(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": "HASHI1",
                    "display_name": "HASHI One",
                    "workbench_port": 18801,
                    "remote_port": 9999,
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": {"hashi1": {"remote_port": 8766}}}),
        encoding="utf-8",
    )

    identity = resolve_supervisor_identity(tmp_path)

    assert identity.instance_id == "HASHI1"
    assert identity.instance_slug == "hashi1"
    assert identity.systemd_service_name == "hashi-remote-hashi1.service"
    assert identity.windows_task_name == "HashiRemote-hashi1"
    assert identity.windows_restart_task_name == "HashiRestart-hashi1"
    assert identity.source == "agents_json"
    assert identity.remote_port == 8766
    assert identity.display_name == "HASHI One"
    assert identity.workbench_port == 18801


def test_supervisor_identity_explicit_override_is_deterministic(tmp_path):
    identity = resolve_supervisor_identity(tmp_path, instance_id="Lab / East")

    assert identity.instance_id == "Lab / East"
    assert identity.instance_slug == "lab-east"
    assert identity.systemd_service_name == "hashi-remote-lab-east.service"
    assert identity.windows_task_name == "HashiRemote-lab-east"
    assert identity.windows_restart_task_name == "HashiRestart-lab-east"
    assert identity.source == "explicit"
    assert identity.display_name is None
    assert identity.workbench_port is None


def test_supervisor_slug_is_bounded_and_nonempty():
    unicode_slug = normalise_instance_slug("月如")
    long_slug = normalise_instance_slug("HASHI-" + "x" * 100)

    assert unicode_slug.startswith("instance-")
    assert len(unicode_slug) <= 48
    assert len(long_slug) <= 48
    assert long_slug != normalise_instance_slug("HASHI-" + "y" * 100)
