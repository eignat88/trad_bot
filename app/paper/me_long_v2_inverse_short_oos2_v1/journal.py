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

# ---------------------------------------------------------------------------
# Round 10 (P1, Codex r4597065733): the recovery journal READ verdict.
#
# "Unavailable" is NOT "absent".  A recovery loader that cannot tell those
# two apart will happily report a clean restart while the only durable copy
# of a protective STOP sits on disk unread.
# ---------------------------------------------------------------------------

#: The journal was listed and every record parsed and integrity-checked.
JOURNAL_LOADED = "JOURNAL_LOADED"
#: Listing succeeded and reported ENOENT — absence is PROVEN, not assumed.
JOURNAL_ABSENT_CONFIRMED = "JOURNAL_ABSENT_CONFIRMED"
#: The journal exists (or its existence cannot be determined) and could not
#: be read: the caller must fail closed and retry, never treat it as empty.
JOURNAL_READ_FAILED = "JOURNAL_READ_FAILED"

#: Round 11 (P2, Codex r4597992776): the decoded payload is syntactically
#: valid JSON but NOT a JSON object (``null`` / ``[]`` / ``"text"`` / ``123``
#: / ``true``).  It can never be a recovery record, and it must never reach
#: ``PendingProtectiveClose.from_dict()``.
JOURNAL_INVALID_PAYLOAD_TYPE = "JOURNAL_INVALID_PAYLOAD_TYPE"

#: Round 12 (P2, Codex r4598420146): the record's OUTER payload is valid but
#: one of its NESTED recovery facts (the frozen snapshot, the terminal leg,
#: an owed partial leg or the observed funding obligation) cannot be parsed
#: or contradicts the outer record.  Recovery must fail closed BEFORE any
#: engine state is touched, so the file is never retired on a half restore.
JOURNAL_SNAPSHOT_RESTORE_FAILED = "JOURNAL_SNAPSHOT_RESTORE_FAILED"

#: Exceptions a malformed journal payload raises while it is decoded or
#: folded into a record.  Explicit on purpose — never a bare
#: ``except Exception``: every member is a *bad input* class, so catching it
#: converts corrupt data into a REPORTED recovery failure instead of letting
#: it escape :meth:`ProtectiveCloseJournal.read_checked`.  ``AttributeError``
#: is exactly what a non-object payload used to raise (``data.get()`` on
#: ``None``/``[]``/``"text"``/``123``/``true``), which aborted recovery before
#: the fail-closed gate could be given a status; ``OverflowError`` covers an
#: unrepresentable numeric field, ``RecursionError`` a pathologically nested
#: corrupt file.
_DESERIALIZATION_ERRORS = (
    AttributeError,
    KeyError,
    OverflowError,
    RecursionError,
    TypeError,
    ValueError,
)


class JournalReadError(RuntimeError):
    """The recovery journal could not be read (never "there is nothing")."""


@dataclass(frozen=True)
class JournalReadResult:
    """Outcome of one recovery-journal read (three states, Round 10)."""

    status: str
    records: dict[str, "PendingProtectiveClose"]
    #: Human-readable diagnostic kept as long as the recovery gate is shut.
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != JOURNAL_READ_FAILED


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
        # Absolute on purpose: every directory this journal fsyncs must be
        # the REAL one, never a path resolved against the process working
        # directory (which in the supplied runtime is the release root).
        self.directory = (
            Path(directory).absolute() if directory is not None
            else DEFAULT_JOURNAL_DIR
        )

    # -- helpers ----------------------------------------------------------
    def _path(self, position_key: str) -> Path:
        # position_key contains ':' and is long — hash it for a portable,
        # collision-free file name (the key itself is inside the payload).
        digest = hashlib.sha256(position_key.encode("utf-8")).hexdigest()[:40]
        return self.directory / f"{digest}{_SUFFIX}"

    def _ensure_dir(self) -> None:
        """Create the journal directory if it does not exist yet.

        Round 9 (P2, Codex r4590138839): creation only.  Whether the
        entries this ``mkdir`` wrote are DURABLE is decided by
        :meth:`_ancestor_chain` on every write — this method deliberately
        reports nothing, because "the directory exists now" is not evidence
        that a previously failed synchronisation ever completed.
        """
        if self.directory.exists():
            return
        self.directory.mkdir(parents=True, exist_ok=True)

    def _ancestor_chain(self) -> list[Path]:
        """``self.directory`` followed by its ancestors, root excluded.

        Round 9 (P2, Codex r4590138839): the first write creates the whole
        tree and then fails while syncing a PARENT directory.  On the retry
        ``_ensure_dir()`` sees an existing directory and creates nothing, so
        the old code synchronised only the leaf and reported the record as
        durable — although the newly created ancestor entries had never been
        confirmed and a host crash could still lose the entire journal tree.

        The chain is therefore recomputed and synchronised on EVERY write:
        it is stateless (nothing to lose across a process restart), it
        cannot be skipped by a de-duplicated rewrite of identical facts, and
        it needs no separate fsync queue.  Leaf first: the record's own
        directory entry, then the directories that entry lives in.

        The FILESYSTEM ROOT is deliberately excluded: this journal never
        creates it (the journal always sits at least one level below an
        existing tree), and a volume root is not openable as a directory
        handle on Windows at all.  Every directory whose entry ``mkdir``
        could have created for this journal is still covered.
        """
        chain = [self.directory]
        probe = self.directory.parent
        while probe != probe.parent:      # stops before the filesystem root
            chain.append(probe)
            probe = probe.parent
        return chain

    # -- public API -------------------------------------------------------
    def write(self, record: PendingProtectiveClose) -> Path:
        """Atomically persist one record; returns the file path.

        The record is only reported as written once the payload ``fsync``
        AND the directory entry of the final name AND every ancestor entry
        this journal depends on are confirmed durable — any ``OSError``
        propagates to the CALLER, which decides how to degrade (it must keep
        the key flagged as durability risk rather than pretend the record
        survived).
        """
        self._ensure_dir()
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
        for directory in self._ancestor_chain():
            self._fsync_directory(directory)
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

    def read_checked(self) -> JournalReadResult:
        """Round 10 (P1, Codex r4597065733): a THREE-state read verdict.

        A recovery journal that could not be read is NOT a journal that does
        not exist.  The previous implementation collapsed every I/O problem
        into "no records": a failed directory probe returned ``{}``, and a
        record whose ``open``/``read`` raised was logged and SKIPPED without
        leaving any trace — so after a restart the database's untouched
        ``OPEN`` row was restored, ``_journal_unresolved`` stayed empty,
        ``durability_risk`` reported ``False`` and the protective exit that
        this journal held was never retried, while subsequent cycles went on
        to re-evaluate the position against fresh market data.

        Statuses:

        ``JOURNAL_ABSENT_CONFIRMED``
            Listing the directory succeeded and reported ENOENT — the probe
            ran and the absence is proven, so normal recovery continues.
        ``JOURNAL_LOADED``
            Every record was read, parsed and integrity-checked.
        ``JOURNAL_READ_FAILED``
            The journal exists (or its existence cannot be determined) but
            the listing, a file read, the JSON payload, its TYPE (a
            syntactically valid non-object such as ``null``/``[]``), the
            schema or the file/payload key check failed.  ``records`` still
            carries every record that DID load (replaying a healthy record is
            always safe), ``reason`` names every file that did not, and the
            caller must keep its recovery gate CLOSED.  Nothing is ever
            deleted or rewritten here.
        """
        try:
            # ``os.scandir`` is the primitive every directory listing in this
            # path bottoms out in, so one fault there is one unreadable
            # listing — and it raises the real errno instead of letting a
            # glob swallow it and report "no records".
            with os.scandir(self.directory) as scan:
                paths = sorted(
                    Path(entry.path) for entry in scan
                    if entry.path.endswith(_SUFFIX) and entry.is_file()
                )
        except FileNotFoundError:
            return JournalReadResult(status=JOURNAL_ABSENT_CONFIRMED, records={})
        except OSError as exc:
            return JournalReadResult(
                status=JOURNAL_READ_FAILED, records={},
                reason=f"journal directory listing failed: {exc!r}",
            )
        records: dict[str, PendingProtectiveClose] = {}
        failures: list[str] = []
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except FileNotFoundError:
                continue  # retired concurrently — nothing to replay
            except OSError as exc:
                # Temporarily unreadable (EIO / EACCES / ...): the record is
                # known to be there, so its absence from the replay is never
                # evidence that it does not exist.
                failures.append(
                    f"{path.name}: read failed ({type(exc).__name__}: {exc})",
                )
                continue
            except _DESERIALIZATION_ERRORS as exc:
                # Un-decodable payload: bad JSON, invalid UTF-8 bytes, an
                # absurd nesting depth.  An EXPLICIT recovery failure — never
                # silently skipped, never deleted.
                failures.append(
                    f"{path.name}: corrupt record ({type(exc).__name__})",
                )
                continue
            if not isinstance(payload, dict):
                # Round 11 (P2, Codex r4597992776): syntactically valid JSON
                # that is NOT an object (``null`` / ``[]`` / ``"text"`` /
                # ``123`` / ``true``) is a corrupt record, not a replayable
                # one.  It used to reach from_dict() and raise AttributeError
                # on ``data.get()``, and that exception ESCAPED this method —
                # recovery aborted before the fail-closed gate could ever be
                # given a status, healthy records next to it were never
                # processed, and the runtime could die at startup or in a
                # recovery cycle.  Only the payload TYPE is reported here;
                # journal CONTENT is never echoed into diagnostics.
                failures.append(
                    f"{path.name}: {JOURNAL_INVALID_PAYLOAD_TYPE} "
                    f"(json payload is {type(payload).__name__}, "
                    "not an object)",
                )
                continue
            try:
                record = PendingProtectiveClose.from_dict(payload)
            except _DESERIALIZATION_ERRORS as exc:
                # Structurally valid object with a missing required field, a
                # wrong field type or a schema/checksum mismatch: an EXPLICIT
                # recovery failure naming the real error.
                failures.append(
                    f"{path.name}: corrupt record ({type(exc).__name__})",
                )
                continue
            if self._path(record.position_key) != path:
                # Integrity (dedup) guard: the SHA-256 name and the payload
                # key disagree, so neither can be trusted.
                failures.append(
                    f"{path.name}: payload/file key mismatch (integrity)",
                )
                continue
            records[record.position_key] = record
        if failures:
            return JournalReadResult(
                status=JOURNAL_READ_FAILED, records=records,
                reason="; ".join(failures),
            )
        return JournalReadResult(status=JOURNAL_LOADED, records=records)

    def read_all(self) -> dict[str, PendingProtectiveClose]:
        """Every durable record, keyed by ``position_key`` (deduplicated).

        Compatibility accessor over :meth:`read_checked`.  It returns ``{}``
        only for a SUCCESSFULLY PROVEN absence and RAISES
        :class:`JournalReadError` when the journal could not be read — a
        caller may never mistake an I/O failure for "there is nothing to
        replay" (Round 10, Codex r4597065733).
        """
        result = self.read_checked()
        if result.status == JOURNAL_READ_FAILED:
            raise JournalReadError(
                result.reason or "recovery journal could not be read",
            )
        return result.records

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
