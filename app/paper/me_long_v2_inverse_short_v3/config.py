"""ME_LONG_V2_INVERSE_SHORT_V3 strict guard configuration (Issue #285).

The Issue #285 entry guards are FROZEN PROTOCOL parameters:

    max_open_positions = 12                (OPEN V2 + OPEN V3, both contours)
    single_active_symbol = True            (cross V2 + V3)
    symbol_entry_cooldown_hours = 12       (from the last COMMITTED entered_at)

and their values are part of the V3 ``execution_spec()`` — and therefore of
the V3 protocol fingerprint (the runtime refuses to run when the frozen
activation fingerprint does not match ``protocol_fingerprint()``).

This module is the STRICT configuration gate for those values — the same
deterministic-config contract the rest of the repository uses
(``app.config.load_settings`` parses ``config.yaml`` with ``json.loads`` and
never a permissive YAML/JSON loader):

* the config file is parsed STRICTLY as JSON (``json.loads`` — a document
  this loader does not understand is a hard error, never a silent default);
* UNKNOWN keys are rejected explicitly — a typo can never silently disable
  a guard;
* explicit env overrides (``ME_INVERSE_SHORT_V3_MAX_OPEN_POSITIONS`` etc.)
  are accepted ONLY when they equal the frozen values — the guards are
  fingerprint-covered, so any deviation is a GUARD_MISMATCH and aborts the
  startup instead of running with split-brain parameters.

Defaults equal the frozen protocol values, so an absent config file is the
documented production state.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.me_inverse_short_v3.json"

CONFIG_KEY_MAX_OPEN = "max_open_positions"
CONFIG_KEY_SINGLE_SYMBOL = "single_active_symbol"
CONFIG_KEY_COOLDOWN_HOURS = "symbol_entry_cooldown_hours"
CONFIG_KEYS = frozenset(
    {CONFIG_KEY_MAX_OPEN, CONFIG_KEY_SINGLE_SYMBOL, CONFIG_KEY_COOLDOWN_HOURS}
)

ENV_KEY_MAX_OPEN = "ME_INVERSE_SHORT_V3_MAX_OPEN_POSITIONS"
ENV_KEY_SINGLE_SYMBOL = "ME_INVERSE_SHORT_V3_SINGLE_ACTIVE_SYMBOL"
ENV_KEY_COOLDOWN_HOURS = "ME_INVERSE_SHORT_V3_SYMBOL_ENTRY_COOLDOWN_HOURS"


class V3ConfigError(ValueError):
    """Invalid V3 guard configuration — fail closed, never a silent fix."""


@dataclass(frozen=True)
class V3GuardConfig:
    max_open_positions: int = 12
    single_active_symbol: bool = True
    symbol_entry_cooldown_hours: int = 12

    def as_dict(self) -> dict[str, Any]:
        return {
            CONFIG_KEY_MAX_OPEN: self.max_open_positions,
            CONFIG_KEY_SINGLE_SYMBOL: self.single_active_symbol,
            CONFIG_KEY_COOLDOWN_HOURS: self.symbol_entry_cooldown_hours,
        }


def _parse_bool(raw: Any, name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise V3ConfigError(f"CONFIG_VALUE_TYPE:{name} must be a strict bool")


def _parse_positive_int(raw: Any, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise V3ConfigError(f"CONFIG_VALUE_TYPE:{name} must be an integer")
    if raw <= 0:
        raise V3ConfigError(f"CONFIG_VALUE_RANGE:{name} must be > 0")
    return raw


def load_v3_guard_config(
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> V3GuardConfig:
    """Load and strictly validate the V3 guard configuration.

    Order of precedence: strict env overrides (only when equal to the frozen
    values) > strict config file > frozen defaults.  Any unknown key, any
    type/range violation and any deviation from the frozen protocol values
    raises :class:`V3ConfigError` — the contour is FROZEN, so there is no
    supported non-default value to configure.
    """
    from .protocol import (
        MAX_OPEN_POSITIONS_TOTAL,
        SINGLE_ACTIVE_SYMBOL,
        SYMBOL_ENTRY_COOLDOWN_HOURS,
    )

    frozen = {
        CONFIG_KEY_MAX_OPEN: MAX_OPEN_POSITIONS_TOTAL,
        CONFIG_KEY_SINGLE_SYMBOL: SINGLE_ACTIVE_SYMBOL,
        CONFIG_KEY_COOLDOWN_HOURS: SYMBOL_ENTRY_COOLDOWN_HOURS,
    }

    raw: dict[str, Any] = {}
    config_path = Path(path) if path is not None else Path(DEFAULT_CONFIG_PATH)
    if config_path.exists():
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise V3ConfigError(
                f"CONFIG_PARSE:{config_path}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise V3ConfigError("CONFIG_SHAPE: top level must be an object")
        unknown = sorted(set(parsed) - CONFIG_KEYS)
        if unknown:
            raise V3ConfigError(f"UNKNOWN_CONFIG_KEYS:{','.join(unknown)}")
        raw.update(parsed)

    values = {
        CONFIG_KEY_MAX_OPEN: _parse_positive_int(
            raw.get(CONFIG_KEY_MAX_OPEN, frozen[CONFIG_KEY_MAX_OPEN]),
            CONFIG_KEY_MAX_OPEN,
        ),
        CONFIG_KEY_SINGLE_SYMBOL: _parse_bool(
            raw.get(CONFIG_KEY_SINGLE_SYMBOL, frozen[CONFIG_KEY_SINGLE_SYMBOL]),
            CONFIG_KEY_SINGLE_SYMBOL,
        ),
        CONFIG_KEY_COOLDOWN_HOURS: _parse_positive_int(
            raw.get(CONFIG_KEY_COOLDOWN_HOURS, frozen[CONFIG_KEY_COOLDOWN_HOURS]),
            CONFIG_KEY_COOLDOWN_HOURS,
        ),
    }

    import os
    env = os.environ if environ is None else environ
    env_overrides = {
        CONFIG_KEY_MAX_OPEN: env.get(ENV_KEY_MAX_OPEN),
        CONFIG_KEY_SINGLE_SYMBOL: env.get(ENV_KEY_SINGLE_SYMBOL),
        CONFIG_KEY_COOLDOWN_HOURS: env.get(ENV_KEY_COOLDOWN_HOURS),
    }
    for key, env_raw in env_overrides.items():
        if env_raw is None:
            continue
        if key == CONFIG_KEY_SINGLE_SYMBOL:
            if env_raw not in ("0", "1"):
                raise V3ConfigError(
                    f"CONFIG_VALUE_TYPE:{ENV_KEY_SINGLE_SYMBOL} must be '0' or '1'"
                )
            parsed_value: Any = env_raw == "1"
        else:
            if not env_raw.isdigit():
                raise V3ConfigError(
                    f"CONFIG_VALUE_TYPE:{key} must be a positive integer"
                )
            parsed_value = int(env_raw)
        values[key] = parsed_value

    # The guards are part of the frozen protocol fingerprint: any deviation
    # from the frozen values is a GUARD_MISMATCH — fail closed, never run
    # with parameters that disagree with the fingerprint.
    for key, value in values.items():
        if value != frozen[key]:
            raise V3ConfigError(
                f"GUARD_MISMATCH:{key}={value} != frozen {frozen[key]} "
                f"(fingerprint-covered protocol value; the V3 contour is "
                f"frozen and does not support re-tuning)"
            )

    config = V3GuardConfig(**values)
    logger.debug("me inverse short v3 guard config: %s", config.as_dict())
    return config
