# LedgerFlow

**Track 4: AI Finance Controller — Razorpay AI Buildathon 2026**

> An intelligent, automated financial reconciliation dashboard that eliminates manual matching bottlenecks across ERP ledgers, payment gateway settlements, and bank statements — with real-time metrics, a full audit trail, and a human-in-the-loop exception queue.

---

## 🎥 Demo Video

**👉 [Watch the 5-Minute Demo on Loom](https://www.loom.com/share/dee4d4c6c24f4a3e9f06bb171a842575)**

---

## ✨ What is LedgerFlow?

Finance teams at high-growth companies spend **days every month** manually reconciling three disconnected data sources:

| Source | Description |
|---|---|
| **ERP Ledger** | Internal accounting records (invoices, expected net payables) |
| **Gateway Settlement** | Razorpay settlement reports (actual fees deducted, UTR numbers) |
| **Bank Statement** | Raw bank credits (narrations, value dates, running balances) |

**LedgerFlow automates this entirely.** Upload your three CSVs, and within seconds the dashboard surfaces your match rate, flags every exception with a confidence score and plain-English reasoning, and gives you a complete audit trail — ready for regulatory review.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| **Backend API** | Python · FastAPI · Uvicorn |
| **Reconciliation Engine** | Pandas · Custom two-stage deterministic + AI-assisted pipeline |
| **Frontend Dashboard** | Vanilla HTML/JS · Tailwind CSS · Chart.js |
| **Data Format** | CSV (UTF-8 / Excel BOM-safe) |
| **Containerisation** | Docker · Docker Compose |

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Solution Overview](#solution-overview)
3. [Architecture](#architecture)
4. [Project Structure](#project-structure)
5. [Setup & Execution](#setup--execution)
6. [API Reference](#api-reference)
7. [Anomaly Coverage](#anomaly-coverage)

---

## Problem Statement

Finance teams at high-growth companies process thousands of payment transactions daily across three independent systems:

| Source | Description |
|---|---|
| **ERP Ledger** | Internal accounting records (invoices, expected net payables) |
| **Gateway Settlement** | Razorpay settlement reports (actual fees deducted, UTR numbers) |
| **Bank Statement** | Raw bank credits (narrations, value dates, running balances) |

Reconciling these three sources manually is a **costly, error-prone bottleneck** caused by:

- **Truncated bank narrations** — banks silently cut off UTR numbers or customer names mid-string, breaking exact-match lookups
- **Fee rounding mismatches** — minor gateway fee variances (< ₹5) flagged as critical errors, burying real issues in noise
- **Missing counterparts** — gateway settlements with no corresponding bank credit, or ERP records with no gateway entry
- **Duplicate bank credits** — same UTR appearing twice in the bank export, risking double-booking
- **Gross amount divergence** — payment amendments between ERP booking and gateway processing

The result: reconciliation cycles that should take minutes stretch into days, with finance controllers manually hunting through spreadsheets for evidence they can explain to auditors.

---

## Solution Overview

LedgerFlow solves this with a **two-stage pipeline** that separates provable logic from ambiguous inference:

- **Stage 1 (Deterministic Core):** All math and ID-matching is handled by strict Pandas rules with zero tolerance for guesswork. If the numbers or IDs align exactly, the transaction is auto-reconciled at confidence ≥ 95.

- **Stage 2 (AI-Assisted Fallback):** Only rows that *cannot* be resolved deterministically escalate to a structured reasoning layer — fuzzy UTR extraction from truncated narrations, and amount-corroboration via gateway hints. Every inference is documented with evidence and a plain-English explanation.

- **Audit Trail:** Every single transaction produces a structured decision record (status, sub-status, confidence score, evidence dict, reasoning text) written to a JSON-lines audit log — ready for regulatory review or controller sign-off.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    LedgerFlow Engine                    │
│                  (ledgerflow_engine.py)                 │
│                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │ erp_ledger  │    │  gateway_   │    │    bank_    │  │
│  │   .csv      │    │ settlement  │    │  statement  │  │
│  │             │    │   .csv      │    │    .csv     │  │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘  │
│         └─────────────────┼───────────────────┘         │
│                           │                             │
│                    ┌──────▼──────┐                      │
│                    │  Data Load  │                      │
│                    │  & Indexes  │ ← O(1) dict lookups  │
│                    └──────┬──────┘                      │
│                           │                             │
│              ┌────────────▼────────────┐                │
│              │  STAGE 1: Deterministic │                │
│              │        Core             │                │
│              │  ─────────────────────  │                │
│              │  • ERP math check       │                │
│              │    (Gross−Fee−Tax=Net)  │                │
│              │  • Gateway math check   │                │
│              │  • Gross cross-check    │                │
│              │  • Exact UTR match      │                │
│              │  • Fee delta classify   │                │
│              │  • Duplicate detection  │                │
│              │  • Net vs bank credit   │                │
│              └────────────┬────────────┘                │
│                           │                             │
│               Fully resolved? ──YES──► MATCHED (conf≥95)│
│                           │ NO                          │
│              ┌────────────▼────────────┐                │
│              │  STAGE 2: AI Fallback   │                │
│              │  ─────────────────────  │                │
│              │  • Fuzzy UTR narration  │                │
│              │    extraction (regex)   │                │
│              │  • Prefix/suffix UTR    │                │
│              │    partial match        │                │
│              │  • Amount + hint        │                │
│              │    corroboration        │                │
│              └────────────┬────────────┘                │
│                           │                             │
│         ┌─────────────────┼─────────────────┐          │
│         │                 │                 │           │
│      MATCHED           REVIEW          EXCEPTION        │
│    (conf 72-82)       (AI-assisted)   (escalated)       │
│                                                         │
│  ─────────────────────────────────────────────────────  │
│                   Audit Trail Writer                    │
│         (audit_trail.jsonl + exception_queue.csv)       │
└─────────────────────────────────────────────────────────┘
                           │
              ┌────────────▼────────────┐
              │   FastAPI REST Layer    │
              │      (main.py)          │
              │  POST /api/reconcile    │
              │  GET  /api/audit-trail  │
              │  GET  /api/exception-   │
              │         queue           │
              └────────────┬────────────┘
                           │
              ┌────────────▼────────────┐
              │   Browser Dashboard     │
              │     (index.html)        │
              │  • Metrics cards        │
              │  • Audit trail table    │
              │  • Exception queue      │
              └─────────────────────────┘
```

### Design Principles

| Principle | Implementation |
|---|---|
| **Math first, AI second** | Stage 1 handles 100% of provable rows via strict Pandas arithmetic. Stage 2 only activates for genuinely ambiguous cases. |
| **Confidence scoring** | Every outcome carries a 0–100 confidence score. MATCHED rows score ≥ 95; REVIEW rows (AI-assisted) 72–82; EXCEPTION rows reflect the certainty that the row *cannot* be auto-resolved. |
| **Full explainability** | Each audit record contains a plain-English `reasoning` string and a structured `evidence` dict — no black-box verdicts. |
| **Exception isolation** | Rows that cannot be proved are written to a separate `exception_queue.csv`, never silently discarded. |
| **Idempotent runs** | Re-running the engine overwrites output files deterministically. Results are reproducible given the same input CSVs. |

---

## Project Structure

```
LedgerFlow/
│
├── generate_mock_data.py     # Generates 100 realistic test transactions
│                             # with ~15% intentional anomalies (A1–A7)
│
├── ledgerflow_engine.py      # Core reconciliation engine (standalone)
│                             # Two-stage: DeterministicCore + AIFallback
│
├── main.py                   # FastAPI application wrapping the engine
│                             # Exposes REST endpoints for the dashboard
│
├── index.html                # Single-page browser dashboard
│                             # Fetches /api/reconcile on load
│
├── requirements.txt          # Python dependencies
│
└── data/
    ├── mock/                 # Input CSVs (generated by generate_mock_data.py)
    │   ├── erp_ledger.csv
    │   ├── gateway_settlement.csv
    │   └── bank_statement.csv
    │
    └── output/               # Engine outputs (created on first run)
        ├── reconciliation_report.csv
        ├── exception_queue.csv
        ├── audit_trail.jsonl
        └── run_summary.txt
```

---

## Setup & Execution

### Prerequisites

- Python 3.10 or higher
- pip

### Step 1 — Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Generate Mock Data

This creates the three input CSVs in `data/mock/` with 100 realistic transactions
and ~15% intentional anomalies to stress-test the reconciliation engine.

```bash
python generate_mock_data.py
```

Expected output:

```
------------------------------------------------------------------
  LedgerFlow - Mock Data Generation Report
------------------------------------------------------------------
  Total records generated    : 100
  Clean records              : 85
  Anomalous records          : 15 (15.0%)

  Anomaly breakdown:
    DUPLICATE_BANK_ENTRY                  1 row(s)
    GROSS_AMOUNT_MISMATCH                 2 row(s)
    MAJOR_FEE_MISMATCH                    2 row(s)
    MINOR_FEE_MISMATCH                    3 row(s)
    MISSING_BANK_CREDIT                   2 row(s)
    MISSING_GATEWAY_ENTRY                 2 row(s)
    TRUNCATED_BANK_NARRATION              3 row(s)
```

### Step 3 — (Optional) Run the Engine Directly

You can run the reconciliation engine standalone — useful for batch jobs or CI pipelines:

```bash
python ledgerflow_engine.py
```

Outputs are written to `data/output/`. A human-readable summary is printed to stdout.

### Step 4 — Start the FastAPI Backend

```bash
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`.  
Interactive Swagger docs: `http://localhost:8000/docs`

### Step 5 — Open the Dashboard

Open `index.html` directly in your browser:

```
# Windows
start index.html

# macOS
open index.html

# Or just double-click index.html in your file explorer
```

The dashboard will automatically call `POST /api/reconcile`, run the engine,
and populate the metrics, audit trail, and exception queue.

> **Note:** The FastAPI backend must be running before opening the dashboard.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness probe — confirms all three CSV inputs are present |
| `POST` | `/api/reconcile` | Runs the full engine; returns summary, all row results, exception queue, and audit trail as JSON |
| `GET` | `/api/audit-trail` | Full decision log; filter by `?status=MATCHED\|REVIEW\|EXCEPTION` and cap with `?limit=N` |
| `GET` | `/api/exception-queue` | Reads `exception_queue.csv`; returns rows needing human action with a sub-status breakdown |
| `GET` | `/docs` | Auto-generated Swagger UI |

### Sample Response — `POST /api/reconcile`

```json
{
  "ok": true,
  "summary": {
    "record_counts": { "total": 100, "matched": 92, "review": 1, "exception": 7 },
    "rates": { "match_rate_pct": 92.0, "exception_rate_pct": 7.0 },
    "average_confidence": 98.7,
    "financials": {
      "total_erp_gross_inr": 7578973.78,
      "reconciled_net_inr": 6836093.39
    }
  },
  "results": [ ... ],
  "exception_queue": [ ... ],
  "audit_trail": [ ... ]
}
```

---

## Anomaly Coverage

The mock data generator injects seven anomaly types. The table below shows how the engine handles each:

| Code | Anomaly | Stage | Resolution |
|---|---|---|---|
| **A1** | Truncated bank narration | Stage 2 | Fuzzy UTR prefix/suffix regex match on narration |
| **A2** | Minor fee mismatch (≤ ₹5) | Stage 1 | Flagged `REVIEW` with `MINOR_FEE_VARIANCE`; net amounts reconcile |
| **A3** | Major fee mismatch (> ₹5) | Stage 1 | `EXCEPTION` — requires gateway debit-note investigation |
| **A4** | Missing gateway entry | Stage 1 | `EXCEPTION` — payment may not have been processed |
| **A5** | Missing bank credit | Stage 2 | Fuzzy + hint search exhausted → `EXCEPTION` with `MISSING_BANK` |
| **A6** | Gross amount mismatch | Stage 1 | `EXCEPTION` — possible payment amendment or gateway error |
| **A7** | Duplicate bank credit | Stage 1 | `EXCEPTION` — same UTR credited twice, reversal required |

---

*Built for the Razorpay AI Buildathon 2026 — Track 4: AI Finance Controller*
