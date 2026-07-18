# Activist Terminal

A production-grade **Shareholder Activism Tracker** — a market-intelligence platform for public-equity activist research. It models the full campaign lifecycle: stake disclosure → demands → escalation → proxy contest → settlement/outcome, and quantifies stock behavior around each milestone using proper event-study methodology (benchmark-adjusted abnormal returns, not naive price deltas).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Next.js](https://img.shields.io/badge/Next.js-15-black)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-blue)](https://postgresql.org/)

## Live Demo

[https://grisheet.github.io/activist-terminal](https://grisheet.github.io/activist-terminal)

## What This Platform Answers

- Which activist investors are currently active?
- Which companies are being targeted?
- What demands are activists making?
- Which campaigns became proxy fights?
- How many board seats were won, settled, or lost?
- How did the stock perform before the campaign, around the announcement, and afterward?
- Which activists have the best hit rates by campaign type, sector, or outcome?

## Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 15 (App Router, TypeScript, RSC) |
| App DB | Prisma + PostgreSQL 16 (`app` schema) |
| Pipeline DB | SQLAlchemy Core + Alembic (`staging` schema) |
| Analytics API | FastAPI (Python 3.12) |
| Charts | lightweight-charts |
| URL state | nuqs |

## Architecture Overview

```
activist-terminal/
├── docker-compose.yml          # Postgres + services
├── Makefile                    # dev shortcuts
├── .env.example
├── web/                        # Next.js app
│   ├── prisma/
│   │   ├── schema.prisma       # 21-table app schema
│   │   └── migrations/
│   └── src/
│       ├── app/                # App Router pages
│       ├── components/         # UI components
│       ├── server/queries/     # RSC data fetchers
│       └── lib/
├── pipelines/                  # Python analytics engine
│   ├── pyproject.toml
│   ├── alembic/                # staging schema migrations
│   └── src/activist_pipelines/
│       ├── ingest/             # SEC EDGAR fetcher
│       ├── parse/              # filing parser
│       ├── match/              # entity resolution
│       ├── load/               # DB writer
│       ├── analytics/          # event-study engine
│       └── jobs/               # CLI entrypoints
│           ├── ingest_filings.py
│           ├── run_event_study.py
│           └── review_matches.py
│   └── tests/
│       └── test_event_study_golden.py
└── docs/
    └── architecture.md
```

## Key Design Decisions

### Schema Ownership Split
Prisma owns the `app` schema exclusively. Python uses SQLAlchemy Core with `ON CONFLICT` upserts — no SQLAlchemy model declarations. Alembic manages a separate `staging` schema for raw filings, entity-match queues, and job ledgers. This prevents migration drift when two runtimes share one Postgres.

### Events as Source of Truth
`campaign_events` is append-only with provenance (filing URL or accession number) on every row. Campaign status, T0, stake, and seat counts are denormalized onto `campaigns` by a single pipeline writer. You can always replay events if a linking rule changes.

### Entity Resolution Cascade
CIK → CUSIP → normalized-alias exact → trigram fuzzy. Auto-accept ≥0.93 confidence; route 0.80–0.93 to `staging.entity_match_reviews` for human review. Every resolved match writes the raw name back as an alias, so the queue shrinks over time.

### Event-Study Methodology
- Estimation window: [−280, −30] trading days (minimum 150 observations)
- Market model: OLS vs SPY log returns
- Fallback to benchmark-adjusted returns if insufficient history
- Pre-windows (−30/−90/−180) stored unadjusted to reflect analyst-cited facts
- Metrics materialized in `campaign_price_metrics`, keyed by (campaign, anchor_event, benchmark, method, window) and gated by an `inputs_hash` for incremental recomputation

## Database Schema (21 tables)

### Core Entities
- `investors` — activist funds and individuals
- `companies` — target companies with CIK, CUSIP, ticker
- `campaigns` — aggregate root; one per activist–company pair
- `campaign_events` — append-only timeline (disclosure, demand, escalation, outcome)
- `campaign_demands` — structured demands per event
- `proxy_fights` — escalated proxy contests
- `board_seat_changes` — seat wins/losses/resignations

### Market Data
- `price_bars` — daily OHLCV per security
- `campaign_price_metrics` — materialized event-study results
- `benchmark_returns` — SPY/other benchmark daily returns

### Ingestion / Staging
- `staging.raw_filings` — raw SEC EDGAR documents
- `staging.filing_parse_jobs` — parse job ledger
- `staging.entity_match_reviews` — human-review queue

## Getting Started

### Prerequisites
- Node.js 20+
- Python 3.12+
- Docker & Docker Compose
- PostgreSQL 16 (via Docker)

### Setup

```bash
# Clone repo
git clone https://github.com/grisheet/activist-terminal.git
cd activist-terminal

# Copy env file
cp .env.example .env

# Start Postgres
docker-compose up -d db

# Web app setup
cd web
npm install
npx prisma migrate dev
npx prisma generate
npm run dev

# Python pipeline setup
cd ../pipelines
pip install -e ".[dev]"
alembic upgrade head

# Run event-study on seed data
python -m activist_pipelines.jobs.run_event_study --seed
```

### Run Tests

```bash
# Python tests (golden-number event-study suite)
cd pipelines
pytest tests/test_event_study_golden.py -v

# Next.js
cd web
npm test
```

## API Endpoints

The FastAPI service runs on port 8000:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/campaigns` | List campaigns with filters |
| GET | `/v1/campaigns/{id}` | Campaign detail + events |
| GET | `/v1/campaigns/{id}/price-metrics` | Event-study results |
| GET | `/v1/investors` | Investor profiles |
| GET | `/v1/investors/{id}/stats` | Hit rates, win rates |
| POST | `/v1/jobs/ingest` | Trigger filing ingestion |
| POST | `/v1/jobs/event-study` | Trigger event-study compute |
| GET | `/v1/review-queue` | Entity match review items |
| PUT | `/v1/review-queue/{id}` | Submit match decision |

## Analytics: Event-Study Details

The event-study engine computes Cumulative Abnormal Returns (CAR) and Buy-and-Hold Abnormal Returns (BHAR) for each campaign anchor event.

**Windows computed:**
- `[-30, 0]`, `[-90, 0]`, `[-180, 0]` — pre-disclosure drift
- `[-1, +1]`, `[-5, +5]` — announcement window
- `[0, +30]`, `[0, +90]`, `[0, +180]` — post-event drift
- `[0, +365]` — one-year post-event

**Anchor events:**
- First 13D filing
- 13D/A amendment (material stake increase)
- First public demand letter
- Proxy contest filing
- Settlement announcement
- Board seat appointment

## Core Analytical Claims

The platform is built to make these sentences computable from first-class data:

> "Elliott's median +180d CAR on campaigns demanding a sale is +9.4%, vs +2.1% for operational campaigns."

> "62% of campaigns that escalated to a definitive proxy filing ended in settlement before the vote."

> "Board-seat conversion rate for first-time 13D filers is 31% vs 54% for repeat activists."

## Contributing

PRs welcome. Please run the golden-number test suite before submitting:

```bash
cd pipelines && pytest tests/ -v
```

## License

MIT
