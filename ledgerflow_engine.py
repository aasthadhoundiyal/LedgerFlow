"""
ledgerflow_engine.py
────────────────────────────────────────────────────────────────────────────────
LedgerFlow — Core Reconciliation Engine
Razorpay AI Buildathon 2026 | Track 4: AI Finance Controller

Architecture: Two-Stage Reconciliation Pipeline
─────────────────────────────────────────────────────────────────────────────
STAGE 1 — DETERMINISTIC CORE
  1a. Exact-match on UTR / invoice IDs (ERP ↔ Gateway ↔ Bank)
  1b. Mathematical integrity check: Gross − Fee − Tax = Net (±ε tolerance)
  1c. Duplicate bank-entry detection (same UTR credited twice)
  1d. Missing-counterpart detection (A4: no gateway row; A5: no bank row)

STAGE 2 — AI / AMBIGUITY FALLBACK
  2a. Fuzzy UTR extraction from truncated bank narrations
  2b. Minor fee-variance resolution (|delta| ≤ ₹5 — policy threshold)
  2c. Gateway-ref-hint cross-matching for narration misses
  2d. Structured reasoning trace attached to every ambiguous decision

AUDIT TRAIL & EXCEPTION QUEUE
  • Every row produces an AuditRecord with:
      – status, sub_status, confidence (0-100), evidence dict, reasoning text
  • Rows that cannot be proved: EXCEPTION queue (CSV + JSON-lines log)
  • Final reconciliation report written to data/output/

Input files (data/mock/):
  erp_ledger.csv          — Internal ERP / accounting records
  gateway_settlement.csv  — Payment gateway settlement report
  bank_statement.csv      — Raw bank statement

Output files (data/output/):
  reconciliation_report.csv    — Full row-level outcome for every ERP record
  exception_queue.csv          — Rows that need human review
  audit_trail.jsonl            — Machine-readable audit log (one JSON per line)
  run_summary.txt              — Human-readable run report
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Configuration constants
# ─────────────────────────────────────────────────────────────────────────────

# Directories
DATA_DIR   = Path("data/mock")
OUTPUT_DIR = Path("data/output")

# Input CSV filenames
ERP_FILE     = DATA_DIR / "erp_ledger.csv"
GATEWAY_FILE = DATA_DIR / "gateway_settlement.csv"
BANK_FILE    = DATA_DIR / "bank_statement.csv"

# Financial thresholds
MATH_EPSILON          = 0.02   # Rs 0.02 — floating-point rounding tolerance for Net check
MINOR_FEE_DELTA_LIMIT = 5.00   # Rs 5.00 — fee variance below this → AI-assisted resolution
NET_MATCH_TOLERANCE   = 0.05   # Rs 0.05 — allowable delta between ERP net and bank credit

# Confidence score floor for an "ACCEPTED" outcome without human review
AUTO_ACCEPT_CONFIDENCE = 70    # 0-100

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ledgerflow")


# ─────────────────────────────────────────────────────────────────────────────
# Status / sub-status taxonomy
# ─────────────────────────────────────────────────────────────────────────────

class Status:
    MATCHED   = "MATCHED"    # Fully reconciled — no intervention needed
    EXCEPTION = "EXCEPTION"  # Cannot be auto-resolved — pushed to exception queue
    REVIEW    = "REVIEW"     # Resolved with AI assist — needs human sign-off


class SubStatus:
    # Stage-1 successes
    EXACT_MATCH          = "EXACT_MATCH"           # UTR + amounts align perfectly
    MATH_VALID           = "MATH_VALID"            # Gross - Fee - Tax = Net within epsilon

    # Stage-2 resolutions
    FUZZY_UTR_MATCH      = "FUZZY_UTR_MATCH"       # UTR extracted from truncated narration
    MINOR_FEE_VARIANCE   = "MINOR_FEE_VARIANCE"    # Fee delta <= Rs5, policy-accepted
    GATEWAY_HINT_MATCH   = "GATEWAY_HINT_MATCH"    # Matched via gateway_ref_hint column

    # Exceptions
    MISSING_GATEWAY      = "MISSING_GATEWAY"       # No gateway row for this ERP record
    MISSING_BANK         = "MISSING_BANK"          # Gateway settled, no bank credit found
    MAJOR_FEE_VARIANCE   = "MAJOR_FEE_VARIANCE"    # Fee delta > Rs5 — unresolvable
    GROSS_MISMATCH       = "GROSS_MISMATCH"        # ERP gross != Gateway gross
    MATH_VIOLATION       = "MATH_VIOLATION"        # Gross - Fee - Tax != Net
    DUPLICATE_BANK_ENTRY = "DUPLICATE_BANK_ENTRY"  # Same UTR credited twice
    NET_AMOUNT_MISMATCH  = "NET_AMOUNT_MISMATCH"   # Net settled != Bank credit
    UNMATCHED            = "UNMATCHED"             # No link found by any method


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AuditRecord:
    """
    Immutable audit evidence attached to every reconciled ERP row.
    Written verbatim to audit_trail.jsonl.
    """
    erp_txn_id:    str
    status:        str
    sub_status:    str
    confidence:    int                      # 0-100
    stage:         int                      # 1 = deterministic, 2 = AI fallback
    reasoning:     str                      # Human-readable decision explanation
    evidence:      dict[str, Any]           # Key facts consulted during decision
    anomaly_hint:  str = ""                 # _anomaly_injected field from ERP (if present)
    ts:            str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReconciliationRow:
    """
    One output row per ERP record, covering all three sources.
    """
    erp_txn_id:          str
    customer_name:        str
    txn_date:             str
    erp_gross:            float
    erp_fee:              float
    erp_tax:              float
    erp_net:              float
    gateway_txn_id:       str
    gw_gross:             float
    gw_fee:               float
    gw_tax:               float
    gw_net:               float
    bank_ref_no:          str
    bank_credit:          float
    bank_narration:       str
    utr:                  str
    fee_delta:            float
    net_delta:            float
    status:               str
    sub_status:           str
    confidence:           int
    stage:                int
    reasoning:            str
    exception_reason:     str = ""
    anomaly_hint:         str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_inr(v: float) -> str:
    """Format a float as INR amount with two decimals."""
    return f"INR {v:,.2f}"


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Coerce a value to float, returning default on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _extract_utr_from_narration(narration: str) -> list[str]:
    """
    Heuristically extract all digit sequences >= 10 chars from a bank narration.
    Real UTRs are 12-digit numbers; partial matches (8-11 digits) are also
    returned as candidates to handle truncated strings.
    """
    candidates = re.findall(r"\d{8,}", narration)
    return candidates


def _partial_utr_match(narration: str, known_utr: str, min_match_len: int = 6) -> bool:
    """
    Returns True if the known UTR appears fully OR as a partial substring
    (first/last `min_match_len` digits) inside the narration.
    Handles patterns like "UPI/PAY/{utr[:8]}/RP..." or "RTGS/SETTL/{utr[:6]}***".
    """
    if known_utr in narration:
        return True
    # Prefix match (first 6-8 chars)
    if len(known_utr) >= min_match_len and known_utr[:min_match_len] in narration:
        return True
    # Suffix match (last 6 chars)
    if len(known_utr) >= min_match_len and known_utr[-min_match_len:] in narration:
        return True
    return False


def _math_check(gross: float, fee: float, tax: float, net: float, label: str) -> tuple[bool, float]:
    """
    Verify the identity: Gross - Fee - Tax = Net within MATH_EPSILON.
    Returns (is_valid, actual_delta).
    """
    computed_net = round(gross - fee - tax, 2)
    delta = round(abs(computed_net - net), 2)
    return delta <= MATH_EPSILON, delta


# ─────────────────────────────────────────────────────────────────────────────
# Data ingestion
# ─────────────────────────────────────────────────────────────────────────────

def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load and lightly sanitise the three input CSVs.
    Returns (erp_df, gateway_df, bank_df).
    """
    log.info("Loading ERP ledger          -> %s", ERP_FILE)
    erp = pd.read_csv(ERP_FILE, dtype=str)

    log.info("Loading gateway settlements -> %s", GATEWAY_FILE)
    gw  = pd.read_csv(GATEWAY_FILE, dtype=str)

    log.info("Loading bank statement      -> %s", BANK_FILE)
    bank = pd.read_csv(BANK_FILE, dtype=str)

    # Normalise column names (strip whitespace)
    for df in (erp, gw, bank):
        df.columns = df.columns.str.strip()

    # Cast numeric columns
    numeric_erp  = ["gross_amount_inr", "gateway_fee_inr", "tax_on_fee_inr", "net_payable_inr"]
    numeric_gw   = ["gross_amount_inr", "gateway_fee_inr", "gst_on_fee_inr", "net_settled_inr"]
    numeric_bank = ["credit_inr", "debit_inr"]

    for col in numeric_erp:
        erp[col] = pd.to_numeric(erp[col], errors="coerce").fillna(0.0)
    for col in numeric_gw:
        gw[col]  = pd.to_numeric(gw[col],  errors="coerce").fillna(0.0)
    for col in numeric_bank:
        bank[col] = pd.to_numeric(bank[col], errors="coerce").fillna(0.0)

    log.info(
        "Loaded: %d ERP rows | %d gateway rows | %d bank rows",
        len(erp), len(gw), len(bank),
    )
    return erp, gw, bank


# ─────────────────────────────────────────────────────────────────────────────
# Index construction — fast O(1) lookups
# ─────────────────────────────────────────────────────────────────────────────

def build_indexes(
    gw: pd.DataFrame,
    bank: pd.DataFrame,
) -> tuple[dict, dict, dict, dict]:
    """
    Pre-build dictionaries for O(1) lookups across gateway and bank data.

    Returns:
        gw_by_erp_ref   : erp_ref_id         -> gateway row (dict)
        gw_by_txn_id    : gateway_txn_id      -> gateway row (dict)
        bank_by_utr     : utr_number          -> list of bank rows (for dup detection)
        bank_by_gw_hint : gateway_ref_hint    -> list of bank rows
    """
    gw_by_erp_ref: dict[str, dict]        = {}
    gw_by_txn_id:  dict[str, dict]        = {}
    bank_by_utr:   dict[str, list[dict]]  = {}
    bank_by_gw_hint: dict[str, list[dict]] = {}

    for _, row in gw.iterrows():
        r = row.to_dict()
        key_erp = str(r.get("erp_ref_id", "")).strip()
        key_txn = str(r.get("gateway_txn_id", "")).strip()
        if key_erp:
            gw_by_erp_ref[key_erp] = r
        if key_txn:
            gw_by_txn_id[key_txn] = r

    for _, row in bank.iterrows():
        r = row.to_dict()
        utr_key  = str(r.get("utr_number", "")).strip()
        hint_key = str(r.get("gateway_ref_hint", "")).strip()
        if utr_key:
            bank_by_utr.setdefault(utr_key, []).append(r)
        if hint_key:
            bank_by_gw_hint.setdefault(hint_key, []).append(r)

    log.info(
        "Indexes built: %d GW(erp_ref) | %d GW(txn_id) | %d bank(UTR) | %d bank(hint)",
        len(gw_by_erp_ref), len(gw_by_txn_id), len(bank_by_utr), len(bank_by_gw_hint),
    )
    return gw_by_erp_ref, gw_by_txn_id, bank_by_utr, bank_by_gw_hint


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — Deterministic Core
# ─────────────────────────────────────────────────────────────────────────────

class DeterministicCore:
    """
    Stage-1 reconciliation.
    Applies strict, rule-based checks with zero tolerance for ambiguity.
    """

    def __init__(
        self,
        gw_by_erp_ref:   dict,
        gw_by_txn_id:    dict,
        bank_by_utr:     dict,
        bank_by_gw_hint: dict,
    ) -> None:
        self.gw_by_erp_ref   = gw_by_erp_ref
        self.gw_by_txn_id    = gw_by_txn_id
        self.bank_by_utr     = bank_by_utr
        self.bank_by_gw_hint = bank_by_gw_hint

    # ── Gateway lookup ────────────────────────────────────────────────────

    def find_gateway_row(self, erp_row: pd.Series) -> dict | None:
        """
        Locate the gateway row for an ERP record.
        Priority: erp_ref_id match > gateway_txn_id match.
        """
        erp_id  = str(erp_row["erp_txn_id"]).strip()
        gw_ref  = str(erp_row.get("gateway_ref", "")).strip()

        row = self.gw_by_erp_ref.get(erp_id)
        if row:
            return row
        if gw_ref:
            return self.gw_by_txn_id.get(gw_ref)
        return None

    # ── Bank lookup ───────────────────────────────────────────────────────

    def find_bank_row(self, gateway_utr: str, gateway_txn_id: str) -> tuple[dict | None, bool]:
        """
        Locate the bank row using the UTR from the gateway record.
        Returns (bank_row_or_None, is_duplicate_detected).
        """
        rows = self.bank_by_utr.get(gateway_utr, [])
        if not rows:
            # Fallback: match via gateway_ref_hint
            rows = self.bank_by_gw_hint.get(gateway_txn_id, [])

        if not rows:
            return None, False

        is_dup = len(rows) > 1
        # Use the first chronological occurrence
        return rows[0], is_dup

    # ── Mathematical integrity check ──────────────────────────────────────

    @staticmethod
    def math_check_row(
        gross: float, fee: float, tax: float, net: float, label: str
    ) -> tuple[bool, float, str]:
        """
        Verify Gross - Fee - Tax = Net.
        Returns (passed, delta, explanation_string).
        """
        ok, delta = _math_check(gross, fee, tax, net, label)
        explanation = (
            f"{label}: {_fmt_inr(gross)} - {_fmt_inr(fee)} - {_fmt_inr(tax)} = "
            f"{_fmt_inr(round(gross - fee - tax, 2))} "
            f"(stated {_fmt_inr(net)}, delta={_fmt_inr(delta)})"
        )
        return ok, delta, explanation

    # ── Gross-amount cross-check ──────────────────────────────────────────

    @staticmethod
    def gross_check(erp_gross: float, gw_gross: float) -> tuple[bool, float]:
        delta = round(abs(erp_gross - gw_gross), 2)
        return delta <= MATH_EPSILON, delta

    # ── Net vs bank-credit check ──────────────────────────────────────────

    @staticmethod
    def net_vs_bank_check(gw_net: float, bank_credit: float) -> tuple[bool, float]:
        delta = round(abs(gw_net - bank_credit), 2)
        return delta <= NET_MATCH_TOLERANCE, delta

    # ── Fee delta between ERP and Gateway ─────────────────────────────────

    @staticmethod
    def fee_delta(erp_fee: float, gw_fee: float) -> float:
        return round(abs(erp_fee - gw_fee), 2)

    # ── Main entry point ──────────────────────────────────────────────────

    def process(self, erp_row: pd.Series) -> dict | None:
        """
        Run all Stage-1 checks for a single ERP row.

        Returns a result dict with the following keys if fully resolved:
            status, sub_status, confidence, stage, reasoning, evidence,
            gw_row (dict), bank_row (dict | None), is_duplicate (bool).

        Returns None if the row must be forwarded to Stage-2 (AI fallback).
        """
        erp_id      = str(erp_row["erp_txn_id"]).strip()
        erp_gross   = _safe_float(erp_row["gross_amount_inr"])
        erp_fee     = _safe_float(erp_row["gateway_fee_inr"])
        erp_tax     = _safe_float(erp_row["tax_on_fee_inr"])
        erp_net     = _safe_float(erp_row["net_payable_inr"])

        # ── ERP internal math check ───────────────────────────────────────
        erp_math_ok, erp_math_delta, erp_math_exp = self.math_check_row(
            erp_gross, erp_fee, erp_tax, erp_net, "ERP"
        )
        if not erp_math_ok:
            return {
                "status":     Status.EXCEPTION,
                "sub_status": SubStatus.MATH_VIOLATION,
                "confidence": 95,
                "stage":      1,
                "reasoning": (
                    f"ERP internal math fails: {erp_math_exp}. "
                    f"Delta {_fmt_inr(erp_math_delta)} exceeds epsilon={MATH_EPSILON}. "
                    "Source data integrity issue — must be corrected at ERP."
                ),
                "evidence": {
                    "erp_gross": erp_gross, "erp_fee": erp_fee,
                    "erp_tax":   erp_tax,   "erp_net": erp_net,
                    "computed_net": round(erp_gross - erp_fee - erp_tax, 2),
                    "math_delta": erp_math_delta,
                },
                "gw_row": None, "bank_row": None, "is_duplicate": False,
            }

        # ── Gateway lookup ────────────────────────────────────────────────
        gw_row = self.find_gateway_row(erp_row)
        if gw_row is None:
            # Missing gateway entry — hard exception
            return {
                "status":     Status.EXCEPTION,
                "sub_status": SubStatus.MISSING_GATEWAY,
                "confidence": 98,
                "stage":      1,
                "reasoning": (
                    f"No gateway settlement row found for ERP record {erp_id}. "
                    "Payment may not have been processed by the gateway, "
                    "or the settlement file is incomplete."
                ),
                "evidence": {"erp_txn_id": erp_id},
                "gw_row": None, "bank_row": None, "is_duplicate": False,
            }

        gw_gross = _safe_float(gw_row["gross_amount_inr"])
        gw_fee   = _safe_float(gw_row["gateway_fee_inr"])
        gw_tax   = _safe_float(gw_row.get("gst_on_fee_inr", 0))
        gw_net   = _safe_float(gw_row["net_settled_inr"])
        gw_utr   = str(gw_row.get("settlement_utr", "")).strip()
        gw_txn   = str(gw_row.get("gateway_txn_id", "")).strip()

        # ── Gateway internal math check ───────────────────────────────────
        gw_math_ok, gw_math_delta, gw_math_exp = self.math_check_row(
            gw_gross, gw_fee, gw_tax, gw_net, "Gateway"
        )
        if not gw_math_ok:
            return {
                "status":     Status.EXCEPTION,
                "sub_status": SubStatus.MATH_VIOLATION,
                "confidence": 92,
                "stage":      1,
                "reasoning": (
                    f"Gateway internal math fails: {gw_math_exp}. "
                    "Settlement file may be corrupted or incorrectly exported."
                ),
                "evidence": {
                    "gw_gross": gw_gross, "gw_fee": gw_fee,
                    "gw_tax":   gw_tax,   "gw_net": gw_net,
                    "math_delta": gw_math_delta,
                },
                "gw_row": gw_row, "bank_row": None, "is_duplicate": False,
            }

        # ── Gross cross-check (ERP vs Gateway) ───────────────────────────
        gross_ok, gross_delta = self.gross_check(erp_gross, gw_gross)
        if not gross_ok:
            return {
                "status":     Status.EXCEPTION,
                "sub_status": SubStatus.GROSS_MISMATCH,
                "confidence": 96,
                "stage":      1,
                "reasoning": (
                    f"Gross amount mismatch: ERP={_fmt_inr(erp_gross)} vs "
                    f"Gateway={_fmt_inr(gw_gross)} (delta={_fmt_inr(gross_delta)}). "
                    "Could indicate a payment amendment or gateway data error."
                ),
                "evidence": {
                    "erp_gross": erp_gross, "gw_gross": gw_gross,
                    "gross_delta": gross_delta,
                },
                "gw_row": gw_row, "bank_row": None, "is_duplicate": False,
            }

        # ── Fee delta check ───────────────────────────────────────────────
        fee_delta_val = self.fee_delta(erp_fee, gw_fee)
        if fee_delta_val > MINOR_FEE_DELTA_LIMIT:
            # Major fee mismatch — exception (cannot auto-resolve)
            return {
                "status":     Status.EXCEPTION,
                "sub_status": SubStatus.MAJOR_FEE_VARIANCE,
                "confidence": 94,
                "stage":      1,
                "reasoning": (
                    f"Major gateway fee variance: ERP fee={_fmt_inr(erp_fee)}, "
                    f"Gateway fee={_fmt_inr(gw_fee)}, delta={_fmt_inr(fee_delta_val)}. "
                    f"Exceeds auto-resolution threshold of INR {MINOR_FEE_DELTA_LIMIT:.2f}. "
                    "Requires manual review and possible gateway debit-note."
                ),
                "evidence": {
                    "erp_fee": erp_fee, "gw_fee": gw_fee,
                    "fee_delta": fee_delta_val,
                    "threshold": MINOR_FEE_DELTA_LIMIT,
                },
                "gw_row": gw_row, "bank_row": None, "is_duplicate": False,
            }

        # ── Bank lookup ───────────────────────────────────────────────────
        bank_row, is_dup = self.find_bank_row(gw_utr, gw_txn)

        if is_dup:
            # Duplicate bank entry — flag immediately
            rows = self.bank_by_utr.get(gw_utr, [])
            dup_refs = [r.get("bank_ref_no", "?") for r in rows]
            return {
                "status":     Status.EXCEPTION,
                "sub_status": SubStatus.DUPLICATE_BANK_ENTRY,
                "confidence": 98,
                "stage":      1,
                "reasoning": (
                    f"UTR {gw_utr} appears {len(rows)} times in the bank statement "
                    f"(refs: {', '.join(dup_refs)}). "
                    "Possible double-credit. One entry must be reversed."
                ),
                "evidence": {
                    "utr": gw_utr, "count": len(rows),
                    "bank_refs": dup_refs,
                    "credits": [_safe_float(r.get("credit_inr", 0)) for r in rows],
                },
                "gw_row": gw_row, "bank_row": bank_row, "is_duplicate": True,
            }

        if bank_row is None:
            # Gateway settled but no bank credit — return None so Stage-2 can
            # attempt fuzzy narration search before declaring exception
            return None

        # ── Net vs bank credit check ──────────────────────────────────────
        bank_credit = _safe_float(bank_row.get("credit_inr", 0))
        net_ok, net_delta = self.net_vs_bank_check(gw_net, bank_credit)
        if not net_ok:
            return {
                "status":     Status.EXCEPTION,
                "sub_status": SubStatus.NET_AMOUNT_MISMATCH,
                "confidence": 93,
                "stage":      1,
                "reasoning": (
                    f"Net settled by gateway ({_fmt_inr(gw_net)}) differs from "
                    f"bank credit ({_fmt_inr(bank_credit)}), delta={_fmt_inr(net_delta)}. "
                    "Possible partial credit or erroneous deduction by bank."
                ),
                "evidence": {
                    "gw_net": gw_net, "bank_credit": bank_credit,
                    "net_delta": net_delta, "tolerance": NET_MATCH_TOLERANCE,
                },
                "gw_row": gw_row, "bank_row": bank_row, "is_duplicate": False,
            }

        # ── Minor fee variance (<=Rs5) — Stage-1 flags but allows flow ───
        # Only raise a REVIEW if there is an actual fee discrepancy
        if fee_delta_val > MATH_EPSILON:
            return {
                "status":     Status.REVIEW,
                "sub_status": SubStatus.MINOR_FEE_VARIANCE,
                "confidence": 82,
                "stage":      1,
                "reasoning": (
                    f"Minor gateway fee variance detected: ERP fee={_fmt_inr(erp_fee)}, "
                    f"Gateway fee={_fmt_inr(gw_fee)}, delta={_fmt_inr(fee_delta_val)}. "
                    f"Within policy threshold of INR {MINOR_FEE_DELTA_LIMIT:.2f}. "
                    "Net amounts reconcile. Flagged for periodic fee audit."
                ),
                "evidence": {
                    "erp_fee": erp_fee, "gw_fee": gw_fee,
                    "fee_delta": fee_delta_val,
                    "gw_net": gw_net, "bank_credit": bank_credit,
                    "utr": gw_utr,
                },
                "gw_row": gw_row, "bank_row": bank_row, "is_duplicate": False,
            }

        # ── All checks passed — perfect match ─────────────────────────────
        return {
            "status":     Status.MATCHED,
            "sub_status": SubStatus.EXACT_MATCH,
            "confidence": 99,
            "stage":      1,
            "reasoning": (
                f"Exact match on ERP<->Gateway<->Bank. "
                f"UTR={gw_utr}, Gross={_fmt_inr(erp_gross)}, "
                f"Net={_fmt_inr(erp_net)}, Bank credit={_fmt_inr(bank_credit)}. "
                f"Math valid (ERP delta={erp_math_delta}, GW delta={gw_math_delta}). "
                "No anomalies detected."
            ),
            "evidence": {
                "utr": gw_utr, "gateway_txn_id": gw_txn,
                "erp_gross": erp_gross, "erp_net": erp_net,
                "gw_gross": gw_gross, "gw_net": gw_net,
                "bank_credit": bank_credit,
                "fee_delta": fee_delta_val,
            },
            "gw_row": gw_row, "bank_row": bank_row, "is_duplicate": False,
        }


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2 — AI / Ambiguity Fallback
# ─────────────────────────────────────────────────────────────────────────────

class AIFallback:
    """
    Stage-2 reconciliation: structured analysis for rows that Stage-1 couldn't
    fully resolve — primarily truncated narrations and missing bank linkages.

    Design note: this is a *structured reasoning* layer (deterministic heuristics
    guided by documented financial logic), not a black-box ML model.  Every
    decision includes an evidence dict and a plain-English reasoning string,
    making it auditable and explainable to finance controllers.
    """

    def __init__(self, bank_df: pd.DataFrame, bank_by_utr: dict, bank_by_gw_hint: dict) -> None:
        self.bank_df         = bank_df
        self.bank_by_utr     = bank_by_utr
        self.bank_by_gw_hint = bank_by_gw_hint

    # ── Attempt 1: fuzzy UTR extraction from all bank narrations ──────────

    def fuzzy_narration_search(
        self, gw_utr: str, gw_txn_id: str
    ) -> dict | None:
        """
        Scan every bank row's narration for any digit string that is a prefix,
        suffix, or full match of `gw_utr`.  Returns the best matching bank row.
        """
        candidates: list[tuple[int, dict]] = []  # (score, row)

        for _, brow in self.bank_df.iterrows():
            narration = str(brow.get("narration", ""))
            utr_field  = str(brow.get("utr_number", "")).strip()

            # Exact UTR field match has highest priority
            if utr_field == gw_utr:
                candidates.append((100, brow.to_dict()))
                continue

            # Gateway hint field match
            hint = str(brow.get("gateway_ref_hint", "")).strip()
            if hint == gw_txn_id:
                candidates.append((90, brow.to_dict()))
                continue

            # Partial UTR match in narration or UTR field
            if _partial_utr_match(narration, gw_utr) or _partial_utr_match(utr_field, gw_utr):
                # Score based on matched length
                narr_candidates = _extract_utr_from_narration(narration)
                best_overlap = max(
                    (len(set(nc) & set(gw_utr)) for nc in narr_candidates),
                    default=0
                )
                score = 50 + best_overlap  # base 50 + overlap bonus
                candidates.append((score, brow.to_dict()))

        if not candidates:
            return None

        # Return the highest-scored candidate
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # ── Attempt 2: amount-based corroboration ─────────────────────────────

    def amount_corroborate(self, gw_net: float, gw_txn_id: str) -> dict | None:
        """
        Secondary check: find a bank row whose credit_inr matches gw_net
        within NET_MATCH_TOLERANCE AND whose gateway_ref_hint matches gw_txn_id.
        This handles cases where narration is completely garbled but amounts align.
        """
        for _, brow in self.bank_df.iterrows():
            hint   = str(brow.get("gateway_ref_hint", "")).strip()
            credit = _safe_float(brow.get("credit_inr", 0))
            if hint == gw_txn_id and abs(credit - gw_net) <= NET_MATCH_TOLERANCE:
                return brow.to_dict()
        return None

    # ── Main entry point ──────────────────────────────────────────────────

    def process(
        self,
        erp_row:     pd.Series,
        gw_row:      dict,
        stage1_hint: str = "",
    ) -> dict:
        """
        Attempt fuzzy bank linkage and structured reasoning for a row that
        Stage-1 could not fully resolve.

        Returns a result dict (same schema as DeterministicCore.process).
        """
        erp_id    = str(erp_row["erp_txn_id"]).strip()
        erp_fee   = _safe_float(erp_row["gateway_fee_inr"])
        erp_net   = _safe_float(erp_row["net_payable_inr"])
        gw_utr    = str(gw_row.get("settlement_utr", "")).strip()
        gw_txn_id = str(gw_row.get("gateway_txn_id", "")).strip()
        gw_net    = _safe_float(gw_row["net_settled_inr"])
        gw_fee    = _safe_float(gw_row["gateway_fee_inr"])

        # ── Attempt 1: fuzzy narration / UTR search ───────────────────────
        bank_row = self.fuzzy_narration_search(gw_utr, gw_txn_id)

        if bank_row is not None:
            bank_credit = _safe_float(bank_row.get("credit_inr", 0))
            narration   = str(bank_row.get("narration", ""))
            bank_ref    = str(bank_row.get("bank_ref_no", ""))
            net_delta   = round(abs(bank_credit - gw_net), 2)
            net_ok      = net_delta <= NET_MATCH_TOLERANCE
            fee_delta_val = round(abs(erp_fee - gw_fee), 2)

            if net_ok:
                return {
                    "status":     Status.REVIEW,
                    "sub_status": SubStatus.FUZZY_UTR_MATCH,
                    "confidence": 78,
                    "stage":      2,
                    "reasoning": (
                        f"Stage-1 exact UTR lookup failed (truncated narration suspected). "
                        f"Stage-2 fuzzy search matched bank ref {bank_ref} via "
                        f"partial UTR / gateway-hint in narration: '{narration}'. "
                        f"Net amounts align: GW={_fmt_inr(gw_net)}, "
                        f"Bank credit={_fmt_inr(bank_credit)}, delta={_fmt_inr(net_delta)}. "
                        f"Fee delta={_fmt_inr(fee_delta_val)}. "
                        "Requires human sign-off due to non-exact narration match."
                    ),
                    "evidence": {
                        "method":       "FUZZY_UTR_NARRATION",
                        "gw_utr":       gw_utr,
                        "narration":    narration,
                        "bank_ref":     bank_ref,
                        "gw_net":       gw_net,
                        "bank_credit":  bank_credit,
                        "net_delta":    net_delta,
                        "fee_delta":    fee_delta_val,
                    },
                    "gw_row": gw_row, "bank_row": bank_row, "is_duplicate": False,
                }
            else:
                # Amounts don't reconcile even after fuzzy match
                return {
                    "status":     Status.EXCEPTION,
                    "sub_status": SubStatus.NET_AMOUNT_MISMATCH,
                    "confidence": 70,
                    "stage":      2,
                    "reasoning": (
                        f"Fuzzy narration match found bank ref {bank_ref}, but "
                        f"net amounts diverge: GW={_fmt_inr(gw_net)}, "
                        f"Bank credit={_fmt_inr(bank_credit)}, "
                        f"delta={_fmt_inr(net_delta)} > tolerance {NET_MATCH_TOLERANCE}. "
                        "Cannot auto-accept. Escalated to exception queue."
                    ),
                    "evidence": {
                        "method":      "FUZZY_UTR_NARRATION",
                        "gw_utr":      gw_utr,
                        "narration":   narration,
                        "gw_net":      gw_net,
                        "bank_credit": bank_credit,
                        "net_delta":   net_delta,
                    },
                    "gw_row": gw_row, "bank_row": bank_row, "is_duplicate": False,
                }

        # ── Attempt 2: amount + hint corroboration ────────────────────────
        bank_row = self.amount_corroborate(gw_net, gw_txn_id)
        if bank_row is not None:
            bank_credit = _safe_float(bank_row.get("credit_inr", 0))
            narration   = str(bank_row.get("narration", ""))
            return {
                "status":     Status.REVIEW,
                "sub_status": SubStatus.GATEWAY_HINT_MATCH,
                "confidence": 72,
                "stage":      2,
                "reasoning": (
                    f"No UTR match found. Matched bank entry via gateway_ref_hint "
                    f"'{gw_txn_id}' and amount corroboration "
                    f"(GW net={_fmt_inr(gw_net)}, bank credit={_fmt_inr(bank_credit)}). "
                    f"Narration: '{narration}'. "
                    "Low confidence — requires human verification."
                ),
                "evidence": {
                    "method":       "AMOUNT_HINT_CORROBORATION",
                    "gw_txn_id":    gw_txn_id,
                    "gw_net":       gw_net,
                    "bank_credit":  bank_credit,
                    "narration":    narration,
                },
                "gw_row": gw_row, "bank_row": bank_row, "is_duplicate": False,
            }

        # ── All fallback methods exhausted -> EXCEPTION ───────────────────
        return {
            "status":     Status.EXCEPTION,
            "sub_status": SubStatus.MISSING_BANK,
            "confidence": 97,
            "stage":      2,
            "reasoning": (
                f"Gateway shows settlement (UTR={gw_utr}, net={_fmt_inr(gw_net)}) "
                "but no corresponding bank credit found by exact UTR lookup, "
                "fuzzy narration search, or amount+hint corroboration. "
                "Possible: bank credit pending, wrong account, or bank file incomplete."
            ),
            "evidence": {
                "gw_utr":    gw_utr,
                "gw_txn_id": gw_txn_id,
                "gw_net":    gw_net,
                "methods_tried": [
                    "EXACT_UTR", "FUZZY_NARRATION", "AMOUNT_HINT_CORROBORATION"
                ],
            },
            "gw_row": gw_row, "bank_row": None, "is_duplicate": False,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliation orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class ReconciliationEngine:
    """
    Orchestrates the full two-stage pipeline across all ERP records.
    Collects ReconciliationRow and AuditRecord objects, then serialises results.
    """

    def __init__(
        self,
        erp_df:  pd.DataFrame,
        gw_df:   pd.DataFrame,
        bank_df: pd.DataFrame,
    ) -> None:
        self.erp_df  = erp_df
        self.gw_df   = gw_df
        self.bank_df = bank_df

        # Build lookup indexes
        self.gw_by_erp_ref, self.gw_by_txn_id, self.bank_by_utr, self.bank_by_gw_hint = (
            build_indexes(gw_df, bank_df)
        )

        # Stage processors
        self.stage1 = DeterministicCore(
            self.gw_by_erp_ref, self.gw_by_txn_id,
            self.bank_by_utr,   self.bank_by_gw_hint,
        )
        self.stage2 = AIFallback(bank_df, self.bank_by_utr, self.bank_by_gw_hint)

        # Output collections
        self.recon_rows:  list[ReconciliationRow] = []
        self.audit_trail: list[AuditRecord]       = []

    # ── Per-row processing ────────────────────────────────────────────────

    def _process_row(self, erp_row: pd.Series) -> None:
        erp_id  = str(erp_row["erp_txn_id"]).strip()
        anomaly = str(erp_row.get("_anomaly_injected", "UNKNOWN")).strip()

        # ── Stage 1 ───────────────────────────────────────────────────────
        result = self.stage1.process(erp_row)

        # Stage-1 returns None for rows needing Stage-2 (bank not found via exact)
        if result is None:
            gw_row = self.stage1.find_gateway_row(erp_row)
            if gw_row:
                result = self.stage2.process(erp_row, gw_row)
            else:
                # Shouldn't happen (Stage-1 catches missing GW rows)
                result = {
                    "status":     Status.EXCEPTION,
                    "sub_status": SubStatus.UNMATCHED,
                    "confidence": 50,
                    "stage":      2,
                    "reasoning":  "No gateway row and no bank row could be found.",
                    "evidence":   {"erp_txn_id": erp_id},
                    "gw_row": None, "bank_row": None, "is_duplicate": False,
                }

        # ── Extract counterpart data ──────────────────────────────────────
        gw_row   = result.get("gw_row") or {}
        bank_row = result.get("bank_row") or {}

        gw_gross = _safe_float(gw_row.get("gross_amount_inr", 0))
        gw_fee   = _safe_float(gw_row.get("gateway_fee_inr", 0))
        gw_tax   = _safe_float(gw_row.get("gst_on_fee_inr", 0))
        gw_net   = _safe_float(gw_row.get("net_settled_inr", 0))
        gw_txn   = str(gw_row.get("gateway_txn_id", ""))
        gw_utr   = str(gw_row.get("settlement_utr", ""))

        bank_credit    = _safe_float(bank_row.get("credit_inr", 0))
        bank_ref       = str(bank_row.get("bank_ref_no", ""))
        bank_narration = str(bank_row.get("narration", ""))

        erp_fee   = _safe_float(erp_row["gateway_fee_inr"])
        erp_net   = _safe_float(erp_row["net_payable_inr"])
        erp_gross = _safe_float(erp_row["gross_amount_inr"])
        erp_tax   = _safe_float(erp_row["tax_on_fee_inr"])

        fee_delta = round(abs(erp_fee - gw_fee), 2) if gw_fee else 0.0
        net_delta = round(abs(erp_net - bank_credit), 2) if bank_credit else 0.0

        exception_reason = ""
        if result["status"] == Status.EXCEPTION:
            exception_reason = result["sub_status"]

        # ── Build ReconciliationRow ───────────────────────────────────────
        recon = ReconciliationRow(
            erp_txn_id=erp_id,
            customer_name=str(erp_row.get("customer_name", "")),
            txn_date=str(erp_row.get("txn_date", "")),
            erp_gross=erp_gross,
            erp_fee=erp_fee,
            erp_tax=erp_tax,
            erp_net=erp_net,
            gateway_txn_id=gw_txn,
            gw_gross=gw_gross,
            gw_fee=gw_fee,
            gw_tax=gw_tax,
            gw_net=gw_net,
            bank_ref_no=bank_ref,
            bank_credit=bank_credit,
            bank_narration=bank_narration,
            utr=gw_utr,
            fee_delta=fee_delta,
            net_delta=net_delta,
            status=result["status"],
            sub_status=result["sub_status"],
            confidence=result["confidence"],
            stage=result["stage"],
            reasoning=result["reasoning"],
            exception_reason=exception_reason,
            anomaly_hint=anomaly,
        )
        self.recon_rows.append(recon)

        # ── Build AuditRecord ─────────────────────────────────────────────
        audit = AuditRecord(
            erp_txn_id=erp_id,
            status=result["status"],
            sub_status=result["sub_status"],
            confidence=result["confidence"],
            stage=result["stage"],
            reasoning=result["reasoning"],
            evidence=result["evidence"],
            anomaly_hint=anomaly,
        )
        self.audit_trail.append(audit)

    # ── Run the full pipeline ─────────────────────────────────────────────

    def run(self) -> None:
        log.info("=" * 70)
        log.info("  LedgerFlow Reconciliation Engine — Starting Run")
        log.info("  Timestamp : %s", datetime.utcnow().isoformat() + "Z")
        log.info("=" * 70)

        total = len(self.erp_df)
        for idx, erp_row in self.erp_df.iterrows():
            erp_id = str(erp_row.get("erp_txn_id", f"row-{idx}"))
            log.debug("Processing %s (%d/%d)", erp_id, idx + 1, total)
            try:
                self._process_row(erp_row)
            except Exception as exc:
                log.error("Unexpected error processing %s: %s", erp_id, exc)
                self.audit_trail.append(AuditRecord(
                    erp_txn_id=erp_id,
                    status=Status.EXCEPTION,
                    sub_status=SubStatus.UNMATCHED,
                    confidence=0,
                    stage=0,
                    reasoning=f"Engine error: {exc}",
                    evidence={"error": str(exc)},
                    anomaly_hint="ENGINE_ERROR",
                ))

        log.info("Processing complete: %d rows evaluated.", total)

    # ── Output serialisation ──────────────────────────────────────────────

    def write_outputs(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Full reconciliation report
        recon_path = OUTPUT_DIR / "reconciliation_report.csv"
        recon_df = pd.DataFrame([vars(r) for r in self.recon_rows])
        recon_df.to_csv(recon_path, index=False)
        log.info("Reconciliation report -> %s (%d rows)", recon_path, len(recon_df))

        # 2. Exception queue
        exc_df = recon_df[recon_df["status"] == Status.EXCEPTION].copy()
        exc_path = OUTPUT_DIR / "exception_queue.csv"
        exc_df.to_csv(exc_path, index=False)
        log.info("Exception queue       -> %s (%d rows)", exc_path, len(exc_df))

        # 3. Audit trail (JSON-lines — one JSON object per ERP row)
        audit_path = OUTPUT_DIR / "audit_trail.jsonl"
        with open(audit_path, "w", encoding="utf-8") as fh:
            for record in self.audit_trail:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        log.info("Audit trail           -> %s (%d entries)", audit_path, len(self.audit_trail))

        # 4. Human-readable run summary
        self._write_summary(recon_df, exc_df)

    def _write_summary(self, recon_df: pd.DataFrame, exc_df: pd.DataFrame) -> None:
        total        = len(recon_df)
        matched      = int((recon_df["status"] == Status.MATCHED).sum())
        review       = int((recon_df["status"] == Status.REVIEW).sum())
        exception    = int((recon_df["status"] == Status.EXCEPTION).sum())
        stage1_count = int((recon_df["stage"] == 1).sum())
        stage2_count = int((recon_df["stage"] == 2).sum())
        avg_conf     = float(recon_df["confidence"].mean())

        sub_counts = recon_df["sub_status"].value_counts().to_dict()

        total_erp_gross   = recon_df["erp_gross"].sum()
        total_gw_net      = recon_df.loc[recon_df["gw_net"] > 0, "gw_net"].sum()
        total_bank_credit = recon_df.loc[recon_df["bank_credit"] > 0, "bank_credit"].sum()
        reconciled_net    = recon_df.loc[
            recon_df["status"] == Status.MATCHED, "erp_net"
        ].sum()

        match_rate = matched / total * 100 if total else 0

        sep = "-" * 72
        lines = [
            "",
            sep,
            "  LedgerFlow -- Reconciliation Run Summary",
            f"  Run timestamp : {datetime.utcnow().isoformat()}Z",
            sep,
            "",
            "  RECORD COUNTS",
            f"    Total ERP records evaluated  : {total:>6}",
            f"    MATCHED (auto-reconciled)     : {matched:>6}  ({matched/total*100:.1f}%)",
            f"    REVIEW  (AI-assisted, pending): {review:>6}  ({review/total*100:.1f}%)",
            f"    EXCEPTION (escalated queue)   : {exception:>6}  ({exception/total*100:.1f}%)",
            "",
            "  STAGE BREAKDOWN",
            f"    Stage-1 (Deterministic Core)  : {stage1_count:>6} rows",
            f"    Stage-2 (AI Fallback)          : {stage2_count:>6} rows",
            "",
            f"  Match rate (MATCHED only)       : {match_rate:.1f}%",
            f"  Average confidence score        : {avg_conf:.1f} / 100",
            "",
            "  SUB-STATUS BREAKDOWN",
        ]
        for ss, cnt in sorted(sub_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {ss:<35} : {cnt:>4} row(s)")

        lines += [
            "",
            "  FINANCIAL SUMMARY (INR)",
            f"    Total ERP Gross              : INR {total_erp_gross:>18,.2f}",
            f"    Total Gateway Net Settled    : INR {total_gw_net:>18,.2f}",
            f"    Total Bank Credits Matched   : INR {total_bank_credit:>18,.2f}",
            f"    Reconciled Net (MATCHED)     : INR {reconciled_net:>18,.2f}",
            "",
        ]

        if exception > 0:
            lines += ["  EXCEPTION QUEUE SUMMARY"]
            exc_sub = exc_df["sub_status"].value_counts().to_dict()
            for ss, cnt in sorted(exc_sub.items(), key=lambda x: -x[1]):
                lines.append(f"    {ss:<35} : {cnt:>4} row(s)")
            lines.append("")

        lines += [
            "  OUTPUT FILES",
            f"    reconciliation_report.csv    : {OUTPUT_DIR / 'reconciliation_report.csv'}",
            f"    exception_queue.csv          : {OUTPUT_DIR / 'exception_queue.csv'}",
            f"    audit_trail.jsonl            : {OUTPUT_DIR / 'audit_trail.jsonl'}",
            "",
            sep,
        ]

        summary_text = "\n".join(lines)

        print(summary_text)

        summary_path = OUTPUT_DIR / "run_summary.txt"
        summary_path.write_text(summary_text, encoding="utf-8")
        log.info("Run summary           -> %s", summary_path)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("LedgerFlow Reconciliation Engine v1.0")
    log.info("Input  directory : %s", DATA_DIR.resolve())
    log.info("Output directory : %s", OUTPUT_DIR.resolve())

    # ── Validate input paths ──────────────────────────────────────────────
    missing = [f for f in (ERP_FILE, GATEWAY_FILE, BANK_FILE) if not f.exists()]
    if missing:
        log.error("Missing input file(s): %s", ", ".join(str(m) for m in missing))
        log.error(
            "Run generate_mock_data.py first to populate data/mock/, "
            "or adjust DATA_DIR in this script."
        )
        sys.exit(1)

    # ── Load data ─────────────────────────────────────────────────────────
    erp_df, gw_df, bank_df = load_data()

    # ── Run reconciliation ────────────────────────────────────────────────
    engine = ReconciliationEngine(erp_df, gw_df, bank_df)
    engine.run()
    engine.write_outputs()

    log.info("LedgerFlow engine run complete.")


if __name__ == "__main__":
    main()
