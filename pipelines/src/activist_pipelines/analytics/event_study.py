"""
Event-study analytics engine for Activist Terminal.

Methodology:
- Estimation window: [-280, -30] trading days (min 150 obs)
- Market model: OLS log returns vs SPY benchmark
- Fallback: benchmark-adjusted (raw return - benchmark return)
- Windows computed: pre (-30/-90/-180), announcement (-1/+1, -5/+5),
  post (+30/+90/+180/+365)
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ESTIMATION_START = -280   # trading days before anchor
ESTIMATION_END   = -30    # trading days before anchor
MIN_OBS          = 150
BENCHMARK        = "SPY"

# (window_start, window_end) pairs to compute
EVENT_WINDOWS: list[tuple[int, int]] = [
    (-180, 0),
    (-90,  0),
    (-30,  0),
    (-1,   1),
    (-5,   5),
    (0,   30),
    (0,   90),
    (0,  180),
    (0,  365),
]


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class MarketModelParams:
    alpha: float
    beta: float
    r_squared: float
    n_obs: int
    method: str = "market_model"


@dataclass
class WindowResult:
    window_start: int
    window_end: int
    car: Optional[float]
    bhar: Optional[float]
    n_obs: int
    method: str


@dataclass
class EventStudyResult:
    campaign_id: str
    anchor_event_id: str
    anchor_date: date
    benchmark: str
    model: MarketModelParams
    windows: list[WindowResult] = field(default_factory=list)
    inputs_hash: str = ""


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

class EventStudyEngine:
    """
    Computes cumulative abnormal returns (CAR) and buy-and-hold
    abnormal returns (BHAR) for a campaign around an anchor event.
    """

    def __init__(self, engine: Engine, benchmark: str = BENCHMARK):
        self.engine = engine
        self.benchmark = benchmark

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(
        self,
        campaign_id: str,
        anchor_event_id: str,
        anchor_date: date,
        ticker: str,
    ) -> EventStudyResult:
        """Run the event study for one anchor event."""
        logger.info(
            "event_study.compute campaign=%s anchor=%s ticker=%s",
            campaign_id, anchor_event_id, ticker,
        )

        # Load price series
        stock_returns = self._load_returns(ticker, anchor_date)
        bench_returns = self._load_returns(self.benchmark, anchor_date)

        if stock_returns.empty or bench_returns.empty:
            raise ValueError(f"No price data for {ticker} or {self.benchmark}")

        # Align
        aligned = stock_returns.to_frame("stock").join(
            bench_returns.to_frame("bench"), how="inner"
        )

        # Build inputs hash for incremental gating
        inputs_hash = self._hash_inputs(ticker, anchor_date, aligned)

        # Estimation window (trading-day offsets)
        est = aligned.query(f"offset >= {ESTIMATION_START} and offset <= {ESTIMATION_END}")

        if len(est) >= MIN_OBS:
            model = self._fit_market_model(est)
        else:
            logger.warning(
                "Insufficient estimation window obs=%d < %d; using benchmark-adjusted",
                len(est), MIN_OBS,
            )
            model = MarketModelParams(alpha=0.0, beta=1.0, r_squared=0.0,
                                      n_obs=len(est), method="benchmark_adjusted")

        # Compute windows
        windows = []
        for ws, we in EVENT_WINDOWS:
            win = aligned.query(f"offset >= {ws} and offset <= {we}")
            result = self._compute_window(win, model, ws, we)
            windows.append(result)

        return EventStudyResult(
            campaign_id=campaign_id,
            anchor_event_id=anchor_event_id,
            anchor_date=anchor_date,
            benchmark=self.benchmark,
            model=model,
            windows=windows,
            inputs_hash=inputs_hash,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_returns(self, ticker: str, anchor_date: date) -> pd.Series:
        """
        Load log returns with trading-day offset relative to anchor_date.
        Joins price_bars or benchmark_returns depending on ticker.
        """
        if ticker == self.benchmark:
            sql = text("""
                SELECT trading_date,
                       log_return::double precision AS log_return
                FROM app.benchmark_returns
                WHERE ticker = :ticker
                ORDER BY trading_date
            """)
        else:
            sql = text("""
                SELECT pb.trading_date,
                       LN(pb.adj_close / LAG(pb.adj_close) OVER (ORDER BY pb.trading_date))
                           AS log_return
                FROM app.price_bars pb
                JOIN app.companies c ON c.id = pb.company_id
                WHERE c.ticker = :ticker
                ORDER BY pb.trading_date
            """)

        with self.engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"ticker": ticker}, parse_dates=["trading_date"])

        df = df.dropna(subset=["log_return"])
        df = df.sort_values("trading_date").reset_index(drop=True)

        # Assign trading-day offset relative to anchor
        anchor_idx = df["trading_date"].searchsorted(pd.Timestamp(anchor_date))
        df["offset"] = np.arange(len(df)) - anchor_idx
        df = df.set_index("offset")["log_return"]
        return df

    @staticmethod
    def _fit_market_model(est: pd.DataFrame) -> MarketModelParams:
        """OLS: stock_return = alpha + beta * bench_return"""
        slope, intercept, r_value, _p, _se = stats.linregress(
            est["bench"].values, est["stock"].values
        )
        return MarketModelParams(
            alpha=float(intercept),
            beta=float(slope),
            r_squared=float(r_value ** 2),
            n_obs=len(est),
            method="market_model",
        )

    @staticmethod
    def _compute_window(
        win: pd.DataFrame,
        model: MarketModelParams,
        ws: int,
        we: int,
    ) -> WindowResult:
        """Compute CAR and BHAR for a window."""
        if win.empty:
            return WindowResult(window_start=ws, window_end=we,
                                car=None, bhar=None, n_obs=0, method=model.method)

        expected = model.alpha + model.beta * win["bench"]
        abnormal = win["stock"] - expected

        car  = float(abnormal.sum())
        bhar = float((1 + win["stock"]).prod() - (1 + expected).prod())

        return WindowResult(
            window_start=ws,
            window_end=we,
            car=car,
            bhar=bhar,
            n_obs=len(win),
            method=model.method,
        )

    @staticmethod
    def _hash_inputs(ticker: str, anchor_date: date, aligned: pd.DataFrame) -> str:
        payload = json.dumps({
            "ticker": ticker,
            "anchor_date": str(anchor_date),
            "n_rows": len(aligned),
            "first_date": str(aligned.index.min()),
            "last_date": str(aligned.index.max()),
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
