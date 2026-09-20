from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.reboot_adoption_bridge import bridge_legacy_broad_reboot


GENERATION = "sha256:" + "a" * 64
OLD_GENERATION = "sha256:" + "0" * 64
OPERATION_ID = "1" * 32


def _write_legacy_receipt(
    bridge_home: Path,
    *,
    mode: str = "max",
    status: str = "succeeded",
    shared_replacement: dict | None = None,
) -> None:
    state_dir = bridge_home / "state" / "instance"
    state_dir.mkdir(parents=True)
    record = {
        "id": OPERATION_ID,
        "mode": mode,
        "status": status,
        "phase": "finished",
        "committed": True,
        "targets": ["alpha", "beta"],
        "created_at": 990.0,
        "updated_at": 1001.0,
        "online": {"alpha": True, "beta": True},
        "workers": {
            "alpha": {
                "new_pid": 201,
                "generation_id": GENERATION,
                "online": True,
            },
            "beta": {
                "new_pid": 202,
                "generation_id": GENERATION,
                "online": True,
            },
        },
        "generations": {"alpha": GENERATION, "beta": GENERATION},
    }
    if shared_replacement is not None:
        record["shared_replacement"] = shared_replacement
    (state_dir / "reboot-receipts.json").write_text(
        json.dumps({"schema": 2, "records": [record]}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_legacy_broad_worker_publishes_one_core_handoff(tmp_path):
    _write_legacy_receipt(tmp_path)

    result = await bridge_legacy_broad_reboot(
        tmp_path,
        agent_name="alpha",
        worker_pid=201,
        generation_id=GENERATION,
        shared_pid=101,
        shared_generation_id=OLD_GENERATION,
        worker_started_at=1000.0,
        now=lambda: 1002.0,
        poll_interval=0,
        timeout=0,
    )

    assert result is not None
    marker = json.loads(
        (
            tmp_path
            / "state"
            / "instance"
            / "legacy-reboot-handoffs"
            / f"{OPERATION_ID}.json"
        ).read_text(encoding="utf-8")
    )
    assert marker == {
        "schema": 1,
        "operation_id": OPERATION_ID,
        "requested_at": 1002.0,
        "old_shared_pid": 101,
        "old_shared_generation_id": OLD_GENERATION,
        "expected_generation_id": GENERATION,
        "leader_agent": "alpha",
        "worker_pid": 201,
    }
    request = json.loads(
        (
            tmp_path
            / "state"
            / "instance"
            / "kernel-requests"
            / f"{OPERATION_ID}.json"
        ).read_text(encoding="utf-8")
    )
    assert request == {"id": OPERATION_ID}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_name", "mode", "status", "shared_replacement", "shared_generation"),
    [
        ("beta", "max", "succeeded", None, OLD_GENERATION),
        ("alpha", "min", "succeeded", None, OLD_GENERATION),
        ("alpha", "max", "running", None, OLD_GENERATION),
        (
            "alpha",
            "max",
            "succeeded",
            {"status": "requested", "request_id": OPERATION_ID},
            OLD_GENERATION,
        ),
        ("alpha", "max", "succeeded", None, GENERATION),
    ],
)
async def test_legacy_bridge_rejects_nonlegacy_or_nonleader_requests(
    tmp_path,
    agent_name,
    mode,
    status,
    shared_replacement,
    shared_generation,
):
    _write_legacy_receipt(
        tmp_path,
        mode=mode,
        status=status,
        shared_replacement=shared_replacement,
    )

    result = await bridge_legacy_broad_reboot(
        tmp_path,
        agent_name=agent_name,
        worker_pid=201 if agent_name == "alpha" else 202,
        generation_id=GENERATION,
        shared_pid=101,
        shared_generation_id=shared_generation,
        worker_started_at=1000.0,
        now=lambda: 1002.0,
        poll_interval=0,
        timeout=0,
    )

    assert result is None
    assert not (tmp_path / "state" / "instance" / "kernel-requests").exists()
