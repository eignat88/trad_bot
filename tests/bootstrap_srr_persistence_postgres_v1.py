"""Test-only disposable PostgreSQL bootstrap for SRR persistence integration tests.

This module never creates or drops a database. The operator must create the
new disposable database first; this bootstrap applies repository migrations
008, 035, 049, 050, 056, 060, 061, and 062 to that database only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tests.srr_postgres_test_safety import (
    SrrPostgresTestConfig,
    SrrPostgresTestSafetyError,
    apply_migration_sequence,
    close_bootstrap_connection,
    load_srr_postgres_test_config,
    open_bootstrap_connection,
    validate_bootstrap_database,
    verify_post_bootstrap,
)

SRR_EXPERIMENT_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"


def bootstrap_srr_persistence_database(
    *,
    config: SrrPostgresTestConfig | None = None,
    repository_root: Any = None,
    connect_factory: Any = None,
) -> dict[str, Any]:
    """Apply the exact repository migration chain and verify its objects."""
    if config is None:
        config = load_srr_postgres_test_config()
    validate_bootstrap_database(config.database)
    if connect_factory is None:
        import pg8000

        connect_factory = pg8000.connect
    if repository_root is None:
        repository_root = Path(__file__).resolve().parent.parent

    conn = open_bootstrap_connection(config, connect_factory)
    try:
        applied = apply_migration_sequence(conn, repository_root)
        verification = verify_post_bootstrap(conn)
    finally:
        close_bootstrap_connection(conn)

    return {
        "database": config.database,
        "host": config.host,
        "port": config.port,
        "applied_migrations": applied,
        "verification": verification,
    }


if __name__ == "__main__":
    # The CLI is intentionally fail-closed: it refuses to run without the
    # explicit disposable database and explicit opt-in.
    if os.getenv("SRR_PERSISTENCE_POSTGRES_IT") != "1":
        raise SystemExit("STOP: SRR_PERSISTENCE_POSTGRES_IT=1 is required")
    config = load_srr_postgres_test_config()
    print(bootstrap_srr_persistence_database(config=config))
