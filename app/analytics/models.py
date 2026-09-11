from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone, date, timedelta
from typing import Any, Optional
from uuid import UUID, uuid4


class Maturity(enum.Enum):
    """Maturity states for analysis runs."""
    PROVISIONAL = "PROVISIONAL"
    FINAL = "FINAL"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RunStatus(enum.Enum):
    """Status of analysis runs."""
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class QualityCheckStatus(enum.Enum):
    """Status for data quality check results.
    
    This is distinct from StageStatus which tracks pipeline stage execution.
    """
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class StageStatus(enum.Enum):
    """Status of analysis pipeline stages (execution tracking)."""
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Severity(enum.Enum):
    """Severity levels for data quality checks."""
    BLOCKING = "BLOCKING"
    DEGRADED = "DEGRADED"
    WARNING = "WARNING"


class QualityStatus(enum.Enum):
    """Quality status for candles."""
    VALIDATED = "validated"
    SUSPECT = "suspect"
    INVALID = "invalid"


@dataclass
class AnalysisRun:
    """Represents a daily analytics pipeline run."""
    run_id: UUID = field(default_factory=uuid4)
    business_date: date = field(default_factory=date.today)
    schedule_timezone: str = "Europe/Sofia"
    analysis_from: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    analysis_to: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    observation_cutoff: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    post_exit_horizon: timedelta = field(default_factory=lambda: timedelta(hours=4))
    maturity: Maturity = Maturity.PROVISIONAL
    status: RunStatus = RunStatus.CREATED
    pipeline_version: str = "1.0.0"
    source_watermarks: dict[str, Any] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self):
        """Validate the analysis run."""
        if self.analysis_from >= self.analysis_to:
            raise ValueError("analysis_from must be before analysis_to")
        
        # Validate error_message doesn't contain secrets
        if self.error_message:
            lower_msg = self.error_message.lower()
            if 'password' in lower_msg or 'secret' in lower_msg or 'token' in lower_msg:
                raise ValueError("error_message must not contain secrets")


@dataclass
class AnalysisStageRun:
    """Represents a single stage execution within an analysis run."""
    stage_run_id: Optional[int] = None
    run_id: UUID = field(default_factory=uuid4)
    stage_name: str = ""
    attempt: int = 1
    status: StageStatus = StageStatus.CREATED
    input_rows: Optional[int] = None
    output_rows: Optional[int] = None
    watermark: Optional[datetime] = None
    result_json: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self):
        """Validate the stage run."""
        if not self.stage_name:
            raise ValueError("stage_name is required")
        
        # Validate error_message doesn't contain secrets
        if self.error_message:
            lower_msg = self.error_message.lower()
            if 'password' in lower_msg or 'secret' in lower_msg or 'token' in lower_msg:
                raise ValueError("error_message must not contain secrets")


@dataclass
class DataQualityResult:
    """Represents a data quality check result."""
    quality_result_id: Optional[int] = None
    run_id: UUID = field(default_factory=uuid4)
    stage_name: str = ""
    check_name: str = ""
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    severity: Severity = Severity.WARNING
    status: QualityCheckStatus = QualityCheckStatus.PASS
    expected_value: Optional[dict[str, Any]] = None
    actual_value: Optional[dict[str, Any]] = None
    affected_entity_count: Optional[int] = None
    affected_entity_ids: Optional[list[Any]] = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: Optional[dict[str, Any]] = None


@dataclass
class Candle:
    """Represents an OHLCV candle."""
    exchange: str = "bybit"
    market_type: str = "linear"
    instrument_id: int = 0
    timeframe: str = "5"
    open_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    close_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    turnover: Optional[float] = None
    is_closed: bool = True
    source: str = "bybit_api"
    source_received_at: Optional[datetime] = None
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    quality_status: QualityStatus = QualityStatus.VALIDATED

    def __post_init__(self):
        """Validate the candle data."""
        # Validate prices are positive
        if self.open <= 0 or self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise ValueError("All prices must be > 0")
        
        # Validate volume is non-negative
        if self.volume < 0:
            raise ValueError("Volume must be >= 0")
        
        # Validate turnover is non-negative if provided
        if self.turnover is not None and self.turnover < 0:
            raise ValueError("Turnover must be >= 0")
        
        # Validate timestamps
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be > open_time")
        
        # Validate OHLC relationships
        if self.high < self.open or self.high < self.close or self.high < self.low:
            raise ValueError("high must be >= open, close, and low")
        
        if self.low > self.open or self.low > self.close:
            raise ValueError("low must be <= open and close")
        
        # Ensure candle is closed for analytics
        self.is_closed = True

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for database insertion."""
        return {
            "exchange": self.exchange,
            "market_type": self.market_type,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "turnover": self.turnover,
            "is_closed": self.is_closed,
            "source": self.source,
            "source_received_at": self.source_received_at,
            "ingested_at": self.ingested_at,
            "quality_status": self.quality_status.value,
        }


@dataclass
class CandleRange:
    """Represents a time range for candle data."""
    instrument_id: int
    timeframe: str
    from_time: datetime
    to_time: datetime

    def __post_init__(self):
        """Validate the range."""
        if self.from_time >= self.to_time:
            raise ValueError("from_time must be before to_time")


@dataclass
class Gap:
    """Represents a gap in candle coverage."""
    instrument_id: int
    timeframe: str
    gap_start: datetime
    gap_end: datetime

    @property
    def duration(self) -> Any:
        """Calculate gap duration."""
        return self.gap_end - self.gap_start


@dataclass
class Watermark:
    """Represents the latest candle timestamp for an instrument."""
    instrument_id: int
    timeframe: str
    latest_candle_time: datetime
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))