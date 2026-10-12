
from types import SimpleNamespace

from paper_runner import _filter_paper_entry_candidates


def test_me_v1_long_never_reaches_paper_entry():
    blocked = SimpleNamespace(
        setup_id='b01-blocked-test',
        scanner_name='momentum_exhaustion_reverse_long_v1'.upper(),
        direction='long'.upper(),
    )
    ordinary = SimpleNamespace(
        scanner_name='trend_pullback_v2'.upper(),
        direction='long'.upper(),
    )

    allowed, disabled = _filter_paper_entry_candidates(
        [blocked, ordinary]
    )

    assert disabled == 1
    assert allowed == [ordinary]
