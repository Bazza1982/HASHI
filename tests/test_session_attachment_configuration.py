"""Exercise configured attachment limits through fresh Functions and real storage."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("configuration_source", ["environment", "instance_file"])
def test_configured_limits_apply_to_publication_final_reply_and_replay(
    tmp_path, configuration_source
):
    import json

    root = Path(__file__).resolve().parents[1]
    bridge_home = tmp_path / "instance"
    env = {
        **os.environ,
        "BRIDGE_HOME": str(bridge_home),
        "HASHI_SESSION_MAX_ATTACHMENTS_PER_MESSAGE": "24",
        "HASHI_SESSION_MAX_ATTACHMENT_BYTES": "4",
        "HASHI_SESSION_MAX_TOTAL_ATTACHMENT_BYTES": "20",
    }
    if configuration_source == "instance_file":
        # Persisted instance configuration must beat an old inherited process
        # environment, so changing limits never requires a Core cold restart.
        env.update({
            "HASHI_SESSION_MAX_ATTACHMENTS_PER_MESSAGE": "1",
            "HASHI_SESSION_MAX_ATTACHMENT_BYTES": "1",
            "HASHI_SESSION_MAX_TOTAL_ATTACHMENT_BYTES": "1",
        })
        config = bridge_home / "state" / "session_attachment_limits.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "version": 1, "max_attachments_per_message": 24,
            "max_attachment_bytes": 4, "max_total_attachment_bytes_per_message": 20,
        }), encoding="utf-8")
    # A fresh process is the configuration/adoption boundary; never reload a
    # running Functions module to change its limits.
    result = subprocess.run(
        [sys.executable, "-c", r"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tests").resolve()))
from test_frontend_attachment_tool import _registry, _running_session
from orchestrator.session_store import (
    MAX_SESSION_ATTACHMENTS_PER_MESSAGE,
    MAX_SESSION_ATTACHMENT_BYTES,
    MAX_SESSION_ATTACHMENT_TOTAL_BYTES,
)
from tools.schemas import TOOL_SCHEMA_MAP

async def exercise():
    root = Path(sys.argv[1])
    store, owner, session, _ = _running_session(root)
    registry = _registry(
        root, store, owner, session,
        allowed_tools=["frontend_send_attachments", "frontend_publish_deliverable"],
    )
    paths = []
    for i in range(17):
        path = root / f"part-{i:02}.txt"
        path.write_bytes(b"x")
        paths.append({"path": str(path)})
    args = {"publication_id": "complete-result", "attachments": paths}
    early = await registry.execute(
        "frontend_publish_deliverable", args, tool_call_id="early"
    )
    assert not early.is_error, early.output
    replay = await registry.execute(
        "frontend_publish_deliverable", args, tool_call_id="early-replay"
    )
    assert not replay.is_error, replay.output
    assert len(store.run_output_attachment_content("request-frontend-output")) == 17
    assert (
        MAX_SESSION_ATTACHMENTS_PER_MESSAGE,
        MAX_SESSION_ATTACHMENT_BYTES,
        MAX_SESSION_ATTACHMENT_TOTAL_BYTES,
    ) == (24, 4, 20)
    for name in ("frontend_send_attachments", "frontend_publish_deliverable"):
        schema = TOOL_SCHEMA_MAP[name]["function"]["parameters"]
        assert schema["properties"]["attachments"]["maxItems"] == 24

    # The configured byte budget is shared by incremental and final results.
    final = root / "final.txt"
    final.write_bytes(b"end")
    accepted = await registry.execute(
        "frontend_send_attachments", {"attachments": [{"path": str(final)}]},
        tool_call_id="final",
    )
    assert not accepted.is_error, accepted.output
    too_large = root / "oversized.txt"
    too_large.write_bytes(b"12345")
    rejected = await registry.execute(
        "frontend_send_attachments", {"attachments": [{"path": str(too_large)}]},
        tool_call_id="oversized",
    )
    assert rejected.is_error
    rejected = await registry.execute(
        "frontend_send_attachments", {"attachments": paths[:1]},
        tool_call_id="aggregate-overflow",
    )
    assert rejected.is_error
    rejected = await registry.execute(
        "frontend_send_attachments", {"attachments": paths + paths[:8]},
        tool_call_id="count-overflow",
    )
    assert rejected.is_error
    parts = store.run_output_attachment_content("request-frontend-output")
    assert [p["filename"] for p in parts] == [
        *(f"part-{i:02}.txt" for i in range(17)), "final.txt"
    ]
    print(json.dumps({"attachments": len(parts), "bytes": sum(p["size_bytes"] for p in parts)}))

asyncio.run(exercise())
""", str(tmp_path)],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"attachments": 18' in result.stdout
    if configuration_source == "instance_file":
        configured = json.loads(config.read_text(encoding="utf-8"))
        configured["max_attachments_per_message"] = 32
        config.write_text(json.dumps(configured), encoding="utf-8")
        adopted = subprocess.run(
            [sys.executable, "-c",
             "from orchestrator.session_store import MAX_SESSION_ATTACHMENTS_PER_MESSAGE; "
             "from tools.schemas import TOOL_SCHEMA_MAP; "
             "assert MAX_SESSION_ATTACHMENTS_PER_MESSAGE == 32; "
             "assert TOOL_SCHEMA_MAP['frontend_send_attachments']['function']"
             "['parameters']['properties']['attachments']['maxItems'] == 32"],
            cwd=root, env=env, text=True, capture_output=True, timeout=30,
        )
        assert adopted.returncode == 0, adopted.stdout + adopted.stderr



@pytest.mark.parametrize("overrides", [
    {"HASHI_SESSION_MAX_ATTACHMENTS_PER_MESSAGE": "0"},
    {"HASHI_SESSION_MAX_ATTACHMENTS_PER_MESSAGE": "1025"},
    {"HASHI_SESSION_MAX_ATTACHMENT_BYTES": "-1"},
    {"HASHI_SESSION_MAX_TOTAL_ATTACHMENT_BYTES": "unlimited"},
    {"HASHI_SESSION_MAX_TOTAL_ATTACHMENT_BYTES": "1073741825"},
])
def test_invalid_attachment_configuration_fails_instead_of_ignoring_override(overrides):
    from orchestrator.session_attachment_limits import load_session_attachment_limits

    with pytest.raises(ValueError, match="HASHI_SESSION_"):
        load_session_attachment_limits(overrides)



@pytest.mark.parametrize("document", [
    {"version": True},
    {"version": 2},
    {"version": 1, "max_files": 24},
    {"version": 1, "max_attachment_bytes": None},
])
def test_invalid_instance_attachment_document_is_not_treated_as_defaults(tmp_path, document):
    import json

    from orchestrator.session_attachment_limits import load_session_attachment_limits

    target = tmp_path / "limits.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_session_attachment_limits({}, config_path=target)

@pytest.mark.parametrize("session_count", [2, 24])
def test_attachment_schemas_match_each_frozen_route_and_enforced_count(tmp_path, session_count):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-B", "-c", r"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path("tests").resolve()))
from test_frontend_attachment_tool import _registry, _running_session
from tools.schemas import TOOL_SCHEMA_MAP
from orchestrator.hchat_attachment_contract import HCHAT_MAX_ATTACHMENTS_PER_MESSAGE

async def exercise():
    root, session_count = Path(sys.argv[1]), int(sys.argv[2])
    tools = ("frontend_send_attachments", "frontend_publish_deliverable")
    for source, count in (("session-api", session_count),
                          ("bridge:hchat-draft", HCHAT_MAX_ATTACHMENTS_PER_MESSAGE)):
        for name in tools:
            directory = root / source.replace(":", "-") / name
            directory.mkdir(parents=True)
            store, owner, session, _ = _running_session(directory)
            registry = _registry(directory, store, owner, session,
                                 request_source=source, allowed_tools=list(tools))
            definitions = {item["function"]["name"]: item["function"]
                           for item in registry.get_tool_definitions()}
            assert definitions[name]["parameters"]["properties"]["attachments"]["maxItems"] == count
            assert TOOL_SCHEMA_MAP[name]["function"]["parameters"]["properties"]["attachments"]["maxItems"] == session_count
            args = {"attachments": [{"path": "never-read"}] * (count + 1)}
            if name == "frontend_publish_deliverable":
                args["publication_id"] = "route-boundary"
            overflow = await registry.execute(name, args, tool_call_id="overflow")
            assert overflow.is_error and "count" in overflow.output, overflow.output
            # HChat intentionally makes the first selection outcome terminal.
            # Exercise a successful selection in a fresh invocation registry.
            registry = _registry(directory, store, owner, session,
                                 request_source=source, allowed_tools=list(tools))
            paths = []
            for i in range(count):
                path = directory / f"part-{i}.txt"
                path.write_bytes(b"x")
                paths.append({"path": str(path)})
            args["attachments"] = paths
            accepted = await registry.execute(name, args, tool_call_id="boundary")
            assert not accepted.is_error, accepted.output
            if name == "frontend_publish_deliverable":
                replay = await registry.execute(name, args, tool_call_id="replay")
                assert not replay.is_error, replay.output
            assert len(store.run_output_attachment_content("request-frontend-output")) == count
            # Projecting an HChat definition must never mutate shared Session schemas.
            assert TOOL_SCHEMA_MAP[name]["function"]["parameters"]["properties"]["attachments"]["maxItems"] == session_count
asyncio.run(exercise())
""", str(tmp_path), str(session_count)],
        cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                       "BRIDGE_HOME": str(tmp_path / "instance"),
                       "HASHI_SESSION_MAX_ATTACHMENTS_PER_MESSAGE": str(session_count),
                       "HASHI_SESSION_MAX_ATTACHMENT_BYTES": "4",
                       "HASHI_SESSION_MAX_TOTAL_ATTACHMENT_BYTES": "64"},
        text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
# HASHI-Fingerprint: hfp_79b8319d2b9043b981c3335f0560d9d1
