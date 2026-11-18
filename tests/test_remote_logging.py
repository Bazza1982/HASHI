from __future__ import annotations

import logging
from pathlib import Path

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
