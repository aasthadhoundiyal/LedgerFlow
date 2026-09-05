"""
generate_mock_data.py
─────────────────────────────────────────────────────────────────────────────
LedgerFlow — Mock Data Generator
Razorpay AI Buildathon 2026 | Track 4: AI Finance Controller

Generates 100 realistic, messy financial records across three CSV files:
  • erp_ledger.csv         — Internal ERP / accounting system records
  • gateway_settlement.csv — Payment gateway settlement report (Razorpay-style)
  • bank_statement.csv     — Raw bank statement with narrations

~15% of rows carry intentional anomalies to stress-test the reconciliation
engine:
  [A1] Truncated / garbled bank narrations
  [A2] Minor fee mismatches between ERP and gateway (within ±₹5)
  [A3] Larger fee mismatch requiring human review (>₹5)
  [A4] Missing gateway entry (ERP record has no gateway counterpart)
  [A5] Missing bank credit (gateway settled but bank row absent)
  [A6] Amount mismatch between ERP gross and gateway gross
  [A7] Duplicate bank narration row (same UTR credited twice)

Architecture note:
  All amounts are computed deterministically (no random net = gross - fee
  re-computation at read-time). The net stored here is exactly what a
  downstream reconciliation engine should independently verify.
─────────────────────────────────────────────────────────────────────────────
"""

import random
import string
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

NUM_RECORDS = 250
ANOMALY_RATE = 0.25  # 25% of records will have some discrepancy
SEED = 999           # Different seed for demo data

OUTPUT_DIR = Path("data/demo")

GATEWAY_FEE_RATE = 0.02      # 2% of gross
TAX_ON_FEE_RATE = 0.18       # 18% GST on gateway fee

random.seed(SEED)
np.random.seed(SEED)

# ─────────────────────────────────────────────────────────────────────────────
# Reference data pools
# ─────────────────────────────────────────────────────────────────────────────

CUSTOMER_NAMES = [
    "Arjun Mehta", "Priya Nair", "Rohan Sharma", "Sunita Kapoor",
    "Vikram Joshi", "Deepika Rao", "Amit Gupta", "Neha Singhania",
    "Rahul Verma", "Pooja Iyer", "Sanjay Bhat", "Anjali Desai",
    "Manish Tiwari", "Kavitha Reddy", "Suresh Pillai", "Meera Agarwal",
    "Dinesh Choudhary", "Lakshmi Narayanan", "Aditya Srivastava", "Rekha Menon",
]

PAYMENT_METHODS = ["UPI", "NEFT", "IMPS", "Card", "Netbanking", "Wallet"]

PAYMENT_METHOD_GATEWAY_MAP = {
    "UPI": "UPI",
    "NEFT": "NEFT",
    "IMPS": "IMPS",
    "Card": "CARD",
    "Netbanking": "NB",
    "Wallet": "WALLET",
}

BANK_NARRATION_TEMPLATES = [
    "NEFT-{utr}-{customer}-REF{ref}",
    "UPI/{utr}/{customer}/RAZORPAY",
    "IMPS/{utr}/{customer}/SETTLE",
    "CR-RAZORPAY-{utr}-{ref}",
    "RAZORPAY SETTLEMENTS {utr}",
    "INB CREDIT {customer} {utr}",
    "RTGS/{utr}/RAZORPAYPAYMENTS/SETTLE",
]

ERP_STATUS_OPTIONS = ["POSTED", "POSTED", "POSTED", "PENDING", "RECONCILED"]


# ─────────────────────────────────────────────────────────────────────────────
# ID generators
# ─────────────────────────────────────────────────────────────────────────────

def make_erp_id(index: int) -> str:
    return f"ERP-2026-{index:04d}"


def make_gateway_txn_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=14))
    return f"pay_{suffix}"


def make_utr() -> str:
    """Generates a realistic UTR (Unique Transaction Reference) number."""
    return str(random.randint(100000000000, 999999999999))


def make_bank_ref() -> str:
    return f"BNK{random.randint(1000000, 9999999)}"


# ─────────────────────────────────────────────────────────────────────────────
# Amount calculators (deterministic — single source of truth)
# ─────────────────────────────────────────────────────────────────────────────

def compute_fee(gross: float) -> float:
    return round(gross * GATEWAY_FEE_RATE, 2)


def compute_tax(fee: float) -> float:
    return round(fee * TAX_ON_FEE_RATE, 2)


def compute_net(gross: float, fee: float, tax: float) -> float:
    return round(gross - fee - tax, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Narration builder
# ─────────────────────────────────────────────────────────────────────────────

def build_narration(customer: str, utr: str, ref: str, truncated: bool = False) -> str:
    """Returns a realistic bank narration string, optionally truncated/garbled."""
    if truncated:
        # Simulate real-world bank truncation patterns
        truncated_templates = [
            f"NEFT-{utr}-{customer[:6]}",            # cut mid-name
            f"UPI/PAY/{utr[:8]}/RP...",              # ellipsis truncation
            f"CR-RAZORPAY-{utr}",                    # missing ref segment
            f"IMPS CREDIT {utr[:8]}",                # partial UTR
            f"RTGS/SETTL/{utr[:6]}***",              # masked UTR
            f"...RPAY SETTLEMENTS {utr[-6:]}",       # leading chars stripped
        ]
        return random.choice(truncated_templates)
    else:
        template = random.choice(BANK_NARRATION_TEMPLATES)
        return (
            template
            .replace("{utr}", utr)
            .replace("{customer}", customer.replace(" ", ""))
            .replace("{ref}", ref)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Anomaly flag registry
# ─────────────────────────────────────────────────────────────────────────────

ANOMALY_LABELS = {
    "A1": "TRUNCATED_BANK_NARRATION",
    "A2": "MINOR_FEE_MISMATCH",        # ≤ ₹5 variance — AI may assist
    "A3": "MAJOR_FEE_MISMATCH",        # > ₹5 variance — exception queue
    "A4": "MISSING_GATEWAY_ENTRY",
    "A5": "MISSING_BANK_CREDIT",
    "A6": "GROSS_AMOUNT_MISMATCH",
    "A7": "DUPLICATE_BANK_ENTRY",
}


def assign_anomalies(total: int, rate: float) -> dict:
    """
    Distributes anomaly types across ~15% of record indices.
    Returns a dict of {record_index: anomaly_code}.
    """
    anomaly_count = int(total * rate)
    indices = random.sample(range(total), anomaly_count)
    types = list(ANOMALY_LABELS.keys())
    # Weight: A1/A2 more common (realistic distribution)
    weights = [0.25, 0.20, 0.10, 0.15, 0.15, 0.10, 0.05]
    chosen = random.choices(types, weights=weights, k=anomaly_count)
    return dict(zip(indices, chosen))


# ─────────────────────────────────────────────────────────────────────────────
# Core record builder
# ─────────────────────────────────────────────────────────────────────────────

def build_base_records(anomaly_map: dict) -> list:
    """
    Builds the canonical list of 100 financial transactions.
    Each record stores all fields needed to populate all three CSVs.
    Anomalies are injected here so downstream writers stay consistent.
    """
    base_date = datetime(2026, 7, 1)
    records = []

    for i in range(NUM_RECORDS):
        anomaly = anomaly_map.get(i)

        # ── Identifiers ───────────────────────────────────────────────────
        erp_id = make_erp_id(i + 1)
        gateway_txn_id = make_gateway_txn_id()
        utr = make_utr()
        bank_ref = make_bank_ref()

        # ── Transaction metadata ──────────────────────────────────────────
        customer = random.choice(CUSTOMER_NAMES)
        payment_method = random.choice(PAYMENT_METHODS)
        txn_date = base_date + timedelta(days=random.randint(0, 53))
        settlement_date = txn_date + timedelta(days=random.randint(1, 3))
        bank_value_date = settlement_date + timedelta(days=random.randint(0, 2))

        # ── Canonical amounts (deterministic) ─────────────────────────────
        gross = round(random.uniform(500.0, 150_000.0), 2)
        erp_fee = compute_fee(gross)
        erp_tax = compute_tax(erp_fee)
        erp_net = compute_net(gross, erp_fee, erp_tax)

        # ── Gateway amounts (default = mirrors ERP) ───────────────────────
        gw_gross = gross
        gw_fee = erp_fee
        gw_tax = erp_tax
        gw_net = erp_net

        # ── Bank defaults ─────────────────────────────────────────────────
        bank_credit = erp_net
        bank_narration = build_narration(customer, utr, erp_id[-6:])
        has_bank_row = True
        has_gateway_row = True
        is_duplicate_bank = False
        anomaly_label = None

        # ── Apply anomaly mutations ───────────────────────────────────────
        if anomaly == "A1":
            bank_narration = build_narration(customer, utr, erp_id[-6:], truncated=True)
            anomaly_label = ANOMALY_LABELS["A1"]

        elif anomaly == "A2":
            delta = round(random.uniform(0.50, 5.00), 2) * random.choice([-1, 1])
            gw_fee = round(erp_fee + delta, 2)
            gw_net = compute_net(gw_gross, gw_fee, gw_tax)
            bank_credit = gw_net
            anomaly_label = ANOMALY_LABELS["A2"]

        elif anomaly == "A3":
            delta = round(random.uniform(5.01, 50.00), 2) * random.choice([-1, 1])
            gw_fee = round(erp_fee + delta, 2)
            gw_net = compute_net(gw_gross, gw_fee, gw_tax)
            bank_credit = gw_net
            anomaly_label = ANOMALY_LABELS["A3"]

        elif anomaly == "A4":
            has_gateway_row = False
            has_bank_row = False    # no settlement → no bank credit
            anomaly_label = ANOMALY_LABELS["A4"]

        elif anomaly == "A5":
            has_bank_row = False    # gateway settled but bank credit missing
            anomaly_label = ANOMALY_LABELS["A5"]

        elif anomaly == "A6":
            gw_gross = round(gross + random.uniform(10.0, 200.0) * random.choice([-1, 1]), 2)
            gw_fee = compute_fee(gw_gross)
            gw_tax = compute_tax(gw_fee)
            gw_net = compute_net(gw_gross, gw_fee, gw_tax)
            bank_credit = gw_net
            anomaly_label = ANOMALY_LABELS["A6"]

        elif anomaly == "A7":
            is_duplicate_bank = True
            anomaly_label = ANOMALY_LABELS["A7"]

        records.append({
            "index": i,
            "erp_id": erp_id,
            "gateway_txn_id": gateway_txn_id,
            "utr": utr,
            "bank_ref": bank_ref,
            "customer": customer,
            "payment_method": payment_method,
            "txn_date": txn_date,
            "settlement_date": settlement_date,
            "bank_value_date": bank_value_date,
            # ERP amounts
            "erp_gross": gross,
            "erp_fee": erp_fee,
            "erp_tax": erp_tax,
            "erp_net": erp_net,
            "erp_status": random.choice(ERP_STATUS_OPTIONS),
            # Gateway amounts (may diverge under anomalies)
            "gw_gross": gw_gross,
            "gw_fee": gw_fee,
            "gw_tax": gw_tax,
            "gw_net": gw_net,
            # Bank
            "bank_credit": bank_credit,
            "bank_narration": bank_narration,
            # Flags
            "has_gateway_row": has_gateway_row,
            "has_bank_row": has_bank_row,
            "is_duplicate_bank": is_duplicate_bank,
            "anomaly_code": anomaly if anomaly else "NONE",
            "anomaly_label": anomaly_label if anomaly_label else "CLEAN",
        })

    return records


# ─────────────────────────────────────────────────────────────────────────────
# CSV writers
# ─────────────────────────────────────────────────────────────────────────────

def write_erp_ledger(records: list, output_dir: Path) -> Path:
    """
    ERP Ledger — internal accounting system export.
    All 100 transactions appear here (it is the master source of truth).
    """
    rows = []
    for r in records:
        rows.append({
            "erp_txn_id":               r["erp_id"],
            "txn_date":                 r["txn_date"].strftime("%Y-%m-%d"),
            "customer_name":            r["customer"],
            "payment_method":           r["payment_method"],
            "gross_amount_inr":         r["erp_gross"],
            "gateway_fee_inr":          r["erp_fee"],
            "tax_on_fee_inr":           r["erp_tax"],
            "net_payable_inr":          r["erp_net"],
            "expected_settlement_date": r["settlement_date"].strftime("%Y-%m-%d"),
            "status":                   r["erp_status"],
            "gateway_ref":              r["gateway_txn_id"],
            # Audit field — present so we can validate reconciliation output
            "_anomaly_injected":        r["anomaly_code"],
        })

    df = pd.DataFrame(rows)
    path = output_dir / "erp_ledger.csv"
    df.to_csv(path, index=False)
    return path


def write_gateway_settlement(records: list, output_dir: Path) -> Path:
    """
    Gateway Settlement Report — Razorpay-style settlement dump.
    Absent for A4 (missing gateway entry) records.
    """
    rows = []
    for r in records:
        if not r["has_gateway_row"]:
            continue  # deliberately omitted

        rows.append({
            "gateway_txn_id":   r["gateway_txn_id"],
            "erp_ref_id":       r["erp_id"],
            "settlement_date":  r["settlement_date"].strftime("%Y-%m-%d"),
            "payment_mode":     PAYMENT_METHOD_GATEWAY_MAP[r["payment_method"]],
            "gross_amount_inr": r["gw_gross"],
            "gateway_fee_inr":  r["gw_fee"],
            "gst_on_fee_inr":   r["gw_tax"],
            "net_settled_inr":  r["gw_net"],
            "settlement_utr":   r["utr"],
            "status":           "SETTLED",
        })

    df = pd.DataFrame(rows)
    path = output_dir / "gateway_settlement.csv"
    df.to_csv(path, index=False)
    return path


def write_bank_statement(records: list, output_dir: Path) -> Path:
    """
    Bank Statement — raw export from the bank portal.
    • Narrations are messy / truncated for A1 records.
    • Row absent for A4 and A5 records (no credit arrived).
    • Extra duplicate row injected for A7 records.
    """
    rows = []
    running_balance = 5_000_000.00  # ₹50 lakh opening balance

    for r in records:
        if not r["has_bank_row"]:
            continue  # deliberately omitted

        running_balance = round(running_balance + r["bank_credit"], 2)
        rows.append({
            "bank_ref_no":      r["bank_ref"],
            "value_date":       r["bank_value_date"].strftime("%Y-%m-%d"),
            "txn_date":         r["bank_value_date"].strftime("%Y-%m-%d"),
            "narration":        r["bank_narration"],
            "utr_number":       r["utr"],
            "credit_inr":       r["bank_credit"],
            "debit_inr":        0.00,
            "balance_inr":      running_balance,
            "gateway_ref_hint": r["gateway_txn_id"],
        })

        if r["is_duplicate_bank"]:
            # Same UTR credited again — duplicate signal for the recon engine
            running_balance = round(running_balance + r["bank_credit"], 2)
            rows.append({
                "bank_ref_no":      make_bank_ref(),          # different bank ref
                "value_date":       r["bank_value_date"].strftime("%Y-%m-%d"),
                "txn_date":         r["bank_value_date"].strftime("%Y-%m-%d"),
                "narration":        r["bank_narration"],      # identical narration
                "utr_number":       r["utr"],                 # SAME UTR — key signal
                "credit_inr":       r["bank_credit"],
                "debit_inr":        0.00,
                "balance_inr":      running_balance,
                "gateway_ref_hint": r["gateway_txn_id"],
            })

    df = pd.DataFrame(rows)
    df = df.sort_values("value_date").reset_index(drop=True)
    path = output_dir / "bank_statement.csv"
    df.to_csv(path, index=False)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Summary report
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(records: list, paths: dict) -> None:
    anomalies = [r for r in records if r["anomaly_code"] != "NONE"]
    clean     = [r for r in records if r["anomaly_code"] == "NONE"]

    anomaly_counts = Counter(r["anomaly_label"] for r in anomalies)

    total_erp_gross   = sum(r["erp_gross"]    for r in records)
    total_gw_net      = sum(r["gw_net"]       for r in records if r["has_gateway_row"])
    total_bank_credit = sum(r["bank_credit"]  for r in records if r["has_bank_row"])

    sep = "-" * 65
    print("\n" + sep)
    print("  LedgerFlow - Mock Data Generation Report")
    print(sep)
    print(f"  Total records generated    : {NUM_RECORDS}")
    print(f"  Clean records              : {len(clean)}")
    print(f"  Anomalous records          : {len(anomalies)} "
          f"({len(anomalies) / NUM_RECORDS * 100:.1f}%)")
    print()
    print("  Anomaly breakdown:")
    for label, count in sorted(anomaly_counts.items()):
        print(f"    {label:<35} {count:>3} row(s)")
    print()
    print(f"  Total ERP Gross (INR)      : {total_erp_gross:>15,.2f}")
    print(f"  Total GW Net Settled (INR) : {total_gw_net:>15,.2f}")
    print(f"  Total Bank Credits (INR)   : {total_bank_credit:>15,.2f}")
    print()
    print("  Output files:")
    for label, path in paths.items():
        print(f"    {label:<30} -> {path}")
    print(sep + "\n")


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Assigning anomalies...")
    anomaly_map = assign_anomalies(NUM_RECORDS, ANOMALY_RATE)

    print("Building base records...")
    records = build_base_records(anomaly_map)

    print("Writing erp_ledger.csv...")
    erp_path = write_erp_ledger(records, OUTPUT_DIR)

    print("Writing gateway_settlement.csv...")
    gw_path = write_gateway_settlement(records, OUTPUT_DIR)

    print("Writing bank_statement.csv...")
    bank_path = write_bank_statement(records, OUTPUT_DIR)

    print_summary(records, {
        "erp_ledger.csv":         erp_path,
        "gateway_settlement.csv": gw_path,
        "bank_statement.csv":     bank_path,
    })


if __name__ == "__main__":
    main()
