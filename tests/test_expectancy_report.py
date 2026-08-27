from unittest.mock import MagicMock, patch

from app.scanners.expectancy_report import _print_table


def test_print_table_empty():
    """Table with no data prints 'no data yet'."""
    _print_table("Empty Report", ["col1", "col2"], [])


def test_print_table_with_rows():
    """Table with data prints all rows."""
    rows = [
        ("TREND_PULLBACK", "LONG", 10, 8, 0.5, 0.4),
        ("BREAKOUT_RETEST", "SHORT", 5, 3, -0.2, -0.25),
    ]
    _print_table("Test Report", ["scanner_name", "direction", "samples", "entries", "avg_r", "avg_r_after_costs"], rows)


def test_print_table_with_none_values():
    """None values render as dash."""
    rows = [
        ("TREND_PULLBACK", "LONG", 3, 2, None, None),
    ]
    _print_table("None Report", ["scanner_name", "direction", "samples", "entries", "avg_r", "avg_r_after_costs"], rows)


class TestExpectancyViewsExist:
    """Verify new expectancy views are created by schema apply."""

    def test_schema_applies_without_error(self):
        from app.db.repository import ScannerRepository
        r = ScannerRepository(backend="postgres")
        try:
            r.ensure_schema()
            cursor = r._conn.cursor()
            cursor.execute(
                "SELECT table_name FROM information_schema.views "
                "WHERE table_schema='dds' AND table_name IN "
                "('scanner_expectancy','scanner_symbol_expectancy','scanner_regime_expectancy','score_bucket_expectancy','scanner_confluence_expectancy') "
                "ORDER BY table_name"
            )
            views = [row[0] for row in cursor.fetchall()]
            assert len(views) == 5
            assert "scanner_expectancy" in views
            assert "scanner_symbol_expectancy" in views
            assert "scanner_regime_expectancy" in views
            assert "score_bucket_expectancy" in views
        finally:
            r.close()
