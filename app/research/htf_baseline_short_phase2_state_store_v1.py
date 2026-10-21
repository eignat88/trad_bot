"""Issue #176 Phase-2 durable state store (Blocker B5).

Production-grade checkpoint handling for the Phase-2 observer:

* versioned, checksummed envelope (``HTF_P2_STATE_V1``) with SHA-256 integrity;
* atomic save: temp file in the destination directory + fsync + ``os.replace``
  + best-effort directory fsync; interrupted writes never clobber the last
  valid checkpoint;
* fail-closed restore: corrupt, truncated, tampered or legacy-schema
  checkpoints raise and are never reinterpreted;
* single-writer lease with metadata and heartbeat; a stale lease (crashed
  writer) is recoverable after ``stale_after_s`` when explicitly allowed
  (``allow_stale_takeover``) — the operator-visible default refuses takeover;
* durable cursor: the checkpoint records the last processed closed 5m bar and
  the set of already-emitted source event ids, so restart replay is idempotent.

Deliberate guarantee statement (see also the protocol): this store provides
AT-LEAST-ONCE processing with IDEMPOTENT emission (DB unique key + emitted
event set in the checkpoint). It does NOT claim exactly-once delivery.
"""
from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

STATE_SCHEMA = "HTF_P2_STATE_V1"
LEGACY_SCHEMA_REJECTED = "HTF_176_COMBINED_CHECKPOINT_V2"


class StateStoreError(RuntimeError):
    pass


class LegacyCheckpointError(StateStoreError):
    pass


class WriterLeaseError(StateStoreError):
    pass


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def checkpoint_envelope(payload: dict) -> dict:
    body = _canonical(payload)
    return {"schema": STATE_SCHEMA, "sha256": sha256(body.encode("utf-8")).hexdigest(), "payload": payload}


def write_checkpoint(path: str | os.PathLike, payload: dict, *, fault: str | None = None) -> None:
    """Atomically persist a checksummed checkpoint.

    ``fault`` injects deterministic failure points for fault-injection tests:
    ``"before-tempfile"``, ``"after-tempfile"``, ``"after-fsync-before-replace"``,
    ``"replace-directory"``.
    """
    target = Path(path)
    envelope = checkpoint_envelope(payload)
    body = json.dumps(envelope, sort_keys=True, indent=2)
    if fault == "before-tempfile":
        raise StateStoreError("injected fault before tempfile")
    if fault == "replace-directory":
        raise ValueError("unknown checkpoint fault: replace-directory")
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if fault == "after-tempfile":
            raise StateStoreError("injected fault after tempfile")
        if fault == "after-fsync-before-replace":
            raise StateStoreError("injected fault after fsync before replace")
        os.replace(temp, target)
        _fsync_dir(target.parent)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _fsync_dir(directory: Path) -> bool:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return False  # e.g. Windows directories
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def read_checkpoint(path: str | os.PathLike) -> dict:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StateStoreError(f"checkpoint unreadable: {exc}") from exc
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateStoreError(f"checkpoint corrupt (invalid JSON): {exc}") from exc
    if not isinstance(envelope, dict) or "payload" not in envelope:
        raise LegacyCheckpointError("checkpoint missing Phase-2 envelope")
    schema = envelope.get("schema")
    if schema == LEGACY_SCHEMA_REJECTED:
        raise LegacyCheckpointError(
            f"legacy checkpoint schema {schema!r} rejected; expected {STATE_SCHEMA!r}"
        )
    if schema != STATE_SCHEMA:
        raise LegacyCheckpointError(
            f"unknown checkpoint schema {schema!r}; expected {STATE_SCHEMA!r}"
        )
    digest = sha256(_canonical(envelope["payload"]).encode("utf-8")).hexdigest()
    if digest != envelope.get("sha256"):
        raise StateStoreError("checkpoint integrity mismatch (sha256)")
    return envelope["payload"]


def checkpoint_exists(path: str | os.PathLike) -> bool:
    return Path(path).exists()


# --------------------------------------------------------------------- lease
LEASE_SUFFIX = ".writer.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True  # unknown → assume alive (conservative)
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True  # unknown → assume alive (conservative)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


class WriterLease:
    """Single-writer lease with heartbeat, fencing token and explicit stale takeover.

    Concurrency contract (remediation CP5):

    * takeover is only possible with the explicit ``allow_stale_takeover``
      flag AND proof that the previous owner is gone: a lock whose owner PID
      is verifiably dead is stale; a lock whose owner PID is ALIVE is never
      stale, no matter how old its heartbeat is (a slow writer must never be
      stolen from);
    * every acquisition mints a unique ``generation`` fencing token; heartbeat
      and release verify ownership by generation, so a writer that lost its
      lease to a takeover detects ``LEASE_LOST`` at its next heartbeat and
      must stop writing — two writers cannot both believe they hold the lease
      for more than one heartbeat interval;
    * unreadable lock metadata (crash during lock creation) is recoverable
      only through the same explicit takeover path.
    """

    def __init__(self, path: str | os.PathLike, *, instance_id: str, stale_after_s: float = 600.0):
        self.lock_path = Path(str(path) + LEASE_SUFFIX)
        self.instance_id = instance_id
        self.stale_after_s = float(stale_after_s)
        self._owned = False
        self._generation = None

    def acquire(self, *, allow_stale_takeover: bool = False) -> dict:
        import time
        import uuid

        metadata = {
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "generation": uuid.uuid4().hex,
            "acquired_at_ms": int(time.time() * 1000),
            "heartbeat_at_ms": int(time.time() * 1000),
        }
        for attempt in (1, 2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(metadata, handle)
                self._owned = True
                self._generation = metadata["generation"]
                return metadata
            except FileExistsError:
                existing = self._read_lock()
                stale = self._is_stale(existing)
                if attempt == 1 and stale and allow_stale_takeover:
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                raise WriterLeaseError(
                    f"OBSERVER_WRITER_ALREADY_ACTIVE: {self.lock_path} held by "
                    f"{existing}; stale={stale}; takeover only with explicit "
                    f"allow_stale_takeover"
                ) from None
        raise WriterLeaseError("unreachable")

    def _read_lock(self) -> dict:
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _is_stale(self, existing: dict) -> bool:
        import time

        pid = existing.get("pid")
        if isinstance(pid, int):
            if _pid_alive(pid):
                return False  # a live owner is NEVER stale
            return True  # verifiably dead owner -> recoverable
        # no readable owner pid: only an explicit takeover may recover this
        heartbeat = existing.get("heartbeat_at_ms")
        if isinstance(heartbeat, (int, float)):
            age_s = (time.time() * 1000 - heartbeat) / 1000.0
            return age_s > self.stale_after_s
        return True  # unreadable lock metadata -> recoverable via explicit takeover

    def heartbeat(self) -> None:
        import time

        if not self._owned:
            raise WriterLeaseError("heartbeat without ownership")
        existing = self._read_lock()
        if (
            existing.get("instance_id") != self.instance_id
            or existing.get("generation") != self._generation
        ):
            self._owned = False
            raise WriterLeaseError(
                "LEASE_LOST: lock was taken over by another writer; "
                "this instance must stop writing"
            )
        existing["heartbeat_at_ms"] = int(time.time() * 1000)
        temp = self.lock_path.with_suffix(".tmp")
        temp.write_text(json.dumps(existing), encoding="utf-8")
        os.replace(temp, self.lock_path)

    def release(self) -> None:
        if not self._owned:
            return
        existing = self._read_lock()
        if (
            existing.get("instance_id") == self.instance_id
            and existing.get("generation") == self._generation
        ):
            try:
                self.lock_path.unlink()
            except OSError:
                pass
        self._owned = False

    def __enter__(self) -> "WriterLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
