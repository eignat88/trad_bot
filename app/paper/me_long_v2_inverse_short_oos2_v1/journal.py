"""OOS-2-only local durable journal for PENDING PROTECTIVE CLOSES (Round 7, P1).

Codex thread ``r4584484945`` (Issue #276, PR #278): when a protective
STOP/TIMEOUT fires while the close-leg history is unreadable **and** the
funding-obligation write fails, the old code returned ``None`` from
``_close()`` and left the position in ``open_positions`` — the original
trigger, the frozen exit facts and the OBSERVED funding rate were all lost
and the next cycle re-evaluated the exit with fresh inputs.

The close itself is now retained as a durable pending protective close.
Its terminal facts already travel through the PostgreSQL close outbox
(``stage_close``), but the **observed rate** has no PostgreSQL home while
``save_funding_obligation()`` is failing — and a memory-only copy would not
survive a process crash ("restart before PostgreSQL recovery", mandatory
test D).  This module is that missing durable home.

Contract
--------
* **OOS-2 only** — never shared with V1/V2, the production Scanner or any
  other contour; it lives inside the isolated OOS-2 package.
* **Atomic** — a record is written to ``<key>.json.tmp``, flushed and
  ``fsync``-ed, then moved over the final name with :func:`os.replace`:
  a reader either sees the previous record or the complete new one, never a
  torn file.
* **Replay** — :meth:`ProtectiveCloseJournal.read_all` returns every
  record keyed by ``position_key`` so an engine restart can restore the
  frozen terminal facts, the owed partial legs and the observed obligation
  without a new market trigger.
* **Deduplicated** — one file per ``position_key``; writing the same key
  again overwrites it with byte-identical facts (a replay never forks the
  record), and a file whose payload does not belong to its own key is
  refused rather than merged.
* **Best effort, honestly reported** — a failed write never raises into the
  protective-exit path (an exit is never blocked by the journal); the caller
  records the key as journal-failed, keeps ``durability_risk`` true and
  refuses new entries, so an impossible guarantee is *documented by the
  telemetry* instead of being silently claimed.

Directory
---------
``<repo>/var/me_inverse_short_oos2_journal`` by default — ``var/`` is
git-ignored (same directory family as the runtime lock file).  Tests pass
their own ``tmp_path`` so no record can ever leak between runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Schema tag written into every record (forward-compat guard: a record from
#: a future schema generation is refused instead of being misread).
JOURNAL_SCHEMA = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:PENDING_PROTECTIVE_CLOSE:v1"

#: ``app/paper/me_long_v2_inverse_short_oos2_v1/journal.py`` -> repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Default durable location (git-ignored ``var/``, created lazily).
DEFAULT_JOURNAL_DIR = PROJECT_ROOT / "var" / "me_inverse_short_oos2_journal"

_SUFFIX = ".json"

#: Windows cannot flush a directory handle — logged once per process so the
#: limitation is VISIBLE without flooding the log on every write.
_DIRECTORY_FSYNC_UNSUPPORTED_LOGGED = False


def _warn_directory_fsync_unsupported(directory: Path, exc: OSError) -> None:
    """Report (once) that this platform has no directory-fsync primitive."""
    global _DIRECTORY_FSYNC_UNSUPPORTED_LOGGED
    if _DIRECTORY_FSYNC_UNSUPPORTED_LOGGED:
        return
    _DIRECTORY_FSYNC_UNSUPPORTED_LOGGED = True
    logger.warning(
        "me inverse short oos2 journal directory fsync is NOT available on "
        "this platform (os.name=%s): a directory handle cannot be flushed "
        "here (%s).  The record payload is still fsync'ed before the atomic "
        "rename and the record is written; only the directory-ENTRY "
        "durability guarantee is POSIX-only.  dir=%s",
        os.name, exc, directory,
    )


@dataclass
class PendingProtectiveClose:
    """Frozen terminal facts of ONE retained protective close.

    Every field Codex listed for ``PENDING_PROTECTIVE_CLOSE`` is present:
    ``position_key``, ``exit_reason``, ``triggered_at``, reference/fill
    price, quantity, the executed partial legs, fees/slippage (inside the
    position snapshot), the observed funding rate + periods (inside
    ``obligation``), the accounting delta and the recovery ids.
    """

    position_key: str
    exit_reason: str
    triggered_at: str | None
    reference_price: float
    fill_price: float
    quantity: float
    #: ``InverseShortPosition.to_dict()`` — the byte-for-byte terminal
    #: snapshot that ``flush_pending_closes()`` replays.
    snapshot: dict[str, Any]
    #: ``InverseShortCloseLeg.to_dict()`` — seq 3, re-staged with the outbox.
    terminal_leg: dict[str, Any]
    #: Dirty TP1/TP2 legs owed by this key (staged in the same transaction).
    owed_partial_legs: list[dict[str, Any]] = field(default_factory=list)
    #: ``InverseShortFundingObligation.to_dict()`` — the OBSERVED rate that
    #: could not reach PostgreSQL yet (``None`` when no rate was observed).
    obligation: dict[str, Any] | None = None
    #: In-process balance delta of this close (terminal leg − folded funding).
    accounting_delta: float = 0.0
    #: Deterministic ids used to recognise the record on replay.
    recovery_ids: dict[str, Any] = field(default_factory=dict)
    #: ISO-8601 instant of the write (audit only, never a trading input).
    written_at: str | None = None
    #: True when ``stage_close()`` had already accepted this snapshot —
    #: only then does a restart need to skip the outbox re-stage half.
    staged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": JOURNAL_SCHEMA,
            "position_key": self.position_key,
            "exit_reason": self.exit_reason,
            "triggered_at": self.triggered_at,
            "reference_price": self.reference_price,
            "fill_price": self.fill_price,
            "quantity": self.quantity,
            "snapshot": self.snapshot,
            "terminal_leg": self.terminal_leg,
            "owed_partial_legs": self.owed_partial_legs,
            "obligation": self.obligation,
            "accounting_delta": self.accounting_delta,
            "recovery_ids": self.recovery_ids,
            "written_at": self.written_at,
            "staged": self.staged,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingProtectiveClose:
        if data.get("schema") != JOURNAL_SCHEMA:
            raise ValueError(
                f"PENDING_PROTECTIVE_CLOSE_SCHEMA_MISMATCH:{data.get('schema')!r}",
            )
        return cls(
            position_key=str(data["position_key"]),
            exit_reason=str(data.get("exit_reason") or ""),
            triggered_at=data.get("triggered_at"),
            reference_price=float(data.get("reference_price") or 0.0),
            fill_price=float(data.get("fill_price") or 0.0),
            quantity=float(data.get("quantity") or 0.0),
            snapshot=dict(data.get("snapshot") or {}),
            terminal_leg=dict(data.get("terminal_leg") or {}),
            owed_partial_legs=list(data.get("owed_partial_legs") or []),
            obligation=(
                dict(data["obligation"]) if data.get("obligation") else None
            ),
            accounting_delta=float(data.get("accounting_delta") or 0.0),
            recovery_ids=dict(data.get("recovery_ids") or {}),
            written_at=data.get("written_at"),
            staged=bool(data.get("staged")),
        )


class ProtectiveCloseJournal:
    """Atomic, replayable, de-duplicated local journal (one file per key)."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = (
            Path(directory) if directory is not None else DEFAULT_JOURNAL_DIR
        )

    # -- helpers ----------------------------------------------------------
    def _path(self, position_key: str) -> Path:
        # position_key contains ':' and is long — hash it for a portable,
        # collision-free file name (the key itself is inside the payload).
        digest = hashlib.sha256(position_key.encode("utf-8")).hexdigest()[:40]
        return self.directory / f"{digest}{_SUFFIX}"

    def _ensure_dir(self) -> list[Path]:
        """Create the journal directory; return the levels ``mkdir`` created.

        Round 8 (P2, Codex r4587604985): the directory entries ``mkdir``
        just created live inside their PARENTS, so those parents have to be
        synchronised as well — otherwise a crash right after the very first
        write can lose the whole journal tree before the rename ever
        happens.  The list is shallowest-first, which is also the order the
        parents must be flushed in.
        """
        if self.directory.exists():
            return []
        missing: list[Path] = []
        probe = self.directory
        while not probe.exists() and probe != probe.parent:
            missing.append(probe)
            probe = probe.parent
        self.directory.mkdir(parents=True, exist_ok=True)
        return list(reversed(missing))

    # -- public API -------------------------------------------------------
    def write(self, record: PendingProtectiveClose) -> Path:
        """Atomically persist one record; returns the file path.

        The record is only reported as written once the payload ``fsync``
        AND the directory entry of the final name are confirmed durable —
        any ``OSError`` propagates to the CALLER, which decides how to
        degrade (it must keep the key flagged as durability risk rather
        than pretend the record survived).
        """
        created = self._ensure_dir()
        # The entry of a directory ``mkdir`` just created lives in ITS
        # parent; flushing every level's parent covers the whole chain.
        new_parents = [level.parent for level in created]
        path = self._path(record.position_key)
        payload = json.dumps(
            record.to_dict(), ensure_ascii=False, sort_keys=True,
        )
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())        # payload durable BEFORE rename
        os.replace(tmp, path)                # atomic on POSIX and Windows
        for directory in new_parents:
            self._fsync_directory(directory)
        self._fsync_directory(self.directory)
        return path

    def _fsync_directory(self, directory: Path) -> None:
        """Synchronise ONE directory entry; failures PROPAGATE.

        Round 8 (P2, Codex r4587604985): the previous implementation opened
        ``os.curdir`` — the process working directory, which in the supplied
        runtime is the release root while the records live under
        ``var/me_inverse_short_oos2_journal`` — so the rename's directory
        entry was never synchronised, and it swallowed every ``OSError``
        (both the open and the ``fsync``), reporting a record as durable
        when no directory synchronisation had happened at all.

        Windows has no directory-fsync primitive whatsoever: a directory
        handle can be opened with ``FILE_FLAG_BACKUP_SEMANTICS`` but
        ``FlushFileBuffers`` rejects it with ``ERROR_ACCESS_DENIED``
        (verified on this platform).  That limitation is REPORTED at
        warning level — never hidden behind ``except OSError: return`` and
        never presented as a completed synchronisation.  The payload is
        still ``fsync``-ed before the atomic rename in every case.
        """
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError as exc:
            if os.name == "nt" and isinstance(exc, PermissionError):
                _warn_directory_fsync_unsupported(directory, exc)
                return
            raise
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def read_all(self) -> dict[str, PendingProtectiveClose]:
        """Every durable record, keyed by ``position_key`` (deduplicated).

        A corrupt / foreign / wrong-schema file is logged and SKIPPED — it
        is never merged into the engine state, and it never aborts recovery
        of the healthy records.
        """
        if not self.directory.exists():
            return {}
        records: dict[str, PendingProtectiveClose] = {}
        for path in sorted(self.directory.glob(f"*{_SUFFIX}")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    record = PendingProtectiveClose.from_dict(json.load(handle))
            except FileNotFoundError:
                continue  # retired concurrently — nothing to replay
            except Exception:
                logger.error(
                    "me inverse short oos2 pending protective close journal "
                    "record unreadable (skipped, never merged): file=%s",
                    path.name, exc_info=True,
                )
                continue
            if self._path(record.position_key) != path:
                logger.error(
                    "me inverse short oos2 pending protective close journal "
                    "record refused (file/payload key mismatch — dedup "
                    "guard): file=%s key=%s",
                    path.name, record.position_key[:48],
                )
                continue
            records[record.position_key] = record
        return records

    def delete(self, position_key: str) -> bool:
        """Idempotently retire one record; True when a file was removed."""
        try:
            self._path(position_key).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "me inverse short oos2 pending protective close journal "
                "retire failed (record kept, retried next cycle): key=%s",
                position_key[:48], exc_info=True,
            )
            return False
        return True

    def exists(self, position_key: str) -> bool:
        return self._path(position_key).exists()

    def paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(self.directory.glob(f"*{_SUFFIX}"))
