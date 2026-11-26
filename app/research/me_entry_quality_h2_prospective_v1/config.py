"""Issue #312 — H2 prospective runtime configuration (research only).

Self-contained, environment-driven configuration.  The runtime is
**disabled by default**: with ``H2_PROSPECTIVE_ENABLED`` off the runner makes
no database connection, no Bybit request and no write (criterion 17).

The candle provider requires its OWN bounded research API budget which must be
explicitly CONFIRMED (``H2_CANDLE_API_BUDGET_CONFIRMED=1``): without
confirmation the provider stays OFF and the runtime reports
``BLOCKED_CANDLE_SOURCE`` (FR-02) — a missing budget is never silently
defaulted into production-like traffic.

No production settings module is modified or imported; database credentials
are read from ``H2_PROSPECTIVE_DB_*`` with a ``DB_*`` fallback (the same
convention as ``scripts/me_upstream_source_v1_admin.py``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    CANDIDATE_OBSERVATION_WINDOWS,
)


class H2ProspectiveConfigError(ValueError):
    """Invalid configuration — fail closed, never guess a value."""


def _parse_bool(raw: str | None, name: str) -> bool:
    if raw is None:
        return False
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise H2ProspectiveConfigError(f"{name}: strict boolean required, got {raw!r}")


def _parse_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise H2ProspectiveConfigError(f"{name}: integer required, got {raw!r}") from None
    return value


def _parse_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise H2ProspectiveConfigError(f"{name}: number required, got {raw!r}") from None
    return value


@dataclass(frozen=True)
class H2ProspectiveConfig:
    """Immutable runtime configuration (fail closed, disabled by default)."""

    # ── contour ─────────────────────────────────────────────────────────────
    #: Master switch.  OFF by default: the runner then performs NO database
    #: connection, NO Bybit request and NO write of any kind.
    enabled: bool = False
    #: Bounded polling (FR-07): never a busy loop, never a scanner cadence.
    poll_interval_s: float = 300.0
    #: Max #305 rows read per poll (PostgreSQL load control).
    source_batch_limit: int = 200
    #: Forward-scan batch budget per cycle (review P1-overlap FR-02): reading
    #: new events can never starve reconciliation and vice versa — the two
    #: phases have INDEPENDENT, bounded budgets whose sum stays bounded.
    source_forward_batches_per_cycle: int = 8
    #: Overlap reconciliation batch budget per cycle (review P1-overlap).
    source_overlap_batches_per_cycle: int = 2
    #: Overlap rewind (seconds) re-read each cycle so late-committed #305 rows
    #: and ``captured_at`` ties can never be lost (review P1-watermark).
    #: Must exceed the maximum capture-to-commit disorder of the source.
    source_overlap_s: int = 7200
    #: Max source events evaluated per cycle (PostgreSQL / API load control).
    max_events_per_cycle: int = 20
    #: Statement timeout for every research statement (milliseconds).
    statement_timeout_ms: int = 10_000
    #: Explicit permission to run prospective accumulation with UNFROZEN
    #: policy fingerprints (pre-freeze research mode).  With pins absent and
    #: this flag off, an enabled runtime refuses to start (fail closed) —
    #: unfrozen-policy accumulation always needs a deliberate act.
    allow_unfrozen_policies: bool = False

    # ── database (dedicated research connections only) ──────────────────────
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "trad_bot"
    db_user: str = "postgres"
    db_password: str = ""
    db_unix_sock: str = ""

    # ── candle provider (FR-02): dedicated bounded research budget ─────────
    #: The budget must be explicitly confirmed; otherwise the provider is OFF.
    candle_budget_confirmed: bool = False
    candle_max_requests_per_cycle: int = 10
    candle_max_requests_per_hour: int = 240
    candle_max_retries: int = 2
    candle_retry_backoff_s: float = 2.0
    candle_request_pause_s: float = 0.5
    candle_page_size: int = 1000
    candle_max_pages: int = 10
    bybit_timeout_s: float = 15.0

    # ── policies (PRELIMINARY candidates — never post-hoc selectable) ──────
    observation_windows: tuple[int, ...] = CANDIDATE_OBSERVATION_WINDOWS
    #: Optional immutable policy-fingerprint pins.  Empty = pre-freeze
    #: research mode (fingerprints recorded, nothing frozen).  When set, a
    #: computed fingerprint outside the set refuses to run (fail closed).
    policy_fingerprint_pins: tuple[str, ...] = ()

    # ── lifecycle ──────────────────────────────────────────────────────────
    #: Evaluation attempts with transient incomplete coverage after the
    #: observation window elapsed before the outcome finalizes as
    #: INCOMPLETE_COVERAGE (never EXPIRED, never CONFIRMED_DELAYED).
    coverage_finalize_after_attempts: int = 3

    @property
    def source_overlap_us(self) -> int:
        """Overlap rewind in microseconds (exact integer math)."""
        return int(self.source_overlap_s) * 1_000_000

    def __post_init__(self) -> None:
        if self.poll_interval_s <= 0:
            raise H2ProspectiveConfigError("poll_interval_s must be positive")
        if self.source_batch_limit < 1:
            raise H2ProspectiveConfigError("source_batch_limit must be >= 1")
        if self.source_forward_batches_per_cycle < 1:
            raise H2ProspectiveConfigError(
                "source_forward_batches_per_cycle must be >= 1"
            )
        if self.source_overlap_batches_per_cycle < 1:
            raise H2ProspectiveConfigError(
                "source_overlap_batches_per_cycle must be >= 1"
            )
        if self.source_overlap_s < 0:
            raise H2ProspectiveConfigError("source_overlap_s cannot be negative")
        if self.max_events_per_cycle < 1:
            raise H2ProspectiveConfigError("max_events_per_cycle must be >= 1")
        if self.statement_timeout_ms < 1:
            raise H2ProspectiveConfigError("statement_timeout_ms must be >= 1")
        if self.candle_max_requests_per_cycle < 1:
            raise H2ProspectiveConfigError(
                "candle_max_requests_per_cycle must be >= 1"
            )
        if self.candle_max_requests_per_hour < 1:
            raise H2ProspectiveConfigError(
                "candle_max_requests_per_hour must be >= 1"
            )
        if self.candle_max_retries < 0:
            raise H2ProspectiveConfigError("candle_max_retries cannot be negative")
        if self.candle_retry_backoff_s < 0:
            raise H2ProspectiveConfigError("candle_retry_backoff_s cannot be negative")
        if self.candle_request_pause_s < 0:
            raise H2ProspectiveConfigError("candle_request_pause_s cannot be negative")
        if self.candle_page_size < 1 or self.candle_page_size > 1000:
            raise H2ProspectiveConfigError("candle_page_size must be in 1..1000")
        if self.candle_max_pages < 1:
            raise H2ProspectiveConfigError("candle_max_pages must be >= 1")
        if self.bybit_timeout_s <= 0:
            raise H2ProspectiveConfigError("bybit_timeout_s must be positive")
        if not self.observation_windows or any(
            not isinstance(w, int) or isinstance(w, bool) or w < 1
            for w in self.observation_windows
        ):
            raise H2ProspectiveConfigError(
                "observation_windows must be positive integers"
            )
        if self.coverage_finalize_after_attempts < 1:
            raise H2ProspectiveConfigError(
                "coverage_finalize_after_attempts must be >= 1"
            )
        if not self.db_name:
            raise H2ProspectiveConfigError("db_name must not be empty")


def load_config(environ: Mapping[str, str] | None = None) -> H2ProspectiveConfig:
    """Build the config from environment variables (strict, fail closed).

    Unknown or malformed values raise :class:`H2ProspectiveConfigError` — a
    broken config never falls back to defaults silently.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ

    def _get(name: str) -> str | None:
        return env.get(name)

    windows_raw = _get("H2_PROSPECTIVE_OBSERVATION_WINDOWS")
    if windows_raw is None or not windows_raw.strip():
        windows = CANDIDATE_OBSERVATION_WINDOWS
    else:
        try:
            windows = tuple(
                int(part.strip()) for part in windows_raw.split(",") if part.strip()
            )
        except ValueError:
            raise H2ProspectiveConfigError(
                "H2_PROSPECTIVE_OBSERVATION_WINDOWS: comma-separated integers"
            ) from None
        if not windows:
            raise H2ProspectiveConfigError(
                "H2_PROSPECTIVE_OBSERVATION_WINDOWS: empty list"
            )

    pins_raw = _get("H2_PROSPECTIVE_POLICY_FINGERPRINT_PINS") or ""
    pins = tuple(
        part.strip() for part in pins_raw.split(",") if part.strip()
    )
    for pin in pins:
        if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
            raise H2ProspectiveConfigError(
                "H2_PROSPECTIVE_POLICY_FINGERPRINT_PINS: canonical sha256 hex required"
            )

    config = H2ProspectiveConfig(
        enabled=_parse_bool(_get("H2_PROSPECTIVE_ENABLED"), "H2_PROSPECTIVE_ENABLED"),
        poll_interval_s=_parse_float(
            _get("H2_PROSPECTIVE_POLL_INTERVAL_S"), 300.0,
            "H2_PROSPECTIVE_POLL_INTERVAL_S",
        ),
        source_batch_limit=_parse_int(
            _get("H2_PROSPECTIVE_SOURCE_BATCH_LIMIT"), 200,
            "H2_PROSPECTIVE_SOURCE_BATCH_LIMIT",
        ),
        source_forward_batches_per_cycle=_parse_int(
            _get("H2_PROSPECTIVE_SOURCE_FORWARD_BATCHES_PER_CYCLE"), 8,
            "H2_PROSPECTIVE_SOURCE_FORWARD_BATCHES_PER_CYCLE",
        ),
        source_overlap_batches_per_cycle=_parse_int(
            _get("H2_PROSPECTIVE_SOURCE_OVERLAP_BATCHES_PER_CYCLE"), 2,
            "H2_PROSPECTIVE_SOURCE_OVERLAP_BATCHES_PER_CYCLE",
        ),
        source_overlap_s=_parse_int(
            _get("H2_PROSPECTIVE_SOURCE_OVERLAP_S"), 7200,
            "H2_PROSPECTIVE_SOURCE_OVERLAP_S",
        ),
        allow_unfrozen_policies=_parse_bool(
            _get("H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES"),
            "H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES",
        ),
        max_events_per_cycle=_parse_int(
            _get("H2_PROSPECTIVE_MAX_EVENTS_PER_CYCLE"), 20,
            "H2_PROSPECTIVE_MAX_EVENTS_PER_CYCLE",
        ),
        statement_timeout_ms=_parse_int(
            _get("H2_PROSPECTIVE_STATEMENT_TIMEOUT_MS"), 10_000,
            "H2_PROSPECTIVE_STATEMENT_TIMEOUT_MS",
        ),
        db_host=_get("H2_PROSPECTIVE_DB_HOST")
        or _get("DB_HOST") or "localhost",
        db_port=_parse_int(
            _get("H2_PROSPECTIVE_DB_PORT") or _get("DB_PORT"), 5432, "DB_PORT"
        ),
        db_name=_get("H2_PROSPECTIVE_DB_NAME") or _get("DB_NAME") or "trad_bot",
        db_user=_get("H2_PROSPECTIVE_DB_USER") or _get("DB_USER") or "postgres",
        db_password=_get("H2_PROSPECTIVE_DB_PASSWORD")
        or _get("DB_PASSWORD") or "",
        db_unix_sock=_get("H2_PROSPECTIVE_DB_UNIX_SOCK") or "",
        candle_budget_confirmed=_parse_bool(
            _get("H2_CANDLE_API_BUDGET_CONFIRMED"), "H2_CANDLE_API_BUDGET_CONFIRMED"
        ),
        candle_max_requests_per_cycle=_parse_int(
            _get("H2_CANDLE_MAX_REQUESTS_PER_CYCLE"), 10,
            "H2_CANDLE_MAX_REQUESTS_PER_CYCLE",
        ),
        candle_max_requests_per_hour=_parse_int(
            _get("H2_CANDLE_MAX_REQUESTS_PER_HOUR"), 240,
            "H2_CANDLE_MAX_REQUESTS_PER_HOUR",
        ),
        candle_max_retries=_parse_int(
            _get("H2_CANDLE_MAX_RETRIES"), 2, "H2_CANDLE_MAX_RETRIES"
        ),
        candle_retry_backoff_s=_parse_float(
            _get("H2_CANDLE_RETRY_BACKOFF_S"), 2.0, "H2_CANDLE_RETRY_BACKOFF_S"
        ),
        candle_request_pause_s=_parse_float(
            _get("H2_CANDLE_REQUEST_PAUSE_S"), 0.5, "H2_CANDLE_REQUEST_PAUSE_S"
        ),
        candle_page_size=_parse_int(
            _get("H2_CANDLE_PAGE_SIZE"), 1000, "H2_CANDLE_PAGE_SIZE"
        ),
        candle_max_pages=_parse_int(
            _get("H2_CANDLE_MAX_PAGES"), 10, "H2_CANDLE_MAX_PAGES"
        ),
        bybit_timeout_s=_parse_float(
            _get("H2_BYBIT_TIMEOUT_S"), 15.0, "H2_BYBIT_TIMEOUT_S"
        ),
        observation_windows=windows,
        policy_fingerprint_pins=pins,
        coverage_finalize_after_attempts=_parse_int(
            _get("H2_PROSPECTIVE_COVERAGE_FINALIZE_AFTER_ATTEMPTS"), 3,
            "H2_PROSPECTIVE_COVERAGE_FINALIZE_AFTER_ATTEMPTS",
        ),
    )

    # Review §7: prospective accumulation with UNFROZEN policy fingerprints
    # must never start by accident — it needs a deliberate permission (pins
    # or an explicit opt-in), otherwise fail closed.
    if (
        config.enabled
        and not config.policy_fingerprint_pins
        and not config.allow_unfrozen_policies
    ):
        raise H2ProspectiveConfigError(
            "H2_PROSPECTIVE_POLICY_FINGERPRINT_PINS is empty while the "
            "runtime is enabled: pin the approved fingerprints or set "
            "H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES=1 for explicit pre-freeze "
            "research (fail closed)"
        )
    return config
