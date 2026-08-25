"""
test_api.py
--------------------------------------------------------------------------------
LedgerFlow -- API Smoke Test
Razorpay AI Buildathon 2026 | Track 4: AI Finance Controller

Verifies the FastAPI backend is reachable, triggers a full reconciliation run,
and prints a clean summary of the pipeline results.

Usage:
    pip install requests
    python test_api.py
--------------------------------------------------------------------------------
"""

import sys

try:
    import requests
except ImportError:
    print("  ERROR: 'requests' is not installed.")
    print("  Run: pip install requests")
    sys.exit(1)

# -------------------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------------------

BASE_URL        = "http://localhost:8000"
HEALTH_ENDPOINT = f"{BASE_URL}/api/health"
RECON_ENDPOINT  = f"{BASE_URL}/api/reconcile"
TIMEOUT_SECONDS = 60   # reconciliation can take a moment on first run

# -------------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------------

SEP  = "-" * 60
SEP2 = "=" * 60

def _pass(msg: str) -> None:
    print(f"  [PASS]  {msg}")

def _fail(msg: str) -> None:
    print(f"  [FAIL]  {msg}")

def _warn(msg: str) -> None:
    print(f"  [WARN]  {msg}")

def _info(msg: str) -> None:
    print(f"          {msg}")

def _section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

# -------------------------------------------------------------------------------
# Step 1 -- Health check
# -------------------------------------------------------------------------------

def check_health() -> bool:
    """
    GET /api/health -- confirms the server is up and all three CSV inputs exist.
    Exits the script if the server is unreachable or files are missing.
    """
    _section("Step 1 · Health Check")
    print(f"  Endpoint : GET {HEALTH_ENDPOINT}\n")

    try:
        resp = requests.get(HEALTH_ENDPOINT, timeout=5)
    except requests.exceptions.ConnectionError:
        _fail(
            f"Cannot connect to {BASE_URL}.\n"
            "          Make sure the backend is running:\n"
            "            python -m uvicorn main:app --reload --port 8000"
        )
        sys.exit(1)
    except requests.exceptions.Timeout:
        _fail("Health endpoint timed out.")
        sys.exit(1)

    if resp.status_code != 200:
        _fail(f"Health check returned HTTP {resp.status_code}.")
        sys.exit(1)

    data = resp.json()

    if data.get("status") == "ok":
        _pass("Server is reachable.")
    else:
        _fail(f"Unexpected health status: {data.get('status')}")
        sys.exit(1)

    if data.get("input_files_ready"):
        _pass("All three input CSV files are present on disk.")
    else:
        missing = data.get("missing_files", [])
        _fail("One or more input CSV files are missing:")
        for f in missing:
            _info(f"  - {f}")
        _info("Run:  python generate_mock_data.py")
        sys.exit(1)

    return True


# -------------------------------------------------------------------------------
# Step 2 -- Reconciliation run
# -------------------------------------------------------------------------------

def run_reconciliation() -> dict:
    """
    POST /api/reconcile -- triggers the two-stage engine and returns full results.
    Returns the parsed JSON payload.
    """
    _section("Step 2 · Run Reconciliation")
    print(f"  Endpoint : POST {RECON_ENDPOINT}")
    print(f"  Timeout  : {TIMEOUT_SECONDS}s\n")
    print("  Running engine... ", end="", flush=True)

    try:
        resp = requests.post(RECON_ENDPOINT, timeout=TIMEOUT_SECONDS)
    except requests.exceptions.Timeout:
        print()
        _fail(f"Request timed out after {TIMEOUT_SECONDS}s.")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print()
        _fail("Lost connection to the server during reconciliation.")
        sys.exit(1)

    print(f"HTTP {resp.status_code}")

    if resp.status_code == 422:
        body = resp.json()
        _fail(f"Validation error: {body.get('detail', {}).get('message', 'unknown')}")
        sys.exit(1)

    if resp.status_code != 200:
        _fail(f"Unexpected HTTP status {resp.status_code}.")
        _info(resp.text[:300])
        sys.exit(1)

    data = resp.json()

    if not data.get("ok"):
        _fail("Engine returned ok=false.")
        sys.exit(1)

    _pass("Reconciliation run completed successfully.")
    return data


# -------------------------------------------------------------------------------
# Step 3 -- Print summary
# -------------------------------------------------------------------------------

def print_summary(data: dict) -> None:
    """
    Extract and pretty-print key metrics from the reconciliation response.
    """
    _section("Step 3 · Pipeline Summary")

    s   = data["summary"]
    rc  = s["record_counts"]
    rt  = s["rates"]
    fin = s["financials"]
    sub = s.get("sub_status_breakdown", {})
    exc = s.get("exception_breakdown", {})
    stg = s.get("stage_breakdown", {})

    print(f"\n  {SEP2}")
    print(f"  {'LedgerFlow -- Reconciliation Results':^58}")
    print(f"  {SEP2}\n")

    # -- Record counts ---------------------------------------------------------
    print("  RECORD COUNTS")
    print(f"    Total processed        : {rc['total']:>6}")
    print(f"    Matched  (auto)        : {rc['matched']:>6}   ({rt['match_rate_pct']:.1f}%)")
    print(f"    Review   (AI-assisted) : {rc['review']:>6}   ({rt['review_rate_pct']:.1f}%)")
    print(f"    Exception (escalated)  : {rc['exception']:>6}   ({rt['exception_rate_pct']:.1f}%)")

    # -- Stage breakdown -------------------------------------------------------
    print("\n  STAGE BREAKDOWN")
    print(f"    Stage 1 - Deterministic: {stg.get('stage_1_deterministic', 'N/A'):>6} rows")
    print(f"    Stage 2 - AI Fallback  : {stg.get('stage_2_ai_fallback',   'N/A'):>6} rows")

    # -- Quality metrics -------------------------------------------------------
    print("\n  QUALITY METRICS")
    print(f"    Average confidence     : {s['average_confidence']:>6.1f} / 100")
    print(f"    Elapsed time           : {s['elapsed_seconds']:>6.3f}s")

    # -- Sub-status histogram --------------------------------------------------
    print("\n  DECISION BREAKDOWN")
    for ss, count in sorted(sub.items(), key=lambda x: -x[1]):
        bar = "#" * min(count, 30)
        print(f"    {ss:<30} : {count:>4}  {bar}")

    # -- Exception detail ------------------------------------------------------
    if exc:
        print("\n  EXCEPTION REASONS")
        for reason, count in sorted(exc.items(), key=lambda x: -x[1]):
            print(f"    {reason:<30} : {count:>4}")

    # -- Financials ------------------------------------------------------------
    print("\n  FINANCIAL SUMMARY (INR)")
    print(f"    Total ERP Gross        : INR {fin['total_erp_gross_inr']:>16,.2f}")
    print(f"    Total GW Net Settled   : INR {fin['total_gw_net_inr']:>16,.2f}")
    print(f"    Total Bank Credits     : INR {fin['total_bank_credits_inr']:>16,.2f}")
    print(f"    Reconciled Net         : INR {fin['reconciled_net_inr']:>16,.2f}")

    print(f"\n  {SEP2}\n")

    # -- Pass / fail verdict ---------------------------------------------------
    if rc["exception"] == 0:
        _pass("All records reconciled -- no exceptions.")
    else:
        _warn(f"{rc['exception']} record(s) in the exception queue (expected for test data).")

    if rt["match_rate_pct"] >= 80.0:
        _pass(f"Match rate {rt['match_rate_pct']:.1f}% meets the >=80% acceptance threshold.")
    else:
        _fail(f"Match rate {rt['match_rate_pct']:.1f}% is below the 80% threshold -- investigate.")

    print()


# -------------------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------------------

def main() -> None:
    print(f"\n{SEP2}")
    print(f"  {'LedgerFlow -- API Smoke Test':^58}")
    print(f"  {'Razorpay AI Buildathon 2026 | Track 4':^58}")
    print(f"{SEP2}")

    check_health()
    data = run_reconciliation()
    print_summary(data)


if __name__ == "__main__":
    main()
