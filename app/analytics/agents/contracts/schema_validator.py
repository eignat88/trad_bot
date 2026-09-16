"""JSON Schema validation for agent contracts.

Loads schema files from the ``contracts/<version>/`` directory and
validates data dicts against them.  All validation errors are wrapped
into human-readable strings so callers never need to import
``jsonschema`` directly.

Usage::

    from app.analytics.agents.contracts.schema_validator import (
        validate_input,
        validate_specialist_output,
        validate_chief_input,
        validate_chief_output,
    )

    errors = validate_input(data)
    if errors:
        raise ValueError("\\n".join(errors))
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

try:
    import jsonschema  # type: ignore[import-untyped]
except ImportError:
    jsonschema = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Schema directory
# ---------------------------------------------------------------------------

_CONTRACTS_DIR = Path(__file__).resolve().parent

# Lazy-loaded cache: {schema_name: schema_dict}
_schema_cache: Dict[str, Any] = {}


def _load_schema(schema_name: str, version: str = "v1") -> dict:
    """Load and cache a JSON Schema from disk."""
    cache_key = f"{schema_name}:{version}"
    if cache_key in _schema_cache:
        return _schema_cache[cache_key]

    schema_path = _CONTRACTS_DIR / version / f"{schema_name}.schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)

    _schema_cache[cache_key] = schema
    return schema


# ---------------------------------------------------------------------------
# Internal validator
# ---------------------------------------------------------------------------


def _validate(data: dict, schema_name: str, version: str = "v1") -> List[str]:
    """Validate *data* against the named schema.

    Returns
    -------
    list[str]
        A list of human-readable error messages.  Empty when the data
        conforms to the schema.
    """
    if jsonschema is None:
        raise ImportError(
            "The 'jsonschema' package is required for schema validation.  "
            "Install it with:  pip install jsonschema"
        )

    schema = _load_schema(schema_name, version)
    validator = jsonschema.Draft7Validator(schema)

    errors: List[str] = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        # Build a readable path, e.g. "data_quality.status"
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"[{path}] {error.message}")

    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_input(data: dict, schema_version: str = "v1") -> List[str]:
    """Validate an agent input manifest.

    Parameters
    ----------
    data:
        The agent input dict.
    schema_version:
        Schema version directory (default ``"v1"``).

    Returns
    -------
    list[str]
        Error messages; empty list means valid.
    """
    return _validate(data, "agent_input", schema_version)


def validate_specialist_output(data: dict, schema_version: str = "v1") -> List[str]:
    """Validate a specialist agent output.

    Returns
    -------
    list[str]
        Error messages; empty list means valid.
    """
    return _validate(data, "specialist_output", schema_version)


def validate_chief_input(data: dict, schema_version: str = "v1") -> List[str]:
    """Validate the chief agent input.

    Returns
    -------
    list[str]
        Error messages; empty list means valid.
    """
    return _validate(data, "chief_input", schema_version)


def validate_chief_output(data: dict, schema_version: str = "v1") -> List[str]:
    """Validate the chief agent output.

    Returns
    -------
    list[str]
        Error messages; empty list means valid.
    """
    return _validate(data, "chief_output", schema_version)
