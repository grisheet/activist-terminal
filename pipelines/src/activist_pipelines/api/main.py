"""
Activist Terminal — FastAPI analytics service.

Internal service for batch ingestion and compute.
Not directly user-facing; proxied by Next.js API routes for client-side needs.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text

app = FastAPI(
    title="Activist Terminal Analytics API",
    version="0.1.0",
    description="Event-study analytics for shareholder activism campaigns",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# DB engine (SQLAlchemy, read-only for API queries)
def get_engine():
    url = os.environ["DATABASE_URL"]
    return create_engine(url, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@app.get("/v1/campaigns")
def list_campaigns(
    status: Optional[str] = None,
    investor_slug: Optional[str] = None,
    sector: Optional[str] = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
):
    engine = get_engine()
    conditions = []
    params: dict = {"limit": limit, "offset": offset}

    if status:
        conditions.append("c.status = :status")
        params["status"] = status
    if investor_slug:
        conditions.append("i.slug = :investor_slug")
        params["investor_slug"] = investor_slug
    if sector:
        conditions.append("co.sector = :sector")
        params["sector"] = sector

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    sql = text(f"""
        SELECT c.id, c.status, c.t0, c.stake_percent,
               c.board_seats_won, c.board_seats_demanded,
               i.slug  AS investor_slug, i.name AS investor_name,
               co.ticker, co.name AS company_name, co.sector
        FROM app.campaigns c
        JOIN app.investors i  ON i.id = c.investor_id
        JOIN app.companies co ON co.id = c.company_id
        {where_clause}
        ORDER BY c.t0 DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()

    return {"campaigns": [dict(r) for r in rows], "total": len(rows)}


@app.get("/v1/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    engine = get_engine()
    with engine.connect() as conn:
        campaign = conn.execute(
            text("""
                SELECT c.*, i.name AS investor_name, i.slug AS investor_slug,
                       co.ticker, co.name AS company_name, co.sector
                FROM app.campaigns c
                JOIN app.investors i  ON i.id = c.investor_id
                JOIN app.companies co ON co.id = c.company_id
                WHERE c.id = :id
            """),
            {"id": campaign_id},
        ).mappings().one_or_none()

        if not campaign:
            raise HTTPException(404, "Campaign not found")

        events = conn.execute(
            text("""
                SELECT * FROM app.campaign_events
                WHERE campaign_id = :id
                ORDER BY event_date
            """),
            {"id": campaign_id},
        ).mappings().all()

    return {"campaign": dict(campaign), "events": [dict(e) for e in events]}


@app.get("/v1/campaigns/{campaign_id}/price-metrics")
def get_price_metrics(campaign_id: str, benchmark: str = "SPY"):
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM app.campaign_price_metrics
                WHERE campaign_id = :id AND benchmark = :benchmark
                ORDER BY anchor_date, window_start
            """),
            {"id": campaign_id, "benchmark": benchmark},
        ).mappings().all()
    return {"metrics": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Investors
# ---------------------------------------------------------------------------

@app.get("/v1/investors")
def list_investors(limit: int = Query(50, le=500), offset: int = 0):
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT i.id, i.slug, i.name, i.fund_type, i.aum,
                       COUNT(c.id) AS total_campaigns,
                       SUM(CASE WHEN c.status = 'won' THEN 1 ELSE 0 END) AS campaigns_won
                FROM app.investors i
                LEFT JOIN app.campaigns c ON c.investor_id = i.id
                GROUP BY i.id
                ORDER BY total_campaigns DESC
                LIMIT :limit OFFSET :offset
            """),
            {"limit": limit, "offset": offset},
        ).mappings().all()
    return {"investors": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

@app.get("/v1/review-queue")
def get_review_queue(limit: int = Query(50, le=200)):
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT * FROM staging.entity_match_reviews
                WHERE resolved_at IS NULL
                ORDER BY confidence DESC
                LIMIT :limit
            """),
            {"limit": limit},
        ).mappings().all()
    return {"items": [dict(r) for r in rows]}


class ReviewDecision(BaseModel):
    accepted: bool
    resolved_company_id: Optional[str] = None
    reviewer_note: Optional[str] = None


@app.put("/v1/review-queue/{item_id}")
def submit_review(item_id: str, decision: ReviewDecision):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE staging.entity_match_reviews
                SET accepted = :accepted,
                    resolved_company_id = :company_id,
                    reviewer_note = :note,
                    resolved_at = NOW()
                WHERE id = :id
            """),
            {
                "id": item_id,
                "accepted": decision.accepted,
                "company_id": decision.resolved_company_id,
                "note": decision.reviewer_note,
            },
        )
    return {"status": "updated"}
