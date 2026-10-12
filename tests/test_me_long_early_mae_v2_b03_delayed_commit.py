import inspect

from app.research.me_r_long_early_mae_v2.repository import MeEarlyMaeV2Repository


def test_source_availability_is_written_after_source_commit():
    code = inspect.getsource(MeEarlyMaeV2Repository.insert_source_with_variants)

    first_commit = code.index("self._conn.commit()")
    availability_update = code.index("SET source_available_at = clock_timestamp()")
    second_commit = code.index("self._conn.commit()", availability_update)

    assert first_commit < availability_update < second_commit
    assert "source_available_at IS NULL" in code


def test_source_registration_validates_all_variants_before_commit():
    code = inspect.getsource(MeEarlyMaeV2Repository.insert_source_with_variants)

    validation = code.index("INCOMPLETE_VARIANT_REGISTRATION")
    commit = code.index("self._conn.commit()")

    assert validation < commit
