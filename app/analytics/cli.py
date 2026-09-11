from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from typing import Optional

from app.analytics.runner import AnalyticsRunner
from app.analytics.retention import CandleRetention
from app.config import Settings, load_settings

logger = logging.getLogger(__name__)


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
    
    # Load settings
    settings = load_settings()
    
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