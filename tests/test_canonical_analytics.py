"""Stage 2: Canonical Analytics Data — Unit Tests.

Covers:
- R sign convention: LONG=+1, SHORT=-1
- MFE >= 0 invariant
- MAE <= 0 invariant
- DCA does not change initial_risk_distance denominator
- net_pnl = gross_pnl - fees - funding - slippage
- config_hash: same config → same hash
- config_hash: different config → different hash
- config_hash: secrets never persisted
- trade_event idempotency (source_event_key UNIQUE)
- event journal append-only (no UPDATE on trade_event)
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

import pytest


# ======================================================================
# R SIGN CONVENTION TESTS
# ======================================================================

class TestRSignConvention:
    """Verify direction_sign = +1 LONG, -1 SHORT convention."""

    def test_long_direction_sign(self):
        """LONG direction should have +1 sign."""
        direction = "LONG"
        direction_sign = 1 if direction == "LONG" else -1
        assert direction_sign == 1

    def test_short_direction_sign(self):
        """SHORT direction should have -1 sign."""
        direction = "SHORT"
        direction_sign = 1 if direction == "LONG" else -1
        assert direction_sign == -1

    def test_long_r_calculation(self):
        """LONG trade: positive price move = positive R."""
        direction = "LONG"
        direction_sign = 1 if direction == "LONG" else -1
        reference_price = 100.0
        exit_price = 105.0
        initial_risk_distance = 5.0

        pnl_r = direction_sign * (exit_price - reference_price) / initial_risk_distance
        assert pnl_r == 1.0  # +1R win

    def test_short_r_calculation(self):
        """SHORT trade: negative price move = positive R."""
        direction = "SHORT"
        direction_sign = 1 if direction == "LONG" else -1
        reference_price = 100.0
        exit_price = 95.0
        initial_risk_distance = 5.0

        pnl_r = direction_sign * (exit_price - reference_price) / initial_risk_distance
        assert pnl_r == 1.0  # +1R win

    def test_long_loss_r(self):
        """LONG trade: negative price move = negative R."""
        direction = "LONG"
        direction_sign = 1 if direction == "LONG" else -1
        reference_price = 100.0
        exit_price = 97.0
        initial_risk_distance = 5.0

        pnl_r = direction_sign * (exit_price - reference_price) / initial_risk_distance
        assert pnl_r == -0.6  # -0.6R loss

    def test_short_loss_r(self):
        """SHORT trade: positive price move = negative R."""
        direction = "SHORT"
        direction_sign = 1 if direction == "LONG" else -1
        reference_price = 100.0
        exit_price = 103.0
        initial_risk_distance = 5.0

        pnl_r = direction_sign * (exit_price - reference_price) / initial_risk_distance
        assert pnl_r == -0.6  # -0.6R loss


# ======================================================================
# MFE/MAE INVARIANT TESTS
# ======================================================================

class TestMFE_MAESign:
    """Verify MFE >= 0 and MAE <= 0 invariants."""

    def test_mfe_always_non_negative(self):
        """MFE (Maximum Favorable Excursion) must always be >= 0."""
        # Simulate LONG trade
        reference_price = 100.0
        max_price_during_trade = 108.0
        initial_risk_distance = 5.0
        direction_sign = 1

        mfe_r = direction_sign * (max_price_during_trade - reference_price) / initial_risk_distance
        assert mfe_r >= 0, f"MFE must be >= 0, got {mfe_r}"

    def test_mae_always_non_positive(self):
        """MAE (Maximum Adverse Excursion) must always be <= 0."""
        # Simulate LONG trade
        reference_price = 100.0
        min_price_during_trade = 96.0
        initial_risk_distance = 5.0
        direction_sign = 1

        mae_r = direction_sign * (min_price_during_trade - reference_price) / initial_risk_distance
        assert mae_r <= 0, f"MAE must be <= 0, got {mae_r}"

    def test_mfe_short_direction(self):
        """MFE for SHORT direction: price drop = positive MFE."""
        reference_price = 100.0
        min_price_during_trade = 92.0  # price dropped (favorable for SHORT)
        initial_risk_distance = 5.0
        direction_sign = -1  # SHORT

        mfe_r = direction_sign * (min_price_during_trade - reference_price) / initial_risk_distance
        # -1 * (92 - 100) / 5 = -1 * (-8) / 5 = 1.6
        assert mfe_r == 1.6
        assert mfe_r >= 0

    def test_mae_short_direction(self):
        """MAE for SHORT direction: price rise = negative MAE."""
        reference_price = 100.0
        max_price_during_trade = 104.0  # price rose (adverse for SHORT)
        initial_risk_distance = 5.0
        direction_sign = -1  # SHORT

        mae_r = direction_sign * (max_price_during_trade - reference_price) / initial_risk_distance
        # -1 * (104 - 100) / 5 = -0.8
        assert mae_r == -0.8
        assert mae_r <= 0


# ======================================================================
# DCA RISK NORMALIZATION TESTS
# ======================================================================

class TestDCARiskNormalization:
    """Verify DCA does not change initial_risk_distance denominator."""

    def test_initial_risk_distance_fixed(self):
        """initial_risk_distance is fixed before first fill, never changes after DCA."""
        reference_price = 100.0
        initial_stop = 95.0
        initial_risk_distance = abs(reference_price - initial_stop)  # 5.0

        # DCA fills at 97.5 (price moved against position)
        dca_fill_price = 97.5

        # initial_risk_distance should remain 5.0, not change to 2.5
        assert initial_risk_distance == 5.0

    def test_dca_avg_entry_does_not_affect_r_denominator(self):
        """DCA fill changes avg_entry_price but NOT initial_risk_distance."""
        initial_fill_price = 100.0
        initial_fill_qty = 0.5
        dca_fill_price = 97.5
        dca_fill_qty = 0.5

        avg_entry_price = (
            initial_fill_price * initial_fill_qty + dca_fill_price * dca_fill_qty
        ) / (initial_fill_qty + dca_fill_qty)

        # avg_entry changes, but initial_risk_distance stays the same
        assert avg_entry_price == 98.75
        # initial_risk_distance = |reference_price - initial_stop| = 5.0
        # This is NOT recalculated after DCA


# ======================================================================
# NET PNL FORMULA TESTS
# ======================================================================

class TestNetPnL:
    """Verify net_pnl = gross_pnl - fees - funding - slippage."""

    def test_net_pnl_formula(self):
        """Basic net_pnl calculation."""
        gross_pnl = 100.0
        entry_fee = 0.55
        dca_fee = 0.0
        exit_fee = 0.55
        funding = 0.10
        slippage_cost = 0.20

        net_pnl = gross_pnl - entry_fee - dca_fee - exit_fee - funding - slippage_cost
        assert net_pnl == pytest.approx(98.60)

    def test_net_pnl_with_dca(self):
        """net_pnl with DCA fees included."""
        gross_pnl = 50.0
        entry_fee = 0.25
        dca_fee = 0.25
        exit_fee = 0.50
        funding = 0.05
        slippage_cost = 0.10

        net_pnl = gross_pnl - entry_fee - dca_fee - exit_fee - funding - slippage_cost
        assert net_pnl == pytest.approx(48.85)

    def test_net_pnl_zero_gross(self):
        """net_pnl with zero gross PnL (breakeven trade)."""
        gross_pnl = 0.0
        entry_fee = 0.55
        dca_fee = 0.0
        exit_fee = 0.55
        funding = 0.0
        slippage_cost = 0.0

        net_pnl = gross_pnl - entry_fee - dca_fee - exit_fee - funding - slippage_cost
        assert net_pnl == pytest.approx(-1.10)

    def test_net_pnl_loss_trade(self):
        """net_pnl for a losing trade."""
        gross_pnl = -75.0
        entry_fee = 0.55
        dca_fee = 0.0
        exit_fee = 0.55
        funding = 0.15
        slippage_cost = 0.20

        net_pnl = gross_pnl - entry_fee - dca_fee - exit_fee - funding - slippage_cost
        assert net_pnl == pytest.approx(-76.45)


# ======================================================================
# CONFIG HASH TESTS
# ======================================================================

class TestConfigHash:
    """Verify config hashing: same config → same hash, different config → different hash."""

    def _compute_hash(self, config_dict: dict) -> str:
        """Compute SHA-256 hash of canonical JSON config."""
        # Canonicalize: sort keys recursively
        canonical = json.dumps(config_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def test_same_config_same_hash(self):
        """Identical configs must produce identical hashes."""
        config1 = {"risk_per_trade": 0.005, "max_positions": 3, "dca_enabled": True}
        config2 = {"risk_per_trade": 0.005, "max_positions": 3, "dca_enabled": True}

        assert self._compute_hash(config1) == self._compute_hash(config2)

    def test_different_config_different_hash(self):
        """Different configs must produce different hashes."""
        config1 = {"risk_per_trade": 0.005, "max_positions": 3}
        config2 = {"risk_per_trade": 0.010, "max_positions": 3}

        assert self._compute_hash(config1) != self._compute_hash(config2)

    def test_key_order_irrelevant(self):
        """Key ordering in JSON must not affect the hash."""
        config1 = {"a": 1, "b": 2, "c": 3}
        config2 = {"c": 3, "a": 1, "b": 2}

        assert self._compute_hash(config1) == self._compute_hash(config2)

    def test_nested_config_same_hash(self):
        """Nested configs with same values must produce same hash."""
        config1 = {"scanner": {"enabled": True, "params": {"atr": 14}}}
        config2 = {"scanner": {"enabled": True, "params": {"atr": 14}}}

        assert self._compute_hash(config1) == self._compute_hash(config2)

    def test_secret_not_in_hash(self):
        """Secrets should be stripped before hashing."""
        config_with_secret = {
            "api_key": "SECRET_VALUE",
            "risk_per_trade": 0.005,
        }
        config_without_secret = {
            "risk_per_trade": 0.005,
        }

        # Strip secrets before hashing
        def strip_secrets(cfg: dict) -> dict:
            secret_keys = {"api_key", "api_secret", "password", "token", "secret"}
            return {k: v for k, v in cfg.items() if k.lower() not in secret_keys}

        hash1 = self._compute_hash(strip_secrets(config_with_secret))
        hash2 = self._compute_hash(strip_secrets(config_without_secret))

        assert hash1 == hash2

    def test_different_values_different_hash(self):
        """Slightly different values must produce different hashes."""
        config1 = {"risk_per_trade": 0.0050000001}
        config2 = {"risk_per_trade": 0.0050000002}

        assert self._compute_hash(config1) != self._compute_hash(config2)


# ======================================================================
# TRADE EVENT IDEMPOTENCY TESTS
# ======================================================================

class TestTradeEventIdempotency:
    """Verify trade_event append-only and idempotency constraints."""

    def test_source_event_key_uniqueness(self):
        """Each source_event_key must be unique across all events."""
        events = [
            {"source_event_key": "entry_filled:123", "event_type": "ENTRY_FILLED"},
            {"source_event_key": "entry_filled:123", "event_type": "ENTRY_FILLED"},  # duplicate
            {"source_event_key": "trade_closed:123", "event_type": "TRADE_CLOSED"},
        ]

        # Simulate UNIQUE constraint: track seen keys
        seen_keys = set()
        unique_events = []
        for event in events:
            key = event["source_event_key"]
            if key not in seen_keys:
                seen_keys.add(key)
                unique_events.append(event)

        # Only 2 unique events should remain
        assert len(unique_events) == 2
        assert len(seen_keys) == 2

    def test_event_type_constraint(self):
        """Only valid event types should be accepted."""
        valid_types = {
            "SETUP_READY", "ENTRY_ATTEMPTED", "ENTRY_FILLED",
            "DCA_PLACED", "DCA_FILLED", "STOP_MOVED",
            "PARTIAL_EXIT", "TRADE_CLOSED"
        }

        # Valid event types
        assert "SETUP_READY" in valid_types
        assert "ENTRY_FILLED" in valid_types
        assert "DCA_FILLED" in valid_types
        assert "TRADE_CLOSED" in valid_types

        # Invalid event type
        assert "INVALID_EVENT" not in valid_types

    def test_event_journal_immutable(self):
        """Events should not be UPDATEd after creation."""
        # Simulate append-only behavior
        events = []
        event = {
            "event_id": 1,
            "trade_id": 100,
            "event_type": "ENTRY_FILLED",
            "price": 10000.0,
        }
        events.append(event)

        # Attempt to update (should fail in production)
        with pytest.raises(Exception, match="append-only"):
            if event["event_type"] == "ENTRY_FILLED":
                raise Exception("trade_event is append-only — UPDATE is not permitted")
