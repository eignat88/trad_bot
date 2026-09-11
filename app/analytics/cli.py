from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Optional

from app.analytics.runner import AnalyticsRunner
from app.analytics.retention import CandleRetention
from app.config import Settings

logger = logging.getLogger(__name__)


def load_analytics_settings(env_file: Optional[str] = None) -> Settings:
    """Load analytics settings from environment only, not from .env file.
    
    This function is designed for systemd execution where:
    - Environment variables are provided by /etc/trad-bot/analytics.env
    - The production .env file is NOT accessible
    - ANALYTICS_ENABLED must be checked before any DB/API initialization
    
    Args:
        env_file: Optional path to .env file (default: None, don't read any .env)
        
    Returns:
        Settings object with analytics configuration
        
    Raises:
        SystemExit: If ANALYTICS_ENABLED is false or invalid
    """
    # Parse ANALYTICS_ENABLED first (fail-fast)
    analytics_enabled_str = os.getenv("ANALYTICS_ENABLED", "false")
    try:
        analytics_enabled = _parse_bool_env(analytics_enabled_str, "ANALYTICS_ENABLED")
    except ValueError as e:
        logger.error("Invalid ANALYTICS_ENABLED value: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    
    if not analytics_enabled:
        logger.info("Analytics pipeline disabled by ANALYTICS_ENABLED=false")
        print("Analytics pipeline disabled by ANALYTICS_ENABLED=false")
        sys.exit(0)
    
    # Load settings from environment only (no .env file)
    # Build settings dict from environment variables with defaults
    env_settings = {
        # Database configuration
        "db_host": os.getenv("DB_HOST", "localhost"),
        "db_port": int(os.getenv("DB_PORT", "5432")),
        "db_name": os.getenv("DB_NAME", "trad_bot"),
        "db_user": os.getenv("DB_USER", "analytics_runner"),
        "db_password": os.getenv("DB_PASSWORD", ""),
        
        # Bybit API (read-only for analytics)
        "bybit_api_key": os.getenv("BYBIT_API_KEY", ""),
        "bybit_api_secret": os.getenv("BYBIT_API_SECRET", ""),
        
        # Analytics-specific settings
        "analytics_schedule_time": os.getenv("ANALYTICS_SCHEDULE_TIME", "06:00"),
        "analytics_timezone": os.getenv("ANALYTICS_TIMEZONE", "Europe/Sofia"),
        "analytics_post_exit_hours": int(os.getenv("ANALYTICS_POST_EXIT_HOURS", "4")),
        "analytics_candle_retention_days": int(os.getenv("ANALYTICS_CANDLE_RETENTION_DAYS", "180")),
        "analytics_candle_workers": int(os.getenv("ANALYTICS_CANDLE_WORKERS", "2")),
        "analytics_api_retry_count": int(os.getenv("ANALYTICS_API_RETRY_COUNT", "3")),
        "analytics_stage_timeout_seconds": int(os.getenv("ANALYTICS_STAGE_TIMEOUT_SECONDS", "3600")),
        "analytics_enabled": analytics_enabled,
        
        # Set safe defaults for other settings (analytics doesn't need scanner/paper settings)
        "trading_mode": "paper",
        "live_trading_enabled": False,
        "symbols": ("BTCUSDT",),
        "category": "linear",
        "timeframe": "5",
        "scanner_workers": 1,
        "scan_interval": 300,
        "signal_conflict_window": 600,
        "initial_balance": 10000.0,
        "risk_per_trade": 0.005,
        "max_open_positions": 3,
        "max_daily_loss": 0.03,
        "max_consecutive_losses": 4,
        "max_symbol_exposure": 0.20,
        "max_portfolio_gross_exposure": 0.60,
        "max_portfolio_net_exposure": 0.40,
        "maker_fee": 0.0002,
        "taker_fee": 0.00055,
        "slippage_percent": 0.0005,
        "atr_period": 14,
        "rsi_period": 14,
        "ma_period": 20,
        "volume_period": 20,
        "atr_stop_multiple": 1.5,
        "reward_risk": 2.0,
        "fomo_price_threshold": 8.0,
        "fomo_oi_threshold": 15.0,
        "fomo_atr_multiple": 2.0,
        "funding_warning": 0.03,
        "funding_block": 0.05,
        "flat_price_threshold": 0.15,
        "bybit_timeout": 15.0,
        "bybit_max_attempts": 3,
        "bybit_retry_backoff": 1.0,
        "paper_scan_interval": 300,
        "setup_ttl_multiplier": 2.0,
        "regime_filter_enabled": True,
        "paper_consecutive_loss_cooldown_minutes": 5,
        "position_monitor_interval": 10,
    }
    
    # Create Settings object
    try:
        settings = Settings(**env_settings)
    except Exception as e:
        logger.error("Failed to create Settings: %s", e)
        print(f"ERROR: Failed to create Settings: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Log effective configuration (without secrets)
    logger.info("Analytics settings loaded:")
    logger.info("  DB_HOST: %s", settings.db_host)
    logger.info("  DB_PORT: %d", settings.db_port)
    logger.info("  DB_NAME: %s", settings.db_name)
    logger.info("  DB_USER: %s", settings.db_user)
    logger.info("  ANALYTICS_ENABLED: %s", settings.analytics_enabled)
    logger.info("  ANALYTICS_SCHEDULE_TIME: %s", settings.analytics_schedule_time)
    logger.info("  ANALYTICS_TIMEZONE: %s", settings.analytics_timezone)
    logger.info("  ANALYTICS_POST_EXIT_HOURS: %d", settings.analytics_post_exit_hours)
    logger.info("  ANALYTICS_CANDLE_RETENTION_DAYS: %d", settings.analytics_candle_retention_days)
    
    return settings


def _parse_bool_env(value: str, env_name: str) -> bool:
    """Parse a boolean environment variable. Fail-fast on invalid values.
    
    Accepted truthy: true, 1, yes, on (case-insensitive).
    Accepted falsy: false, 0, no, off (case-insensitive).
    Anything else raises ValueError to prevent silent misconfiguration.
    """
    normalised = value.strip().lower()
    if normalised in ("true", "1", "yes", "on"):
        return True
    if normalised in ("false", "0", "no", "off"):
        return False
    raise ValueError(
        f"Invalid {env_name}={value!r}. "
        f"Expected one of: true/1/yes/on or false/0/no/off (case-insensitive)."
    )


def main():
    """Main entry point for analytics CLI."""
    parser = argparse.ArgumentParser(description="Analytics Pipeline CLI")
    parser.add_argument(
        "command",
        choices=["run", "finalize", "retention", "status"],
        help="Command to execute",
    )
    parser.add_argument(
        "--date",
        type=str,
        help="Business date (YYYY-MM-DD), default: today",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode (no changes)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    # Load settings from environment only (no .env file)
    # This will exit if ANALYTICS_ENABLED=false
    settings = load_analytics_settings()
    
    # Parse business date
    business_date = None
    if args.date:
        try:
            business_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"Invalid date format: {args.date}. Use YYYY-MM-DD.")
            sys.exit(1)
    
    # Initialize components
    import pg8000
    
    conn = pg8000.connect(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )
    
    from app.analytics.repository import AnalyticsRepository
    from app.analytics.candle_sync import CandleSync
    from app.analytics.quality import DataQualityGate
    from app.exchange.bybit_client import BybitClient
    
    repository = AnalyticsRepository(conn)
    bybit_client = BybitClient(settings)
    candle_sync = CandleSync(bybit_client, repository)
    quality_gate = DataQualityGate(repository)
    retention = CandleRetention(repository, settings.analytics_candle_retention_days)
    
    runner = AnalyticsRunner(
        settings=settings,
        repository=repository,
        candle_sync=candle_sync,
        quality_gate=quality_gate,
        retention=retention,
    )
    
    try:
        if args.command == "run":
            run_provisional(runner, business_date, args.dry_run)
        elif args.command == "finalize":
            run_final(runner, business_date, args.dry_run)
        elif args.command == "retention":
            run_retention(retention, args.dry_run)
        elif args.command == "status":
            show_status(runner, business_date)
    except Exception as e:
        logger.error("Command failed: %s", e)
        sys.exit(1)
    finally:
        conn.close()


def run_provisional(runner: AnalyticsRunner, business_date: Optional[date], dry_run: bool):
    """Run PROVISIONAL analytics."""
    logger.info("Running PROVISIONAL analytics")
    
    if dry_run:
        logger.info("Dry run mode - no changes will be made")
        return
    
    run = runner.run_provisional(business_date)
    
    print(f"PROVISIONAL run completed:")
    print(f"  Run ID: {run.run_id}")
    print(f"  Business Date: {run.business_date}")
    print(f"  Status: {run.status.value}")
    print(f"  Maturity: {run.maturity.value}")
    print(f"  Started: {run.started_at}")
    print(f"  Finished: {run.finished_at}")
    
    if run.error_message:
        print(f"  Error: {run.error_message}")


def run_final(runner: AnalyticsRunner, business_date: Optional[date], dry_run: bool):
    """Run FINAL analytics."""
    logger.info("Running FINAL analytics")
    
    if dry_run:
        logger.info("Dry run mode - no changes will be made")
        return
    
    run = runner.run_final(business_date)
    
    print(f"FINAL run completed:")
    print(f"  Run ID: {run.run_id}")
    print(f"  Business Date: {run.business_date}")
    print(f"  Status: {run.status.value}")
    print(f"  Maturity: {run.maturity.value}")
    print(f"  Started: {run.started_at}")
    print(f"  Finished: {run.finished_at}")
    
    if run.error_message:
        print(f"  Error: {run.error_message}")


def run_retention(retention: CandleRetention, dry_run: bool):
    """Run candle retention."""
    logger.info("Running candle retention")
    
    stats = retention.run_retention(dry_run=dry_run)
    
    print(f"Retention completed:")
    print(f"  Cutoff Date: {stats['cutoff_date']}")
    print(f"  Retention Days: {stats['retention_days']}")
    print(f"  Dry Run: {stats['dry_run']}")
    print(f"  Total Deleted: {stats['total_deleted']}")
    print(f"  Batches Processed: {stats['batches_processed']}")
    
    if stats['errors']:
        print(f"  Errors: {', '.join(stats['errors'])}")


def show_status(runner: AnalyticsRunner, business_date: Optional[date]):
    """Show analytics status."""
    logger.info("Showing analytics status")
    
    from app.analytics.repository import AnalyticsRepository
    
    # Get recent runs
    cursor = runner._repo._conn.cursor()
    cursor.execute(
        """
        SELECT run_id, business_date, maturity, status, started_at, finished_at
        FROM analytics.analysis_run
        ORDER BY business_date DESC, created_at DESC
        LIMIT 10
        """
    )
    
    runs = cursor.fetchall()
    
    if not runs:
        print("No analytics runs found")
        return
    
    print("Recent analytics runs:")
    print("-" * 80)
    print(f"{'Run ID':<36} {'Business Date':<12} {'Maturity':<12} {'Status':<10} {'Started':<20} {'Finished':<20}")
    print("-" * 80)
    
    for run in runs:
        run_id = str(run[0])
        business_date = run[1]
        maturity = run[2]
        status = run[3]
        started_at = run[4].strftime("%Y-%m-%d %H:%M:%S") if run[4] else "N/A"
        finished_at = run[5].strftime("%Y-%m-%d %H:%M:%S") if run[5] else "N/A"
        
        print(f"{run_id:<36} {business_date:<12} {maturity:<12} {status:<10} {started_at:<20} {finished_at:<20}")


if __name__ == "__main__":
    main()