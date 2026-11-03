"""BYBIT_SHARED_LIMITER_ALL_CONSUMERS_ALIGNMENT_V1 — Issue #253.

Single canonical source for the *parameters* of the shared Bybit rate
limiter, so that every process which enables ``global_bybit_rate_limiter_``
resolves ONE identical :class:`~app.exchange.global_rate_limiter.LimiterConfig`
— hence one identical state file, lock file and token budget.

Why this exists (Issue #253 root cause)
---------------------------------------
Activation of the shared limiter is resolved from WHATEVER config file a
consumer happens to load (``config.yaml`` for most services,
``scanner_canary.json`` for the Scanner canary). Two consequences:

1. **Activation drift** — the same repository can run with the limiter ON in
   one service and OFF in the others (confirmed on ``trad-bot-01``).
2. **Parameter drift** — if two consumers enable the limiter from different
   config files that carry different ``global_limiter_*`` values, they would
   open the SAME state file with incompatible budgets. Neither process would
   notice: the shared state file carries no configuration identity.

This module closes (2) and makes (1) explicit:

* ``global_limiter_contract_path`` (a new, optional settings key) points at
  ONE JSON file holding the canonical ``global_limiter_*`` parameters.
* Every consumer that configures it resolves the same parameters and the
  same ``global_limiter_state_path`` regardless of its own config file.
* A consumer config that *disagrees* with the contract fails startup with an
  explicit :class:`LimiterContractError` (a ``ValueError``) — there is no
  silent override in either direction and never a silent fallback to
  limiter-OFF.
* Activation flags can NEVER be set by the contract: activation stays a
  per-consumer, per-config-file decision so a staged rollout (one service at
  a time, each behind an operator GO) remains possible.

Layering (Issue #253 §4 — configuration separation)
---------------------------------------------------
* **Git-versioned** — this module, the contract schema and the shipped
  reference contract file.
* **Production runtime** — the deployed ``config.yaml`` /
  ``scanner_canary.json`` (activation + non-limiter settings) and the
  deployed contract file. Never modified by this issue's PR.
* **Staged activation** — flipping ``global_bybit_rate_limiter_enabled``
  per consumer, one at a time, after an operator GO.
* **Rollback** — flipping the flag back / removing the contract path; the
  contract file itself is never mutated at runtime.

The contract is OPTIONAL: without ``global_limiter_contract_path`` the
behaviour is byte-identical to Issue #231 (default-off, config-file
parameters, relative state paths anchored at the repository root). The
runtime cross-process guard in ``GlobalBybitRateLimiter`` (state
``config_fingerprint``) still fail-closes any residual parameter mismatch
between two processes sharing one state file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: Repo-root anchor for a RELATIVE contract path — deliberately the same
#: rule as ``settings.resolve_limiter_state_path`` (the repository root
#: derived from this module's location, never ``os.getcwd()``), so every
#: process resolves the same contract file no matter where it was started.
_CONTRACT_ANCHOR = Path(__file__).resolve().parents[2]

#: Settings key naming the canonical contract file. Empty (the default)
#: disables contract alignment entirely (legacy Issue #231 behaviour).
CONTRACT_PATH_KEY = "global_limiter_contract_path"

#: Keys a contract may never carry. Activation is a per-consumer decision
#: (staged rollout) and the contract path itself would be self-referential.
FORBIDDEN_CONTRACT_KEYS = frozenset({
    "global_bybit_rate_limiter_enabled",
    "global_bybit_rate_limiter_allow_env",
    CONTRACT_PATH_KEY,
})


class LimiterContractError(ValueError):
    """Explicit, fail-closed configuration error (never a silent fallback).

    Subclasses ``ValueError`` so every existing ``load_settings()`` failure
    contract (abort startup before any ``BybitClient`` exists) applies
    unchanged.
    """


def limiter_param_keys() -> tuple[str, ...]:
    """Every ``global_limiter_*`` SETTINGS key except the contract path.

    Derived from the ``Settings`` dataclass so a newly added parameter is
    automatically part of the contract vocabulary (and of the unknown-key
    rejection) — the two lists cannot drift.
    """
    from app.config.settings import Settings

    return tuple(sorted(
        name for name in Settings.__dataclass_fields__
        if name.startswith("global_limiter_") and name != CONTRACT_PATH_KEY
    ))


def resolve_limiter_contract_path(value: Any) -> str:
    """Normalise a configured contract path (``""`` = alignment disabled).

    Purely lexical (no I/O): safe to call while the limiter is disabled.
    A relative path is anchored at the repository root — never the process
    WorkingDirectory — exactly like the shared state path, so all consumers
    read the same file.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} must be a string (path or empty string); "
            f"got {type(value).__name__} {value!r}"
        )
    stripped = value.strip()
    if not stripped:
        return ""
    if "\x00" in stripped:
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} must not contain NUL bytes; got {value!r}"
        )
    normalised = os.path.normpath(stripped)
    if os.path.isabs(normalised):
        return normalised
    return os.path.normpath(os.path.join(str(_CONTRACT_ANCHOR), normalised))


def _same_value(left: Any, right: Any) -> bool:
    """Semantic equality for JSON-config values (numbers, sequences, text)."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        try:
            lf, rf = float(left), float(right)
        except (TypeError, ValueError, OverflowError):
            return False
        if not (lf == lf and rf == rf):            # NaN never matches
            return False
        return lf == rf
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (len(left) == len(right)
                and all(_same_value(a, b) for a, b in zip(left, right)))
    return left == right


def load_limiter_contract(path: str) -> dict[str, Any]:
    """Load and STRICTLY validate the canonical contract file.

    Fail-closed on every problem — an unreadable, malformed, unknown-keyed
    or out-of-range contract aborts startup instead of quietly falling back
    to per-consumer defaults (which is exactly the misalignment this issue
    exists to remove).
    """
    from app.exchange.global_rate_limiter import LimiterConfig

    contract_path = Path(path)
    try:
        text = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} {contract_path} could not be read "
            f"({type(exc).__name__}) — the shared limiter contract must be "
            "readable by every consumer (fail-closed, no parameters applied)"
        ) from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} {contract_path} is not valid JSON "
            f"({exc}) — fail-closed"
        ) from exc
    if not isinstance(doc, dict):
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} {contract_path} must contain a JSON object "
            f"of {CONTRACT_PATH_KEY} parameters; got {type(doc).__name__}"
        )

    allowed = set(limiter_param_keys())
    forbidden = sorted(key for key in doc if key in FORBIDDEN_CONTRACT_KEYS)
    if forbidden:
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} {contract_path} must not carry activation "
            f"keys {forbidden} — activation stays a per-consumer decision "
            "(staged rollout); the contract only fixes the shared parameters"
        )
    # Keys prefixed with "_" are documentation comments (e.g. "_comment")
    # and are deliberately ignored — everything else must be a real
    # parameter, so a typo like "global_limiter_burstt" can never be
    # silently dropped from the shared contract.
    unknown = sorted(key for key in doc
                     if key not in allowed and not key.startswith("_"))
    if unknown:
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} {contract_path} contains unknown key(s) "
            f"{unknown}; allowed: {list(limiter_param_keys())}"
        )
    if "global_limiter_state_path" not in doc:
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} {contract_path} must declare "
            "'global_limiter_state_path' — a contract that does not pin the "
            "shared state path cannot guarantee that all consumers use ONE "
            "limiter state (fail-closed)"
        )

    # Single source of truth for the parameter contract: run the SAME strict
    # validation load_settings() runs (types, NaN/Inf, ranges, live-mode
    # absolute path) so a bad value is reported with its exact key name.
    try:
        LimiterConfig.from_settings(_Namespace(**doc))
    except ValueError as exc:
        raise LimiterContractError(
            f"{CONTRACT_PATH_KEY} {contract_path} is invalid: {exc}"
        ) from exc
    return dict(doc)


class _Namespace:
    """Minimal attribute view over the contract dict for from_settings()."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


def apply_limiter_contract(values: dict[str, Any],
                           raw: dict[str, Any]) -> dict[str, Any]:
    """Merge the canonical contract into the resolved settings (in place).

    ``values`` is ``load_settings()``'s resolved field dict, ``raw`` the
    literal config-file content (used to detect an explicit disagreement).

    Rules:
      * no ``global_limiter_contract_path`` configured -> no-op (legacy);
      * contract value for key K wins when the config file does not set K;
      * config file and contract DISAGREEING on K -> :class:`LimiterContractError`;
      * an equivalent config value (e.g. ``8`` vs ``8.0``) -> accepted.
    """
    configured = resolve_limiter_contract_path(
        values.get(CONTRACT_PATH_KEY, raw.get(CONTRACT_PATH_KEY, "")))
    if not configured:
        return values
    contract = load_limiter_contract(configured)
    for key, contract_value in contract.items():
        if key.startswith("_"):
            continue                      # documentation comment, not a value
        if key in raw and not _same_value(raw[key], contract_value):
            raise LimiterContractError(
                f"{key} is set to {raw[key]!r} by this consumer's config "
                f"file but the shared limiter contract {configured} requires "
                f"{contract_value!r} — every consumer of one shared limiter "
                "state must use identical parameters (fail-closed: no silent "
                "override, no fallback to limiter-OFF). Align the config "
                "file with the contract or remove the key from it."
            )
        values[key] = contract_value
    return values


__all__ = [
    "CONTRACT_PATH_KEY",
    "FORBIDDEN_CONTRACT_KEYS",
    "LimiterContractError",
    "apply_limiter_contract",
    "limiter_param_keys",
    "load_limiter_contract",
    "resolve_limiter_contract_path",
]
