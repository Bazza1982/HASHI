from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from remote import main as remote_main
from remote.main import _remote_log_handler, _trim_oversized_log


def test_legacy_remote_log_is_trimmed_from_the_tail_without_full_scan(tmp_path: Path):
    path = tmp_path / "hashi-remote-supervisor.log"
    path.write_bytes((b"old-record\n" * 4096) + b"newest-record\n")

    assert _trim_oversized_log(path, max_bytes=1024) is True

    retained = path.read_bytes()
    assert len(retained) <= 1024
    assert retained.endswith(b"newest-record\n")
    assert b"old-record\n" in retained
    assert list(tmp_path.glob(".*.bounded.tmp")) == []


def test_remote_log_handler_bounds_files_backups_and_single_records(tmp_path: Path):
    path = tmp_path / "hashi-remote-supervisor.log"
    path.write_bytes(b"legacy\n" * 300)
    handler, trimmed = _remote_log_handler(
        path,
        max_bytes=512,
        backup_count=2,
        max_record_bytes=128,
    )
    logger = logging.getLogger(f"test.remote.bounded.{id(handler)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        for index in range(80):
            logger.info("record-%03d %s", index, "x" * 80)
        logger.info("oversized %s", "y" * 2048)
        for _ in range(8):
            logger.info("multibyte %s", "🌸" * 2048)
    finally:
        handler.close()
        logger.handlers.clear()

    files = [
        candidate
        for candidate in [path, Path(f"{path}.1"), Path(f"{path}.2")]
        if candidate.exists()
    ]
    assert trimmed == [path]
    assert len(files) == 3
    assert all(candidate.stat().st_size <= 512 for candidate in files)
    assert "[truncated " in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("failure_mode", ["open", "write"])
def test_unavailable_remote_log_uses_stable_supervisor_fallback(
    tmp_path: Path, monkeypatch, failure_mode: str
):
    class _WriteFailureStream:
        def __init__(self, stream):
            self._stream = stream

        def write(self, value):
            raise OSError("simulated initial write failure")

        def __getattr__(self, name):
            return getattr(self._stream, name)

    fallback = io.StringIO()
    monkeypatch.setattr(remote_main.sys, "stderr", fallback)
    log_file = tmp_path / "remote.log"
    if failure_mode == "open":
        blocked_parent = tmp_path / "not-a-directory"
        blocked_parent.write_text("blocked", encoding="utf-8")
        log_file = blocked_parent / "remote.log"
    else:
        original_open = remote_main._Utf8RotatingFileHandler._open

        def open_unwriteable(handler):
            return _WriteFailureStream(original_open(handler))

        monkeypatch.setattr(
            remote_main._Utf8RotatingFileHandler, "_open", open_unwriteable
        )
    app = remote_main.HashiRemoteApplication(
        hashi_root=tmp_path,
        use_tls=False,
        log_file=log_file,
    )

    try:
        app._setup_logging()
        remote_main.logger.error("fallback-record-one")
        remote_main.logger.error("fallback-record-two")
        for handler in logging.getLogger().handlers:
            handler.flush()
    finally:
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)

    output = fallback.getvalue()
    assert output.count("Could not activate bounded Remote log") == 1
    if failure_mode == "write":
        assert "simulated initial write failure" in output
    assert "fallback-record-one" in output
    assert "fallback-record-two" in output
    assert "--- Logging error ---" not in output
