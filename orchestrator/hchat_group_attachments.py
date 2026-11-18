"""Snapshot one user-selected attachment set for an HChat group attempt."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def snapshot_group_attachments(paths: list[Path]) -> Iterator[tuple[list[Path], list[dict]]]:
    """Keep every recipient on identical bytes and remove the private copies afterwards."""
    if not paths:
        yield [], []
        return
    with tempfile.TemporaryDirectory(prefix="hashi-hchat-group-") as temp_dir:
        root = Path(temp_dir)
        snapshots = []
        manifest = []
        for index, raw_path in enumerate(paths):
            path = Path(raw_path).expanduser()
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or path.is_symlink():
                raise ValueError("Selected HChat attachment is not a regular file")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(path, flags)
            try:
                opened = os.fstat(source_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                ):
                    raise ValueError("Selected HChat attachment changed before reading")
                directory = root / str(index)
                directory.mkdir()
                snapshot = directory / path.name
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(source_fd, "rb") as source:
                    source_fd = -1
                    with snapshot.open("xb") as destination:
                        while chunk := source.read(1024 * 1024):
                            destination.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                        destination.flush()
                        os.fsync(destination.fileno())
                    after = os.fstat(source.fileno())
                current = path.lstat()
                if (
                    size != before.st_size
                    or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    or not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                ):
                    raise ValueError("Selected HChat attachment changed while reading")
                snapshots.append(snapshot)
                manifest.append({
                    "filename": path.name, "size_bytes": size, "sha256": digest.hexdigest(),
                })
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
        yield snapshots, manifest
