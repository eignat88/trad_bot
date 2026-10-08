import pytest

from app.research.srr_short_runtime_wiring import (
    SrrRuntimeWriteBlocked,
    prepare_srr_short_writer,
)


def test_default_mode_exposes_no_writer():
    with prepare_srr_short_writer(
        reader_conn=object(),
        host="127.0.0.1",
        port=5432,
        database="trad_bot_srr_persistence_it_20261008",
        user="postgres",
    ) as writer:
        assert writer is None


def test_write_activation_is_hard_blocked():
    with pytest.raises(
        SrrRuntimeWriteBlocked,
        match="SRR_SHORT_RUNTIME_WRITES_BLOCKED",
    ):
        with prepare_srr_short_writer(
            reader_conn=object(),
            host="127.0.0.1",
            port=5432,
            database="trad_bot_srr_persistence_it_20261008",
            user="postgres",
            enable_writes=True,
        ):
            pass


def test_disabled_mode_does_not_require_database():
    # An intentionally invalid DB endpoint must never be contacted.
    with prepare_srr_short_writer(
        reader_conn=object(),
        host="invalid-host.example.invalid",
        port=5432,
        database="not_a_real_database",
        user="invalid_user",
        enable_writes=False,
    ) as writer:
        assert writer is None
