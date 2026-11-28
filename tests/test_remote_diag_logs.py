"""Tests for the default-off, read-only LAN diagnostics log gate."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from remote.api.diag_logs import (
    DEFAULT_TTL_SECONDS,
    LOG_ROOT_REMOTE,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    DiagLogError,
    DiagLogRevoked,
    DiagLogsGate,
    LogFileSet,
    build_diag_log_roots,
    classify_client_host,
    compute_start_offset,
    iter_log_bytes,
    list_log_files,
    normalise_relative_path,
    read_log_slice,
    resolve_effective_logs_dir,
    resolve_log_file,
)
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.terminal.executor import TerminalExecutor

LOOPBACK = ("127.0.0.1", 51234)
LAN = ("192.168.0.57", 51234)
PUBLIC = ("203.0.113.9", 51234)

BIG_LINE_COUNT = 4000


@pytest.fixture()
def instance_root(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    (logs / "verifier").mkdir(parents=True)
    (logs / "bridge.log").write_bytes(b"line-1\nline-2\nline-3\n")
    (logs / "verifier" / "her_v2_audit.jsonl").write_bytes(b'{"event":"a"}\n{"event":"b"}\n')
    big = log_root_big_payload()
    (logs / "hashi-remote-supervisor.log").write_bytes(big)
    (tmp_path / "secrets.json").write_bytes(b'{"hashi_remote_shared_token":"secret"}')
    return tmp_path


def log_root_big_payload() -> bytes:
    rows = [f"2026-11-28T20:00:{index % 60:02d} line {index} padding-padding\n" for index in range(BIG_LINE_COUNT)]
    return "".join(rows).encode("utf-8")


# ---------------------------------------------------------------------------
# Client scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", "loopback"),
        ("127.5.6.7", "loopback"),
        ("::1", "loopback"),
        ("::ffff:127.0.0.1", "loopback"),
        ("[::1]", "loopback"),
        ("192.168.0.211", "lan"),
        ("10.0.0.7", "lan"),
        ("172.16.5.4", "lan"),
        ("172.31.255.254", "lan"),
        ("172.32.0.1", "public"),
        ("8.8.8.8", "public"),
        ("100.64.0.5", "public"),
        ("169.254.10.10", "public"),
        ("203.0.113.1", "public"),
        ("", "unknown"),
        ("not-an-ip", "unknown"),
    ],
)
def test_client_scope_classification(host: str, expected: str) -> None:
    assert classify_client_host(host) == expected


# ---------------------------------------------------------------------------
# Gate state machine
# ---------------------------------------------------------------------------


def test_gate_starts_closed_and_opens_for_sixty_minutes_by_default(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    gate = DiagLogsGate(tmp_path / "logs", instance_id="HASHI8", now=lambda: now)
    assert gate.is_open() is False
    assert gate.status()["enabled"] is False
    assert gate.status()["remaining_seconds"] is None

    state = gate.open()
    assert state["enabled"] is True
    assert state["ttl_seconds"] == 3600
    assert state["remaining_seconds"] == 3600
    assert DEFAULT_TTL_SECONDS == 3600


def test_gate_ttl_is_clamped_to_sane_bounds(tmp_path: Path) -> None:
    gate = DiagLogsGate(tmp_path / "logs", now=lambda: 0.0)
    assert gate.open(ttl_seconds=1)["ttl_seconds"] == MIN_TTL_SECONDS
    assert gate.open(ttl_seconds=10**9)["ttl_seconds"] == MAX_TTL_SECONDS


def test_gate_expires_itself_after_the_ttl(tmp_path: Path) -> None:
    clock = {"now": 1_800_000_000.0}
    gate = DiagLogsGate(tmp_path / "logs", now=lambda: clock["now"])
    gate.open(ttl_seconds=60)
    clock["now"] += 59
    assert gate.is_open() is True
    clock["now"] += 2
    assert gate.is_open() is False
    status = gate.status()
    assert status["enabled"] is False
    assert status["last_close_reason"] == "ttl_expired"
    assert status["expires_at"] is None


def test_gate_close_records_manual_reason(tmp_path: Path) -> None:
    gate = DiagLogsGate(tmp_path / "logs", now=lambda: 5.0)
    gate.open(ttl_seconds=120)
    state = gate.close()
    assert state["enabled"] is False
    assert state["last_close_reason"] == "manual"
    assert state["remaining_seconds"] is None


# ---------------------------------------------------------------------------
# Path rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "c:\\windows\\win.ini",
        "../secrets.json",
        "verifier/../../secrets.json",
        "..\\secrets.json",
        "a/\x00b",
    ],
)
def test_relative_path_normalisation_rejects_escapes(raw: str) -> None:
    with pytest.raises(DiagLogError):
        normalise_relative_path(raw)


def test_relative_path_normalisation_accepts_nested_logs() -> None:
    assert normalise_relative_path("verifier/events.log") == "verifier/events.log"
    assert normalise_relative_path("verifier\\events.log") == "verifier/events.log"
    assert normalise_relative_path("./bridge.log") == "bridge.log"


def test_resolve_returns_file_inside_log_root(instance_root: Path) -> None:
    resolved = resolve_log_file(instance_root / "logs", "verifier/her_v2_audit.jsonl")
    assert resolved.name == "her_v2_audit.jsonl"


def test_resolve_rejects_missing_file(instance_root: Path) -> None:
    with pytest.raises(DiagLogError) as excinfo:
        resolve_log_file(instance_root / "logs", "nope.log")
    assert excinfo.value.status_code == 404


def test_resolve_rejects_directory(instance_root: Path) -> None:
    with pytest.raises(DiagLogError) as excinfo:
        resolve_log_file(instance_root / "logs", "verifier")
    assert excinfo.value.status_code == 404


@pytest.mark.skipif(os.name != "nt", reason="junction escape check is Windows specific")
def test_resolve_rejects_junction_that_escapes_the_log_root(instance_root: Path) -> None:
    link = instance_root / "logs" / "outside"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(instance_root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create junction: {result.stderr.strip()}")
    try:
        with pytest.raises(DiagLogError) as excinfo:
            resolve_log_file(instance_root / "logs", "outside/secrets.json")
        assert excinfo.value.reason == "outside_log_root"
        assert excinfo.value.status_code == 403
    finally:
        os.rmdir(link)


# ---------------------------------------------------------------------------
# Ranged reads
# ---------------------------------------------------------------------------


def test_compute_start_offset_rules() -> None:
    assert compute_start_offset(100) == 0
    assert compute_start_offset(100, offset=40) == 40
    assert compute_start_offset(100, offset=1000) == 100
    assert compute_start_offset(100, tail=30) == 70
    assert compute_start_offset(20, tail=500) == 0
    with pytest.raises(DiagLogError):
        compute_start_offset(100, offset=1, tail=1)
    with pytest.raises(DiagLogError):
        compute_start_offset(100, offset=-1)
    with pytest.raises(DiagLogError):
        compute_start_offset(100, tail=0)


def test_full_read_returns_every_byte(instance_root: Path) -> None:
    path = instance_root / "logs" / "hashi-remote-supervisor.log"
    start, payload = read_log_slice(path)
    assert start == 0
    assert payload == path.read_bytes()
    assert len(payload) > 100_000


def test_offset_tail_lines_and_max_bytes(instance_root: Path) -> None:
    path = instance_root / "logs" / "bridge.log"
    full = path.read_bytes()

    start, payload = read_log_slice(path, offset=7)
    assert start == 7
    assert payload == full[7:]

    start, payload = read_log_slice(path, tail=7)
    assert start == len(full) - 7
    assert payload == full[-7:]

    _, payload = read_log_slice(path, lines=2)
    assert payload == b"line-1\nline-2\n"

    _, payload = read_log_slice(path, max_bytes=4)
    assert payload == b"line"

    _, payload = read_log_slice(path, offset=7, lines=2)
    assert payload == b"line-2\nline-3\n"


def test_streaming_is_chunked_and_lossless(instance_root: Path) -> None:
    path = instance_root / "logs" / "hashi-remote-supervisor.log"
    chunks = list(iter_log_bytes(path, chunk_size=1024))
    assert len(chunks) > 10
    assert b"".join(chunks) == path.read_bytes()


def test_listing_reports_every_log_file(instance_root: Path) -> None:
    listing = list_log_files(instance_root / "logs")
    paths = {row["path"] for row in listing["files"]}
    assert paths == {
        "bridge.log",
        "verifier/her_v2_audit.jsonl",
        "hashi-remote-supervisor.log",
    }
    assert listing["file_count"] == 3
    assert listing["total_bytes"] == sum(row["size"] for row in listing["files"])
    assert listing["truncated"] is False


def test_listing_honours_max_files(instance_root: Path) -> None:
    listing = list_log_files(instance_root / "logs", max_files=2)
    assert listing["truncated"] is True
    assert listing["file_count"] == 2


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def build_client(instance_root: Path, gate: DiagLogsGate, peer=LOOPBACK) -> TestClient:
    app = create_app(
        {"instance_id": "HASHI8", "display_name": "Diag", "remote_port": 8775},
        PairingManager(storage_dir=instance_root / "pairing", lan_mode=True),
        TerminalExecutor(),
        hashi_root=str(instance_root),
        diag_logs_gate=gate,
    )
    return TestClient(app, client=peer)


def new_gate(instance_root: Path, *, clock=None) -> DiagLogsGate:
    now = clock if clock is not None else (lambda: time.time())
    return DiagLogsGate(instance_root / "logs", instance_id="HASHI8", now=now)


def test_gate_is_closed_by_default_for_everyone(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    for peer in (LAN, LOOPBACK):
        client = build_client(instance_root, gate, peer)
        assert client.get("/diag/logs").status_code == 404
        assert client.get("/diag/logs/bridge.log").status_code == 404


def test_loopback_can_read_gate_status_while_lan_cannot(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    loopback = build_client(instance_root, gate, LOOPBACK)
    lan = build_client(instance_root, gate, LAN)

    assert loopback.get("/diag/gate").status_code == 200
    assert lan.get("/diag/gate").status_code == 404


def test_only_loopback_can_open_and_close_the_gate(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    lan = build_client(instance_root, gate, LAN)
    public = build_client(instance_root, gate, PUBLIC)
    loopback = build_client(instance_root, gate, LOOPBACK)

    assert lan.post("/diag/gate", json={"enabled": True}).status_code == 403
    assert public.post("/diag/gate", json={"enabled": True}).status_code == 403
    assert gate.is_open() is False

    opened = loopback.post("/diag/gate", json={"enabled": True})
    assert opened.status_code == 200
    assert opened.json()["enabled"] is True
    assert opened.json()["ttl_seconds"] == 3600
    assert gate.is_open() is True

    closed = loopback.post("/diag/gate", json={"enabled": False})
    assert closed.status_code == 200
    assert gate.is_open() is False


def test_lan_fully_reads_logs_once_open(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    gate.open()
    client = build_client(instance_root, gate, LAN)

    listing = client.get("/diag/logs")
    assert listing.status_code == 200
    body = listing.json()
    assert body["instance_id"] == "HASHI8"
    assert body["file_count"] == 3

    big = instance_root / "logs" / "hashi-remote-supervisor.log"
    response = client.get("/diag/logs/hashi-remote-supervisor.log")
    assert response.status_code == 200
    assert response.content == big.read_bytes()
    assert response.headers["x-diag-log-size"] == str(big.stat().st_size)
    assert response.headers["x-diag-log-start"] == "0"
    assert response.headers["cache-control"] == "no-store"


def test_public_client_is_refused_even_when_open(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    gate.open()
    client = build_client(instance_root, gate, PUBLIC)
    response = client.get("/diag/logs")
    assert response.status_code == 403
    assert response.json()["error"] == "lan_required"
    assert client.get("/diag/logs/bridge.log").status_code == 403


def test_lan_reads_ranges_and_line_limits(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    gate.open()
    client = build_client(instance_root, gate, LAN)

    tailed = client.get("/diag/logs/bridge.log", params={"tail": 7})
    assert tailed.content == b"line-3\n"

    offset = client.get("/diag/logs/bridge.log", params={"offset": 7})
    assert offset.content == b"line-2\nline-3\n"
    assert offset.headers["x-diag-log-start"] == "7"

    two_lines = client.get("/diag/logs/bridge.log", params={"lines": 2})
    assert two_lines.content == b"line-1\nline-2\n"


def test_invalid_ranges_are_rejected(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    gate.open()
    client = build_client(instance_root, gate, LAN)

    assert client.get("/diag/logs/bridge.log", params={"offset": -1}).status_code == 400
    assert client.get("/diag/logs/bridge.log", params={"tail": 0}).status_code == 400
    assert client.get("/diag/logs/bridge.log", params={"lines": 0}).status_code == 400
    assert (
        client.get("/diag/logs/bridge.log", params={"offset": 1, "tail": 2}).status_code == 400
    )


def test_traversal_and_secrets_are_refused(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    gate.open()
    client = build_client(instance_root, gate, LAN)

    for probe in (
        "/diag/logs/%2e%2e%2fsecrets.json",
        "/diag/logs/verifier/%2e%2e%2f%2e%2e%2fsecrets.json",
        "/diag/logs/C:/Windows/win.ini",
    ):
        response = client.get(probe)
        assert response.status_code in {400, 403, 404}, probe
        assert b"hashi_remote_shared_token" not in response.content


def test_expired_gate_stops_serving(instance_root: Path) -> None:
    clock = {"now": 1_800_000_000.0}
    gate = new_gate(instance_root, clock=lambda: clock["now"])
    gate.open(ttl_seconds=MIN_TTL_SECONDS)
    client = build_client(instance_root, gate, LAN)
    assert client.get("/diag/logs").status_code == 200

    clock["now"] += MIN_TTL_SECONDS + 1
    assert client.get("/diag/logs").status_code == 404
    assert client.get("/diag/logs/bridge.log").status_code == 404


def test_head_returns_metadata_without_body(instance_root: Path) -> None:
    gate = new_gate(instance_root)
    gate.open()
    client = build_client(instance_root, gate, LAN)
    response = client.head("/diag/logs/bridge.log")
    assert response.status_code == 200
    assert response.headers["x-diag-log-path"] == "bridge.log"


def test_gate_state_is_exposed_for_ui_hints(instance_root: Path) -> None:
    clock = {"now": 1_800_000_000.0}
    gate = new_gate(instance_root, clock=lambda: clock["now"])
    client = build_client(instance_root, gate, LOOPBACK)
    gate.open(ttl_seconds=600)
    body = client.get("/diag/gate").json()
    assert body["enabled"] is True
    assert body["remaining_seconds"] == 600
    assert body["log_root_exists"] is True


# ---------------------------------------------------------------------------
# Effective log directory: instances that move global.base_logs_dir
# ---------------------------------------------------------------------------


def write_agents_json(hashi_root: Path, base_logs_dir) -> None:
    payload = {"global": {"authorized_id": 1, "instance_id": "HASHI8"}}
    if base_logs_dir is not None:
        payload["global"]["base_logs_dir"] = base_logs_dir
    (hashi_root / "agents.json").write_text(json.dumps(payload), encoding="utf-8")


def test_effective_logs_dir_defaults_to_logs_when_unconfigured(tmp_path: Path) -> None:
    write_agents_json(tmp_path, None)
    assert resolve_effective_logs_dir(tmp_path) == (tmp_path / "logs").resolve()


def test_effective_logs_dir_follows_configured_absolute_path(tmp_path: Path) -> None:
    custom = tmp_path / "moved-logs"
    write_agents_json(tmp_path, str(custom))
    assert resolve_effective_logs_dir(tmp_path) == custom.resolve()


def test_effective_logs_dir_supports_home_prefix_and_relative_values(tmp_path: Path) -> None:
    write_agents_json(tmp_path, "@home/alt-logs")
    assert resolve_effective_logs_dir(tmp_path) == (tmp_path / "alt-logs").resolve()

    write_agents_json(tmp_path, "nested/rel-logs")
    assert resolve_effective_logs_dir(tmp_path) == (tmp_path / "nested" / "rel-logs").resolve()


def test_effective_logs_dir_survives_missing_or_broken_config(tmp_path: Path) -> None:
    assert resolve_effective_logs_dir(tmp_path) == (tmp_path / "logs").resolve()
    (tmp_path / "agents.json").write_text("{not json", encoding="utf-8")
    assert resolve_effective_logs_dir(tmp_path) == (tmp_path / "logs").resolve()


def test_diag_log_roots_add_the_remote_file_only_when_it_lives_elsewhere(tmp_path: Path) -> None:
    moved = tmp_path / "moved-logs"
    write_agents_json(tmp_path, str(moved))

    assert list(build_diag_log_roots(tmp_path)) == ["instance"]
    # Inside the instance log tree: the instance root already lists it.
    assert list(build_diag_log_roots(tmp_path, remote_log_file=moved / "remote.log")) == ["instance"]

    separate = tmp_path / "remote-logs"
    separate.mkdir()
    roots = build_diag_log_roots(tmp_path, remote_log_file=separate / "hashi-remote.log")
    assert roots["instance"] == moved.resolve()
    remote = roots[LOG_ROOT_REMOTE]
    assert isinstance(remote, LogFileSet)
    assert remote.directory == separate.resolve()
    assert remote.names == ("hashi-remote.log",)


def test_gate_serves_the_effective_logs_dir_and_the_remote_dir(tmp_path: Path) -> None:
    instance_logs = tmp_path / "moved-logs"
    (instance_logs / "agent").mkdir(parents=True)
    (instance_logs / "agent" / "her_v2_audit.jsonl").write_bytes(b'{"a":1}\n')
    (instance_logs / "bridge.log").write_bytes(b"bridge line\n")
    remote_logs = tmp_path / "remote-logs"
    remote_logs.mkdir()
    (remote_logs / "hashi-remote.log").write_bytes(b"remote line\n")

    gate = DiagLogsGate(
        log_roots={"instance": instance_logs, "remote": remote_logs},
        instance_id="HASHI8",
    )
    client = build_client(tmp_path, gate, LOOPBACK)
    assert client.post("/diag/gate", json={"enabled": True}).status_code == 200

    listing = client.get("/diag/logs").json()
    served = {(entry["root"], entry["path"]) for entry in listing["files"]}
    assert ("instance", "agent/her_v2_audit.jsonl") in served
    assert ("instance", "bridge.log") in served
    assert ("remote", "hashi-remote.log") in served
    assert set(listing["log_roots"]) == {"instance", "remote"}

    remote_read = client.get("/diag/logs/remote/hashi-remote.log")
    assert remote_read.status_code == 200
    assert remote_read.content == b"remote line\n"
    assert remote_read.headers["x-diag-log-root"] == "remote"

    # The original unprefixed URL keeps resolving against the instance root.
    legacy = client.get("/diag/logs/agent/her_v2_audit.jsonl")
    assert legacy.status_code == 200
    assert legacy.content == b'{"a":1}\n'
    assert legacy.headers["x-diag-log-root"] == "instance"

    # Boundaries are enforced per root, not just once for the whole gate.
    # Percent-encoded so the HTTP client cannot pre-resolve the `..` itself.
    traversal = client.get("/diag/logs/remote/%2e%2e%2fsecrets.json")
    assert traversal.status_code == 400
    assert traversal.json()["error"] == "parent_traversal"
    assert client.get("/diag/logs/instance/%2e%2e%2f%2e%2e%2fsecrets.json").status_code == 400


# ---------------------------------------------------------------------------
# Revoking a download that is already running
# ---------------------------------------------------------------------------


def test_in_flight_download_is_revoked_when_the_switch_closes(tmp_path: Path) -> None:
    gate = DiagLogsGate(log_roots={"instance": tmp_path})
    payload = tmp_path / "big.log"
    payload.write_bytes(b"y" * (256 * 1024))
    gate.open()

    chunks = iter_log_bytes(payload, chunk_size=4096, keep_alive=gate.is_open)
    assert len(next(chunks)) == 4096
    gate.close()
    with pytest.raises(DiagLogRevoked):
        next(chunks)


def test_in_flight_download_is_revoked_when_the_ttl_expires(tmp_path: Path) -> None:
    clock = {"now": 1_800_000_000.0}
    gate = DiagLogsGate(log_roots={"instance": tmp_path}, now=lambda: clock["now"])
    payload = tmp_path / "big.log"
    payload.write_bytes(b"z" * (256 * 1024))
    gate.open(ttl_seconds=MIN_TTL_SECONDS)

    chunks = iter_log_bytes(payload, chunk_size=4096, keep_alive=gate.is_open)
    assert len(next(chunks)) == 4096
    clock["now"] += MIN_TTL_SECONDS + 1
    with pytest.raises(DiagLogRevoked):
        next(chunks)


def test_download_route_binds_the_live_gate_to_the_stream() -> None:
    """The read route must hand the live switch to the streamer.

    Without this binding the admission check would only run once, and a
    download would keep running after the switch was closed or expired.
    """

    source = Path("remote/api/diag_logs.py").read_text(encoding="utf-8")
    assert "keep_alive=lambda: gate.token_is_alive(window)" in source
    assert "keep_alive=gate.is_open" not in source


def test_download_started_before_a_reopen_is_still_revoked(tmp_path: Path) -> None:
    """Closing and re-opening must not resurrect a download from the old window.

    Regression: the stream only asked "is the switch open?", so a close followed
    a second later by an open put the flag back to True and the transfer carried
    on as if nothing had happened.
    """

    gate = DiagLogsGate(log_roots={"instance": tmp_path})
    payload = tmp_path / "big.log"
    payload.write_bytes(b"x" * (256 * 1024))
    gate.open()

    window = gate.active_token()
    chunks = iter_log_bytes(
        payload,
        chunk_size=4096,
        keep_alive=lambda: gate.token_is_alive(window),
    )
    assert len(next(chunks)) == 4096

    gate.close()
    gate.open()  # a fresh window: same flag, different authority
    assert gate.is_open() is True
    with pytest.raises(DiagLogRevoked):
        next(chunks)


def test_log_root_never_widens_to_the_instance_root(tmp_path: Path) -> None:
    """A blank or dot-ish ``base_logs_dir`` must not publish the instance itself.

    Regression: ``""`` resolved to the config directory, so the gate listed and
    served ``agents.json`` and ``secrets.json`` while the Core kept writing to
    ``<instance>/logs``.
    """

    (tmp_path / "agents.json").write_text('{"global": {}}', encoding="utf-8")
    (tmp_path / "secrets.json").write_text('{"token": "leak-me"}', encoding="utf-8")
    expected = (tmp_path / "logs").resolve()

    for value in ("", "   ", None, ".", "..", str(tmp_path), "@home/."):
        write_agents_json(tmp_path, value)
        assert resolve_effective_logs_dir(tmp_path) == expected, value


@pytest.mark.skipif(os.name != "nt", reason="junction escape check is Windows specific")
def test_junctioned_logs_dir_cannot_widen_the_root_to_the_instance(tmp_path: Path) -> None:
    """A ``logs`` junction pointing back at the instance must not be served.

    Regression: only the *configured* value was validated.  When ``logs`` was a
    junction to the instance root, the default fallback resolved there and was
    returned verbatim, publishing agents.json and secrets.json as "logs".
    """

    (tmp_path / "agents.json").write_text('{"global": {}}', encoding="utf-8")
    (tmp_path / "secrets.json").write_text('{"token": "leak-me"}', encoding="utf-8")
    logs = tmp_path / "logs"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(logs), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create junction: {result.stderr.strip()}")
    try:
        assert resolve_effective_logs_dir(tmp_path) is None
        assert "instance" not in build_diag_log_roots(tmp_path)
    finally:
        os.rmdir(logs)


@pytest.mark.skipif(os.name != "nt", reason="junction escape check is Windows specific")
def test_listing_skips_subdirectories_linked_outside_the_log_root(
    instance_root: Path,
) -> None:
    """A junction *inside* logs must not pull its target into the listing."""

    link = instance_root / "logs" / "outside"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(instance_root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create junction: {result.stderr.strip()}")
    try:
        listing = list_log_files(instance_root / "logs")
        paths = [item["path"] for item in listing["files"]]
        assert "bridge.log" in paths
        assert not [path for path in paths if path.startswith("outside/")]
        assert "secrets.json" not in paths
    finally:
        os.rmdir(link)


def test_proxied_request_cannot_flip_the_switch(tmp_path: Path) -> None:
    """A relayed request looks loopback, so it must not inherit local rights."""

    gate = new_gate(tmp_path)
    client = build_client(tmp_path, gate, LOOPBACK)
    response = client.post(
        "/diag/gate",
        json={"enabled": True},
        headers={"x-forwarded-for": "192.168.0.9"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "proxy_not_allowed"
    assert gate.is_open() is False


def test_remote_log_file_is_served_without_its_directory(tmp_path: Path) -> None:
    """Only the Remote log and its rotated copies are exposed, not the folder."""

    shared = tmp_path / "operator-files"
    shared.mkdir()
    (shared / "remote.log").write_bytes(b"remote line\n")
    (shared / "remote.log.1").write_bytes(b"older\n")
    (shared / "remote.log.2026-11-28.gz").write_bytes(b"gz")
    (shared / "secrets.json").write_text('{"token": "leak-me"}', encoding="utf-8")
    (shared / "notes.txt").write_text("private", encoding="utf-8")

    roots = build_diag_log_roots(tmp_path, remote_log_file=shared / "remote.log")
    remote = roots[LOG_ROOT_REMOTE]
    assert isinstance(remote, LogFileSet)

    gate = DiagLogsGate(log_roots=roots, instance_id="HASHI8")
    client = build_client(tmp_path, gate, LOOPBACK)
    assert client.post("/diag/gate", json={"enabled": True}).status_code == 200

    listing = client.get("/diag/logs").json()
    served = {(entry["root"], entry["path"]) for entry in listing["files"]}
    assert ("remote", "remote.log") in served
    assert ("remote", "remote.log.1") in served
    assert ("remote", "remote.log.2026-11-28.gz") in served
    assert ("remote", "secrets.json") not in served
    assert ("remote", "notes.txt") not in served

    assert client.get("/diag/logs/remote/remote.log").content == b"remote line\n"
    assert client.get("/diag/logs/remote/remote.log.1").content == b"older\n"
    assert client.get("/diag/logs/remote/secrets.json").status_code == 404
    assert client.get("/diag/logs/remote/notes.txt").status_code == 404
# HASHI-Fingerprint: hfp_4174ac376c904b8285ee559e4dfb6a6b
