"""Crash-safe, cross-process JSONL primitives for the local fallback."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_thread_locks_guard = threading.Lock()
_thread_locks: dict[Path, threading.Lock] = {}


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    """Return the process-local guard for a sidecar lock file."""
    with _thread_locks_guard:
        return _thread_locks.setdefault(lock_path.resolve(), threading.Lock())


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialize writers using a stable sidecar lock file.

    ``msvcrt.locking`` coordinates separate processes, but Windows rejects a
    second lock request from another thread in the *same* process with
    ``EDEADLK`` rather than waiting.  The local lock serializes those requests
    before the cross-process lock is acquired.
    """
    import msvcrt

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock_for(lock_path):
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read complete records and ignore only an interrupted final line."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise
    return records


def atomic_rewrite(path: Path, records: list[dict[str, Any]]) -> None:
    """Replace a JSONL file only after a complete fsynced temporary write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Append one complete JSON object while holding the writer lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False) + "\n"
    with file_lock(path):
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
