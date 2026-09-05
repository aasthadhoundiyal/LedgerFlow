"""
main.py
────────────────────────────────────────────────────────────────────────────────
LedgerFlow — FastAPI Application
Razorpay AI Buildathon 2026 | Track 4: AI Finance Controller

Wraps the core reconciliation engine (ledgerflow_engine.py) as a REST API.

Endpoints
─────────
GET  /api/health
    Lightweight liveness probe.

POST /api/reconcile
    Triggers the full two-stage reconciliation run against the CSV files in
    data/mock/.  Returns the complete summary report, per-row results,
    and the exception queue — all as structured JSON.

POST /api/upload-and-reconcile
    Accepts three CSV files as multipart/form-data (erp_ledger,
    gateway_settlement, bank_statement). Saves them to an isolated temp
    directory, runs the reconciliation engine, returns the full results,
    then cleans up. No pre-existing files required on the server.

GET  /api/audit-trail
    Returns the full decision log from the last /api/reconcile run.
    Supports optional query params: ?status=MATCHED|REVIEW|EXCEPTION
    and ?limit=N to cap result size.

GET  /api/exception-queue
    Returns rows escalated to the exception queue from the last
    /api/reconcile run.

Run with Uvicorn:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Engine imports ────────────────────────────────────────────────────────────
from ledgerflow_engine import (
    OUTPUT_DIR,
    ERP_FILE,
    GATEWAY_FILE,
    BANK_FILE,
    MATH_EPSILON,
    MINOR_FEE_DELTA_LIMIT,
    NET_MATCH_TOLERANCE,
    ReconciliationEngine,
    Status,
    load_data,
)

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ledgerflow.api")

# ─────────────────────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="LedgerFlow Reconciliation API",
    description=(
        "Two-stage financial reconciliation engine: deterministic UTR matching "
        "plus AI-assisted fallback for truncated narrations and minor fee variances."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────────────────────────────────────────────────────────────────
# CORS — allow any frontend origin during development; lock down in production
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # Replace with specific origins in production
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_input_files() -> list[str]:
    """Return a list of missing input file paths (empty = all present)."""
    return [str(f) for f in (ERP_FILE, GATEWAY_FILE, BANK_FILE) if not f.exists()]


def _read_audit_trail() -> list[dict]:
    """
    Read audit_trail.jsonl from disk and return as a list of dicts.
    Returns an empty list if the file doesn't exist yet.
    """
    audit_path = OUTPUT_DIR / "audit_trail.jsonl"
    if not audit_path.exists():
        return []
    records: list[dict] = []
    with open(audit_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _build_summary(recon_df: pd.DataFrame, exc_df: pd.DataFrame, elapsed_s: float) -> dict:
    """
    Construct the structured summary dict returned by POST /api/reconcile.
    Mirrors the human-readable run_summary.txt but in JSON form.
    """
    total     = len(recon_df)
    matched   = int((recon_df["status"] == Status.MATCHED).sum())
    review    = int((recon_df["status"] == Status.REVIEW).sum())
    exception = int((recon_df["status"] == Status.EXCEPTION).sum())

    sub_counts  = recon_df["sub_status"].value_counts().to_dict()
    exc_reasons = exc_df["sub_status"].value_counts().to_dict() if len(exc_df) else {}

    return {
        "run_timestamp":    _utcnow_iso(),
        "elapsed_seconds":  round(elapsed_s, 3),
        "record_counts": {
            "total":     total,
            "matched":   matched,
            "review":    review,
            "exception": exception,
        },
        "rates": {
            "match_rate_pct":     round(matched   / total * 100, 2) if total else 0,
            "review_rate_pct":    round(review    / total * 100, 2) if total else 0,
            "exception_rate_pct": round(exception / total * 100, 2) if total else 0,
        },
        "stage_breakdown": {
            "stage_1_deterministic": int((recon_df["stage"] == 1).sum()),
            "stage_2_ai_fallback":   int((recon_df["stage"] == 2).sum()),
        },
        "average_confidence": round(float(recon_df["confidence"].mean()), 1),
        "sub_status_breakdown": sub_counts,
        "exception_breakdown":  exc_reasons,
        "financials": {
            "total_erp_gross_inr":    round(float(recon_df["erp_gross"].sum()), 2),
            "total_gw_net_inr":       round(float(recon_df.loc[recon_df["gw_net"] > 0, "gw_net"].sum()), 2),
            "total_bank_credits_inr": round(float(recon_df.loc[recon_df["bank_credit"] > 0, "bank_credit"].sum()), 2),
            "reconciled_net_inr":     round(float(recon_df.loc[recon_df["status"] == Status.MATCHED, "erp_net"].sum()), 2),
        },
        "engine_config": {
            "math_epsilon":           MATH_EPSILON,
            "minor_fee_delta_limit":  MINOR_FEE_DELTA_LIMIT,
            "net_match_tolerance":    NET_MATCH_TOLERANCE,
        },
    }


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame to a JSON-serialisable list of dicts."""
    return [
        {k: (None if (isinstance(v, float) and v != v) else v)  # NaN -> None
         for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]


def _run_engine_on_dataframes(
    erp_df: pd.DataFrame,
    gw_df: pd.DataFrame,
    bank_df: pd.DataFrame,
    persist_outputs: bool = False,
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """
    Shared engine execution helper used by both reconciliation endpoints.

    Parameters
    ----------
    erp_df, gw_df, bank_df:
        Pre-loaded DataFrames for the three sources.
    persist_outputs:
        When True, write the standard output CSVs + JSONL to OUTPUT_DIR
        (used by the static /api/reconcile endpoint).
        When False (upload endpoint), results are returned in-memory only.

    Returns
    -------
    (summary, results, exception_queue, audit_trail) -- all JSON-serialisable.
    """
    t_start = time.perf_counter()

    engine = ReconciliationEngine(erp_df, gw_df, bank_df)
    engine.run()

    if persist_outputs:
        engine.write_outputs()

    elapsed = time.perf_counter() - t_start

    recon_df = pd.DataFrame([vars(r) for r in engine.recon_rows])
    exc_df   = recon_df[recon_df["status"] == Status.EXCEPTION].copy()

    summary         = _build_summary(recon_df, exc_df, elapsed)
    results         = _df_to_records(recon_df)
    exception_queue = _df_to_records(exc_df)
    audit_trail_out = [a.to_dict() for a in engine.audit_trail]

    log.info(
        "Engine run complete: %d rows, match_rate=%.1f%%, elapsed=%.3fs",
        len(recon_df),
        summary["rates"]["match_rate_pct"],
        elapsed,
    )
    return summary, results, exception_queue, audit_trail_out


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

# ── Health probe ──────────────────────────────────────────────────────────────

@app.get(
    "/api/health",
    summary="Liveness probe",
    tags=["System"],
)
async def health() -> dict:
    """
    Returns service health and a quick check on whether all three input CSV
    files are present on disk.
    """
    missing = _check_input_files()
    return {
        "status":        "ok",
        "timestamp":     _utcnow_iso(),
        "input_files": {
            "erp_ledger":         str(ERP_FILE),
            "gateway_settlement": str(GATEWAY_FILE),
            "bank_statement":     str(BANK_FILE),
        },
        "input_files_ready": len(missing) == 0,
        "missing_files":     missing,
    }


# ── POST /api/reconcile ───────────────────────────────────────────────────────

@app.post(
    "/api/reconcile",
    summary="Run full reconciliation",
    tags=["Reconciliation"],
    response_description=(
        "Structured JSON containing the summary report, per-row results, "
        "and the exception queue."
    ),
)
async def reconcile() -> JSONResponse:
    """
    Triggers the two-stage LedgerFlow reconciliation engine:

    1. **Stage 1 — Deterministic Core**: exact UTR/invoice-ID matching and
       strict mathematical validation (`Gross − Fee − Tax = Net`).
    2. **Stage 2 — AI Fallback**: fuzzy narration search and amount
       corroboration for rows that Stage 1 cannot fully resolve.

    Returns the full summary, all 100 per-row decisions, and the exception queue.
    All output files (`reconciliation_report.csv`, `exception_queue.csv`,
    `audit_trail.jsonl`, `run_summary.txt`) are also written to `data/output/`.
    """
    # ── Guard: verify input CSVs are present ─────────────────────────────
    missing = _check_input_files()
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error":   "MISSING_INPUT_FILES",
                "message": (
                    "One or more input CSV files are missing. "
                    "Run generate_mock_data.py first."
                ),
                "missing": missing,
            },
        )

    log.info("POST /api/reconcile — starting engine run")

    try:
        erp_df, gw_df, bank_df = load_data()
        summary, results, exc_queue, audit = _run_engine_on_dataframes(
            erp_df, gw_df, bank_df, persist_outputs=True
        )
        return JSONResponse(content={
            "ok":              True,
            "source":          "static_files",
            "summary":         summary,
            "results":         results,
            "exception_queue": exc_queue,
            "audit_trail":     audit,
        })

    except Exception as exc:
        log.exception("POST /api/reconcile — engine error: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "ENGINE_ERROR", "message": str(exc)},
        )


# ── GET /api/audit-trail ──────────────────────────────────────────────────────

@app.get(
    "/api/audit-trail",
    summary="Fetch the full decision log",
    tags=["Reconciliation"],
    response_description=(
        "List of audit records from the most recent reconciliation run, "
        "optionally filtered by status and/or capped at `limit` entries."
    ),
)
async def audit_trail(
    status: Literal["MATCHED", "REVIEW", "EXCEPTION"] | None = Query(
        default=None,
        description="Filter by reconciliation status. Omit to return all records.",
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of records to return (1–5000).",
    ),
) -> JSONResponse:
    """
    Returns the full decision log produced by the most recent
    `POST /api/reconcile` run.

    Every entry carries:
    - `erp_txn_id` — source ERP record
    - `status` — MATCHED | REVIEW | EXCEPTION
    - `sub_status` — granular outcome code (e.g. EXACT_MATCH, FUZZY_UTR_MATCH)
    - `confidence` — engine confidence score (0–100)
    - `stage` — 1 (deterministic) or 2 (AI fallback)
    - `reasoning` — plain-English decision explanation
    - `evidence` — key financial facts that drove the decision
    - `anomaly_hint` — injected anomaly code from test data (if present)
    - `ts` — UTC timestamp of the decision
    """
    records = _read_audit_trail()

    if not records:
        raise HTTPException(
            status_code=404,
            detail={
                "error":   "NO_AUDIT_TRAIL",
                "message": (
                    "No audit trail found. "
                    "Run POST /api/reconcile first to generate one."
                ),
            },
        )

    # Optional status filter
    if status:
        records = [r for r in records if r.get("status") == status]

    # Cap result size
    total_before_limit = len(records)
    records = records[:limit]

    return JSONResponse(content={
        "ok":                 True,
        "total_records":      total_before_limit,
        "returned":           len(records),
        "filter_status":      status,
        "limit":              limit,
        "audit_trail":        records,
    })


# ── POST /api/upload-and-reconcile ───────────────────────────────────────────

_ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",          # some browsers send .csv as text/plain
    "application/octet-stream",
}

_REQUIRED_COLS: dict[str, list[str]] = {
    "erp_ledger": [
        "erp_txn_id", "gross_amount_inr", "gateway_fee_inr",
        "tax_on_fee_inr", "net_payable_inr", "gateway_ref",
    ],
    "gateway_settlement": [
        "gateway_txn_id", "erp_ref_id", "gross_amount_inr",
        "gateway_fee_inr", "gst_on_fee_inr", "net_settled_inr",
        "settlement_utr",
    ],
    "bank_statement": [
        "utr_number", "credit_inr", "narration",
    ],
}

_NUMERIC_COLS: dict[str, list[str]] = {
    "erp_ledger":         ["gross_amount_inr", "gateway_fee_inr",
                           "tax_on_fee_inr",   "net_payable_inr"],
    "gateway_settlement": ["gross_amount_inr", "gateway_fee_inr",
                           "gst_on_fee_inr",   "net_settled_inr"],
    "bank_statement":     ["credit_inr", "debit_inr"],
}


def _validate_and_load_upload(
    raw_bytes: bytes,
    field_name: str,
    content_type: str | None,
) -> pd.DataFrame:
    """
    Parse raw CSV bytes into a DataFrame, enforcing:
      - Non-empty file
      - Advisory content-type check
      - Required column presence
    Casts numeric columns and returns a clean DataFrame.
    """
    # Content-type guard (advisory)
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct and ct not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error":   "UNSUPPORTED_MEDIA_TYPE",
                "field":   field_name,
                "message": (
                    f"'{field_name}' has content-type '{ct}'. "
                    "Expected a CSV file (text/csv or application/octet-stream)."
                ),
            },
        )

    if not raw_bytes:
        raise HTTPException(
            status_code=422,
            detail={
                "error":   "EMPTY_FILE",
                "field":   field_name,
                "message": f"'{field_name}' is empty.",
            },
        )

    # Parse
    import io
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes), dtype=str, encoding="utf-8-sig")
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error":   "CSV_PARSE_ERROR",
                "field":   field_name,
                "message": f"Could not parse '{field_name}' as CSV: {exc}",
            },
        )

    df.columns = df.columns.str.replace('\ufeff', '', regex=False).str.strip().str.lower().str.replace(r'[\s\-]+', '_', regex=True)

    # Required-column validation
    required = _REQUIRED_COLS.get(field_name, [])
    missing_cols = [c for c in required if c not in df.columns]
    if missing_cols:
        raise HTTPException(
            status_code=422,
            detail={
                "error":   "MISSING_COLUMNS",
                "field":   field_name,
                "missing": missing_cols,
                "message": (
                    f"'{field_name}' is missing required column(s): "
                    + ", ".join(missing_cols)
                ),
            },
        )

    # Numeric casting
    for col in _NUMERIC_COLS.get(field_name, []):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    return df


@app.post(
    "/api/upload-and-reconcile",
    summary="Upload CSV files and run reconciliation",
    tags=["Reconciliation"],
    response_description=(
        "Full reconciliation results derived from the uploaded files: "
        "summary, per-row decisions, exception queue, and audit trail."
    ),
)
async def upload_and_reconcile(
    erp_ledger: UploadFile = File(
        ...,
        description="ERP ledger CSV — must match erp_ledger.csv column schema.",
    ),
    gateway_settlement: UploadFile = File(
        ...,
        description="Gateway settlement CSV — must match gateway_settlement.csv schema.",
    ),
    bank_statement: UploadFile = File(
        ...,
        description="Bank statement CSV — must match bank_statement.csv schema.",
    ),
) -> JSONResponse:
    """
    Accepts **three CSV files** as `multipart/form-data`, runs the full
    two-stage LedgerFlow reconciliation engine on them entirely in memory,
    and returns the complete results — no pre-existing server files required.

    **Form fields (all required)**
    | Field | Description |
    |---|---|
    | `erp_ledger` | Internal ERP / accounting export |
    | `gateway_settlement` | Payment gateway settlement report |
    | `bank_statement` | Raw bank statement |

    **Column validation**
    Required columns are checked before the engine runs.
    A `422` response lists any missing columns per file.

    **Lifecycle**
    - Files are read into memory via `UploadFile.read()`.
    - A UUID-namespaced temp directory is created, the files are written
      there, parsed, then the temp directory is deleted in a `finally` block
      regardless of outcome.
    - Output is **not** persisted to `data/output/` — results live only in
      the HTTP response.
    """
    run_id  = str(uuid.uuid4())[:8]
    tmp_dir: Path | None = None

    log.info(
        "POST /api/upload-and-reconcile [%s] — files: '%s' | '%s' | '%s'",
        run_id,
        erp_ledger.filename,
        gateway_settlement.filename,
        bank_statement.filename,
    )

    try:
        # ── 1. Read raw bytes from all three uploads ───────────────────────
        erp_bytes  = await erp_ledger.read()
        gw_bytes   = await gateway_settlement.read()
        bank_bytes = await bank_statement.read()

        # ── 2. Validate & parse each upload ───────────────────────────────
        erp_df  = _validate_and_load_upload(erp_bytes,  "erp_ledger",
                                             erp_ledger.content_type)
        gw_df   = _validate_and_load_upload(gw_bytes,   "gateway_settlement",
                                             gateway_settlement.content_type)
        bank_df = _validate_and_load_upload(bank_bytes, "bank_statement",
                                             bank_statement.content_type)

        log.info(
            "[%s] Parsed: %d ERP | %d GW | %d bank rows",
            run_id, len(erp_df), len(gw_df), len(bank_df),
        )

        # ── 3. Also write to a temp dir (for engine's write_outputs path) ──
        # persist_outputs=False means the engine skips writing CSV/JSONL,
        # so tmp_dir is only used for the engine's internal path references.
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"ledgerflow_{run_id}_"))
        (tmp_dir / "erp_ledger.csv").write_bytes(erp_bytes)
        (tmp_dir / "gateway_settlement.csv").write_bytes(gw_bytes)
        (tmp_dir / "bank_statement.csv").write_bytes(bank_bytes)

        # ── 4. Run the engine ─────────────────────────────────────────────
        summary, results, exc_queue, audit = _run_engine_on_dataframes(
            erp_df, gw_df, bank_df, persist_outputs=False
        )

        log.info(
            "POST /api/upload-and-reconcile [%s] — done, match_rate=%.1f%%",
            run_id, summary["rates"]["match_rate_pct"],
        )

        return JSONResponse(content={
            "ok":      True,
            "run_id":  run_id,
            "source":  "uploaded_files",
            "uploaded_files": {
                "erp_ledger":         erp_ledger.filename,
                "gateway_settlement": gateway_settlement.filename,
                "bank_statement":     bank_statement.filename,
            },
            "summary":         summary,
            "results":         results,
            "exception_queue": exc_queue,
            "audit_trail":     audit,
        })

    except HTTPException as exc:
        log.warning("POST /api/upload-and-reconcile [%s] — validation error: %s", run_id, exc.detail)
        return JSONResponse(content={
            "ok": False,
            "run_id": run_id,
            "source": "uploaded_files",
            "summary": {
                "record_counts": {"total": 0, "matched": 0, "review": 0, "exception": 0},
                "rates": {"match_rate_pct": 0, "review_rate_pct": 0, "exception_rate_pct": 0}
            },
            "exception_queue": [],
            "audit_trail": [],
            "error_detail": exc.detail
        })

    except Exception as exc:
        log.exception(
            "POST /api/upload-and-reconcile [%s] — engine error: %s", run_id, exc
        )
        return JSONResponse(content={
            "ok": False,
            "run_id": run_id,
            "source": "uploaded_files",
            "summary": {
                "record_counts": {"total": 0, "matched": 0, "review": 0, "exception": 0},
                "rates": {"match_rate_pct": 0, "review_rate_pct": 0, "exception_rate_pct": 0}
            },
            "exception_queue": [],
            "audit_trail": [],
            "error_detail": {"message": f"Engine error: {exc}"}
        })

    finally:
        # ── 5. Always clean up the temp directory ─────────────────────────
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.debug("[%s] Temp dir removed.", run_id)


# ── GET /api/exception-queue ──────────────────────────────────────────────────

@app.get(
    "/api/exception-queue",
    summary="Fetch the current exception queue",
    tags=["Reconciliation"],
    response_description=(
        "List of rows that could not be auto-reconciled and require human review."
    ),
)
async def exception_queue() -> JSONResponse:
    """
    Returns rows from the most recent run that were escalated to the exception
    queue (status = EXCEPTION).  Reads from `data/output/exception_queue.csv`.
    """
    exc_path = OUTPUT_DIR / "exception_queue.csv"
    if not exc_path.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error":   "NO_EXCEPTION_QUEUE",
                "message": (
                    "No exception queue found. "
                    "Run POST /api/reconcile first to generate one."
                ),
            },
        )

    exc_df = pd.read_csv(exc_path, dtype=str)
    records = _df_to_records(exc_df)

    # Sub-status breakdown for quick triage
    if records:
        sub_counts: dict[str, int] = {}
        for r in records:
            ss = r.get("sub_status", "UNKNOWN")
            sub_counts[ss] = sub_counts.get(ss, 0) + 1
    else:
        sub_counts = {}

    return JSONResponse(content={
        "ok":                 True,
        "total_exceptions":   len(records),
        "sub_status_summary": sub_counts,
        "exception_queue":    records,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Dev entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
