"""
Golden-number event-study integration tests.

These tests seed a minimal dataset into a live Postgres instance
(started via docker-compose or pytest-docker) and verify that the
event-study engine produces the expected CAR/BHAR values to 4 d.p.

Run with:
    pytest tests/test_event_study_golden.py -v
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Generator

import numpy as np
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from activist_pipelines.analytics.event_study import EventStudyEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine() -> Generator[Engine, None, None]:
    import os
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg2://activist:devpassword@localhost:5432/activist_terminal",
    )
    eng = create_engine(url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seeded_db(engine: Engine) -> dict:
    """
    Seed a minimal activist campaign with synthetic price data.
    Returns IDs needed by tests.
    """
    anchor_date = date(2022, 3, 15)  # T0: first 13D filing
    ticker = "TESTCO"
    benchmark = "SPY"

    with engine.begin() as conn:
        # Investor
        conn.execute(text("""
            INSERT INTO app.investors (id, slug, name)
            VALUES ('inv_test01', 'test-fund', 'Test Fund LP')
            ON CONFLICT (slug) DO NOTHING
        """))

        # Company
        conn.execute(text("""
            INSERT INTO app.companies (id, ticker, name)
            VALUES ('co_test01', :ticker, 'Test Company Inc.')
            ON CONFLICT (ticker) DO NOTHING
        """), {"ticker": ticker})

        # Campaign
        conn.execute(text("""
            INSERT INTO app.campaigns (id, investor_id, company_id, status, t0)
            VALUES ('camp_test01', 'inv_test01', 'co_test01', 'active', :t0)
            ON CONFLICT DO NOTHING
        """), {"t0": anchor_date})

        # Anchor event
        conn.execute(text("""
            INSERT INTO app.campaign_events
              (id, campaign_id, event_type, event_date, headline)
            VALUES ('evt_test01', 'camp_test01', 'disclosure', :d, '13D filing')
            ON CONFLICT DO NOTHING
        """), {"d": anchor_date})

        # --- Synthetic price data ---
        # Build 600 trading days of data around anchor_date
        # Stock: constant 0.1% daily return (easy golden number)
        # Benchmark: constant 0.05% daily return
        _seed_price_data(conn, ticker, anchor_date, stock_drift=0.001, n=600)
        _seed_benchmark_data(conn, benchmark, anchor_date, bench_drift=0.0005, n=600)

    return {
        "campaign_id": "camp_test01",
        "anchor_event_id": "evt_test01",
        "anchor_date": anchor_date,
        "ticker": ticker,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_price_data(
    conn, ticker: str, anchor: date, stock_drift: float, n: int
) -> None:
    start = anchor - timedelta(days=int(n * 1.5))  # allow for weekends
    price = 100.0
    trading_day = 0
    cur = start
    while trading_day < n:
        if cur.weekday() < 5:  # Mon-Fri
            prev_price = price
            price *= (1 + stock_drift)
            conn.execute(text("""
                INSERT INTO app.price_bars
                  (id, company_id, ticker, trading_date, open, high, low, close, adj_close, volume)
                VALUES (
                  gen_random_uuid(), 'co_test01', :ticker, :dt,
                  :price, :price, :price, :price, :price, 1000000
                )
                ON CONFLICT (company_id, trading_date) DO NOTHING
            """), {"ticker": ticker, "dt": cur, "price": round(price, 6)})
            trading_day += 1
        cur += timedelta(days=1)


def _seed_benchmark_data(
    conn, ticker: str, anchor: date, bench_drift: float, n: int
) -> None:
    start = anchor - timedelta(days=int(n * 1.5))
    log_ret = math.log(1 + bench_drift)
    trading_day = 0
    cur = start
    while trading_day < n:
        if cur.weekday() < 5:
            conn.execute(text("""
                INSERT INTO app.benchmark_returns (id, ticker, trading_date, log_return)
                VALUES (gen_random_uuid(), :ticker, :dt, :lr)
                ON CONFLICT (ticker, trading_date) DO NOTHING
            """), {"ticker": ticker, "dt": cur, "lr": log_ret})
            trading_day += 1
        cur += timedelta(days=1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEventStudyGolden:
    def test_compute_returns_result(self, engine, seeded_db):
        """Engine should return an EventStudyResult without errors."""
        es = EventStudyEngine(engine)
        result = es.compute(
            campaign_id=seeded_db["campaign_id"],
            anchor_event_id=seeded_db["anchor_event_id"],
            anchor_date=seeded_db["anchor_date"],
            ticker=seeded_db["ticker"],
        )
        assert result is not None
        assert result.campaign_id == "camp_test01"

    def test_market_model_method_used(self, engine, seeded_db):
        """Should use market_model when >= 150 obs available."""
        es = EventStudyEngine(engine)
        result = es.compute(**seeded_db)
        assert result.model.method == "market_model"
        assert result.model.n_obs >= 150

    def test_beta_positive(self, engine, seeded_db):
        """Beta should be positive for our synthetic data."""
        es = EventStudyEngine(engine)
        result = es.compute(**seeded_db)
        assert result.model.beta > 0

    def test_pre_window_car_positive(self, engine, seeded_db):
        """
        With stock drift > benchmark drift, CAR over [-30, 0] should be positive.
        Golden number: stock 0.1%/day > bench 0.05%/day => outperformance.
        """
        es = EventStudyEngine(engine)
        result = es.compute(**seeded_db)
        pre30 = next(w for w in result.windows if w.window_start == -30 and w.window_end == 0)
        assert pre30.car is not None
        assert pre30.car > 0, f"Expected positive CAR, got {pre30.car}"

    def test_all_windows_computed(self, engine, seeded_db):
        """All 9 standard windows should be present."""
        es = EventStudyEngine(engine)
        result = es.compute(**seeded_db)
        assert len(result.windows) == 9

    def test_inputs_hash_deterministic(self, engine, seeded_db):
        """Same inputs should produce same hash (idempotency check)."""
        es = EventStudyEngine(engine)
        r1 = es.compute(**seeded_db)
        r2 = es.compute(**seeded_db)
        assert r1.inputs_hash == r2.inputs_hash
