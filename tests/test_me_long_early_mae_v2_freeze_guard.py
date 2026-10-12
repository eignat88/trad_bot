from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.research.me_r_long_early_mae_v2 import repository
from app.research.me_r_long_early_mae_v2.protocol import (
    validate_activation_freeze,
)


UTC = timezone.utc


def test_unconfigured_freeze_rejects_registration_before_sql():
    assert repository.FROZEN_AT is None

    connection = MagicMock()
    repo = repository.MeEarlyMaeV2Repository(connection)

    with pytest.raises(RuntimeError, match="FREEZE_NOT_CONFIGURED"):
        repo.register_protocol()

    connection.cursor.assert_not_called()
    connection.commit.assert_not_called()
    connection.rollback.assert_not_called()


def test_freeze_rejects_invalid_order():
    now = datetime(2026, 10, 12, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="FREEZE_ORDER_INVALID"):
        validate_activation_freeze(now, now, now)


def test_freeze_rejects_missing_timezone():
    now = datetime(2026, 10, 12, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="FREEZE_TIMEZONE_INVALID"):
        validate_activation_freeze(
            now.replace(tzinfo=None),
            now + timedelta(days=1),
            now,
        )


def test_valid_future_activation_boundary():
    now = datetime(2026, 10, 12, tzinfo=UTC)
    frozen_at = now - timedelta(minutes=1)
    activation_at = now + timedelta(days=1)

    validate_activation_freeze(frozen_at, activation_at, now)



@pytest.mark.parametrize(
    "method_name",
    ["insert_source", "insert_source_with_variants"],
)
def test_unconfigured_freeze_rejects_direct_source_writes(method_name):
    assert repository.FROZEN_AT is None

    connection = MagicMock()
    repo = repository.MeEarlyMaeV2Repository(connection)

    with pytest.raises(RuntimeError, match="FREEZE_NOT_CONFIGURED"):
        getattr(repo, method_name)({})

    connection.cursor.assert_not_called()
    connection.commit.assert_not_called()
    connection.rollback.assert_not_called()
