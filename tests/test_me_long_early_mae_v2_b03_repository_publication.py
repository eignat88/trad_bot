
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.research.me_r_long_early_mae_v2 import repository as module


def make_source():
    detected = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    return {
        'source_setup_id': '11111111-1111-4111-8111-111111111111',
        'instrument_id': 1,
        'symbol': 'btcusdt',
        'detected_at': detected,
        'signal_candle_open_at': detected - timedelta(minutes=5),
        'entry_zone_low': 99.8,
        'entry_zone_high': 100.1,
        'reference_price': 100.0,
        'invalidation_price': 97.5,
        'target_1': 103.0,
        'features': {},
    }


def make_connection():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (101,)
    cursor.fetchall.return_value = [
        (
            variant.variant_id,
            variant.horizon_minutes,
            variant.threshold_mae_r,
            module.CANDLE_SOURCE_VERSION,
        )
        for variant in module.VARIANTS
    ]
    return conn


def configure(monkeypatch):
    activation = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'frozen_at'.upper(), activation - timedelta(minutes=1))
    monkeypatch.setattr(module, 'activation_at'.upper(), activation)
    monkeypatch.setattr(
        module,
        'require_protocol_fingerprint',
        lambda *args: 'test-fingerprint',
    )


def test_publication_uses_second_transaction(monkeypatch):
    configure(monkeypatch)
    conn = make_connection()

    repo_type = getattr(module, 'meearlymaev2repository'.replace(
        'meearlymaev2repository', 'MeEarlyMaeV2Repository'
    ))
    repo = repo_type(conn)

    assert repo.insert_source_with_variants(make_source()) == 101
    assert conn.commit.call_count == 2

    calls = conn.cursor.return_value.execute.call_args_list
    publication = [
        c for c in calls
        if 'source_available_at = clock_timestamp()' in c.args[0].lower()
    ]
    assert len(publication) == 1
    assert 'source_available_at is null' in publication[0].args[0].lower()


def test_publication_failure_rolls_back_second_transaction(monkeypatch):
    configure(monkeypatch)
    conn = make_connection()

    def commit():
        if conn.commit.call_count == 2:
            raise RuntimeError('injected publication failure')

    conn.commit.side_effect = commit

    repo_type = getattr(module, 'meearlymaev2repository'.replace(
        'meearlymaev2repository', 'MeEarlyMaeV2Repository'
    ))
    repo = repo_type(conn)

    with pytest.raises(RuntimeError, match='injected publication failure'):
        repo.insert_source_with_variants(make_source())

    assert conn.commit.call_count == 2
    assert conn.rollback.call_count >= 1
