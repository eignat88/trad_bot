import tempfile

from app.research.htf_baseline_short_phase2_bybit_candle_source_v1 import (
    BybitPhase2CandleSource,
    BAR_MS,
)
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    IncrementalPhase2CandleSource,
)

BAR = BAR_MS
clock = [5001 * BAR]


class FakeHTTP:
    def __init__(self):
        self.pages = 0
        self.windows = []

    def fetch(self, symbol, start_ms, end_ms, *,
              window_end, attempts):
        self.pages += 1
        self.windows.append((start_ms, end_ms))

        rows = []
        for ts in range(start_ms, end_ms + BAR, BAR):
            rows.append([
                str(ts), "100", "102", "98", "101", "5", "505"
            ])

        return list(reversed(rows)), clock[0]


def build_source(http):
    source = BybitPhase2CandleSource(
        object(),
        page_limit=200,
        max_pages=20,
        min_history_bars=3000,
        retry_backoff_s=0,
    )
    source._fetch_server_time_ms = lambda: clock[0]
    source._fetch_page = http.fetch
    return source


def compare(label, expected, actual):
    assert len(actual) >= len(expected), (
        f"{label}: insufficient history"
    )

    comparable = actual[-len(expected):]

    assert expected == comparable, (
        f"{label}: CANDLE_PARITY_FAIL "
        f"expected={len(expected)} "
        f"actual={len(actual)}"
    )

    assert all(
        actual[i].timestamp - actual[i-1].timestamp == BAR
        for i in range(1, len(actual))
    ), f"{label}: NON_CONTIGUOUS_CANDLES"

    print(
        f"{label}: WINDOW_PARITY_PASS "
        f"baseline={len(expected)} "
        f"cache={len(actual)}"
    )


def main():
    with tempfile.TemporaryDirectory(
        prefix="issue226_parity_"
    ) as directory:

        baseline_http = FakeHTTP()
        cached_http = FakeHTTP()

        baseline = build_source(baseline_http)
        upstream = build_source(cached_http)

        cache = IncrementalPhase2CandleSource(
            upstream,
            directory,
            max_incremental_bars=200,
            max_cache_bars=4000,
        )

        # Cycle 1: cold load
        expected = baseline.load("BTCUSDT")
        actual = cache.load("BTCUSDT")

        compare("COLD", expected, actual)

        print("COLD pages:", cached_http.pages)
        assert cached_http.pages == 15

        # Cycle 2: one additional closed candle
        clock[0] += BAR

        baseline_http.pages = 0
        cached_http.pages = 0

        expected = baseline.load("BTCUSDT")
        actual = cache.load("BTCUSDT")

        compare("INCREMENTAL", expected, actual)

        print("BASELINE pages:", baseline_http.pages)
        print("CACHE pages:", cached_http.pages)

        assert cached_http.pages < baseline_http.pages
        assert cached_http.pages <= 1

        # Cycle 3: restart using same cache directory
        cached_http.pages = 0

        restarted = IncrementalPhase2CandleSource(
            build_source(cached_http),
            directory,
            max_incremental_bars=200,
            max_cache_bars=4000,
        )

        actual = restarted.load("BTCUSDT")

        compare("RESTART", expected, actual)

        print("RESTART pages:", cached_http.pages)

        assert cached_http.pages == 0

    print("ISSUE226 INTEGRATION SMOKE PASS")


if __name__ == "__main__":
    main()
