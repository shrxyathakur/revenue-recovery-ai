"""
Synthetic Payment Failure Event Generator — v1
Covers: Card, UPI (Intent), E-mandate (Registration + Subsequent)

Source of truth for error_reason / bucket / hard_decline:
  reason_bucket_map_card_upi_emandate.md (built earlier in this project,
  grounded directly against Razorpay's official docs — cards, UPI, and the
  user-provided E-mandate error-reasons PDF).

IMPORTANT HONESTY NOTE (do not remove):
  - error_source values are doc-grounded for all three methods.
  - error_step values are doc-grounded for Card (5 steps), UPI Intent (15 steps),
    and E-mandate (3 steps: payment_initiation, payment_authentication,
    payment_authorization — confirmed directly from Razorpay's docs, same
    coarse shape as Netbanking's 3-step model).
  - Per-reason error_source/error_step assignment (which specific step a given
    reason maps to) is OUR inference, not something Razorpay publishes as a
    reason->step lookup. Bucket assignment is likewise our inference on top of
    Razorpay's reason explanations, as already documented in the .md file.
"""

import random
import string
import csv
import json
from datetime import datetime, timedelta

random.seed(42)  # reproducible for demo/eval purposes; remove or vary for stress testing

# ---------------------------------------------------------------------------
# 1. REASON TABLE — transcribed directly from reason_bucket_map_card_upi_emandate.md
#    Fields: method, payment_phase, error_reason, bucket, hard_decline,
#            error_source, error_step
# ---------------------------------------------------------------------------

CARD_REASONS = [
    # reason,                              bucket,             hard_decline, source,       step
    ("authentication_failed",              "genuine_decline",  False, "customer",     "payment_authentication"),
    ("insufficient_funds",                 "genuine_decline",  False, "customer",     "payment_authorization"),
    ("incorrect_cvv",                      "genuine_decline",  False, "customer",     "payment_authentication"),
    ("payment_cancelled",                  "genuine_decline",  False, "customer",     "payment_authentication"),
    ("card_not_enrolled",                  "genuine_decline",  True,  "issuer_bank",  "card_enrollment_check"),
    ("card_disabled_for_online_payments",  "genuine_decline",  True,  "issuer_bank",  "card_enrollment_check"),
    ("debit_instrument_inactive",          "genuine_decline",  True,  "issuer_bank",  "card_enrollment_check"),
    ("debit_instrument_blocked",           "genuine_decline",  True,  "issuer_bank",  "payment_authorization"),
    ("card_expired",                       "genuine_decline",  True,  "customer",     "payment_initiation"),
    ("transaction_limit_exceeded",         "genuine_decline",  True,  "issuer_bank",  "payment_authorization"),
    ("bank_technical_error",               "bank_outage",      False, "issuer_bank",  "payment_authorization"),
    ("gateway_technical_error",            "bank_outage",      False, "gateway",      "payment_authorization"),
    ("payment_risk_check_failed",          "fraud_fp",         False, "issuer_bank",  "payment_authorization"),
    ("card_declined",                      "uncertain",        False, "issuer_bank",  "payment_authorization"),
    ("payment_failed",                     "uncertain",        False, "issuer_bank",  "payment_authorization"),
]

UPI_REASONS = [
    ("insufficient_funds",                 "genuine_decline",  False, "customer",         "payment_debit_response"),
    ("payment_cancelled",                  "genuine_decline",  False, "customer",         "payment_authentication"),
    ("invalid_vpa",                        "genuine_decline",  True,  "customer",         "payment_creation"),
    ("bank_technical_error",               "bank_outage",      False, "issuer_bank",      "payment_debit_response"),
    ("gateway_technical_error",            "bank_outage",      False, "gateway",          "payment_request"),
    ("credit_failed",                      "bank_outage",      False, "beneficiary_bank", "payment_credit_response"),
    ("payment_collect_request_expired",    "uncertain",        False, "customer",         "payment_authentication"),
    ("payment_declined",                   "uncertain",        False, "issuer_bank",      "payment_debit_response"),
    ("payment_timed_out",                  "uncertain",        False, "network",          "payment_status_response"),
    ("vpa_resolution_failed",              "uncertain",        False, "network",          "payment_creation"),
]

# E-mandate: shared (both phases), registration-only, subsequent-only
# error_step now grounded against the confirmed 3-step flow (payment_initiation /
# payment_authentication / payment_authorization). Per-reason step assignment
# is our inference on top of that confirmed list, same as Card/UPI.
EMANDATE_SHARED = [
    ("bank_account_invalid",           "genuine_decline", True,  "issuer_bank", "payment_authorization"),
    ("bank_account_validation_failed", "uncertain",        False, "gateway",     "payment_authentication"),
    ("bank_technical_error",           "bank_outage",      False, "issuer_bank", "payment_authorization"),
    ("debit_instrument_blocked",       "genuine_decline", True,  "issuer_bank", "payment_authorization"),
    ("debit_instrument_inactive",      "genuine_decline", True,  "issuer_bank", "payment_authorization"),
    ("gateway_technical_error",        "bank_outage",      False, "gateway",     "payment_initiation"),
    ("insufficient_funds",             "genuine_decline", False, "customer",    "payment_authorization"),
    ("payment_cancelled",              "genuine_decline", False, "customer",    "payment_authentication"),
    ("payment_failed",                 "uncertain",        False, "issuer_bank", "payment_authorization"),
    ("payment_timed_out",              "uncertain",        False, "issuer_bank", "payment_authorization"),
    ("server_error",                   "bank_outage",      False, "internal",    "payment_authorization"),
    ("transaction_limit_exceeded",     "genuine_decline", True,  "issuer_bank", "payment_authorization"),
]

EMANDATE_REGISTRATION_ONLY = [
    ("already_declined",                       "genuine_decline", True,  "network",     "payment_authentication"),
    ("authentication_failed",                  "genuine_decline", False, "customer",    "payment_authentication"),
    ("card_expired",                           "genuine_decline", True,  "customer",    "payment_authentication"),
    ("card_number_invalid",                    "genuine_decline", True,  "customer",    "payment_initiation"),
    ("duplicate_request",                      "uncertain",        False, "gateway",     "payment_initiation"),
    ("incorrect_card_expiry_date",              "genuine_decline", False, "customer",    "payment_authentication"),
    ("incorrect_cvv",                          "genuine_decline", False, "customer",    "payment_authentication"),
    ("incorrect_otp",                          "genuine_decline", False, "customer",    "payment_authentication"),
    ("incorrect_pin",                          "genuine_decline", False, "customer",    "payment_authentication"),
    ("joint_account_not_allowed",               "genuine_decline", True,  "issuer_bank", "payment_authorization"),
    ("otp_attempts_exceeded",                  "genuine_decline", True,  "issuer_bank", "payment_authentication"),
    ("payment_pending_approval",               "uncertain",        False, "issuer_bank", "payment_authorization"),
    ("payment_risk_check_failed",              "fraud_fp",         False, "issuer_bank", "payment_authorization"),
    ("user_not_registered_for_netbanking",     "genuine_decline", True,  "customer",    "payment_authentication"),
]

EMANDATE_SUBSEQUENT_ONLY = [
    ("incorrect_ifsc",              "genuine_decline", True,  "customer",    "payment_initiation"),
    ("input_validation_failed",     "uncertain",        False, "business",    "payment_initiation"),
    ("invalid_amount",              "uncertain",        False, "business",    "payment_initiation"),
    ("mandate_not_active",          "genuine_decline", True,  "issuer_bank", "payment_authorization"),
    ("payment_declined",            "uncertain",        False, "issuer_bank", "payment_authorization"),
    ("payment_mandate_not_active",  "bank_outage",      False, "issuer_bank", "payment_authorization"),
]

def build_reason_table():
    """Flatten all method/phase reason lists into one uniform list of dicts."""
    table = []
    for reason, bucket, hd, source, step in CARD_REASONS:
        table.append({"method": "card", "payment_phase": None, "error_reason": reason,
                      "bucket": bucket, "hard_decline": hd, "error_source": source, "error_step": step})
    for reason, bucket, hd, source, step in UPI_REASONS:
        table.append({"method": "upi", "payment_phase": None, "error_reason": reason,
                      "bucket": bucket, "hard_decline": hd, "error_source": source, "error_step": step})
    for reason, bucket, hd, source, step in EMANDATE_SHARED:
        for phase in ("registration", "subsequent"):
            table.append({"method": "emandate", "payment_phase": phase, "error_reason": reason,
                          "bucket": bucket, "hard_decline": hd, "error_source": source, "error_step": step})
    for reason, bucket, hd, source, step in EMANDATE_REGISTRATION_ONLY:
        table.append({"method": "emandate", "payment_phase": "registration", "error_reason": reason,
                      "bucket": bucket, "hard_decline": hd, "error_source": source, "error_step": step})
    for reason, bucket, hd, source, step in EMANDATE_SUBSEQUENT_ONLY:
        table.append({"method": "emandate", "payment_phase": "subsequent", "error_reason": reason,
                      "bucket": bucket, "hard_decline": hd, "error_source": source, "error_step": step})
    return table

REASON_TABLE = build_reason_table()

# ---------------------------------------------------------------------------
# 2. JOIN KEY POOLS
#    Bank codes: real IFSC-style 4-letter codes, same identifier space
#    confirmed across card.issuer, emandate.bank, and the Downtime API's
#    instrument.issuer / instrument.bank fields.
# ---------------------------------------------------------------------------

BANK_CODES = ["HDFC", "UTIB", "ICIC", "SBIN", "YESB", "COSB", "PUNB", "KKBK", "IDFB", "INDB"]

# VPA handles: Category A resolves to a real bank code (usable for bank-level join),
# Category B resolves only to a PSP (route-level clustering only) — per
# vpa_handle_bank_psp_mapping.md
VPA_CATEGORY_A = {
    "okhdfcbank": "HDFC", "okaxis": "UTIB", "oksbi": "SBIN", "okicici": "ICIC",
    "ptsbi": "SBIN", "pthdfc": "HDFC", "ptaxis": "UTIB", "ptyes": "YESB",
}
VPA_CATEGORY_B = ["ybl", "axl", "ibl", "paytm", "upi", "yapl", "jio"]

FIRST_NAMES = ["arjun", "priya", "rahul", "sneha", "vikram", "anita", "karan", "divya", "rohit", "meera"]


def _rand_id(prefix, length=14):
    chars = string.ascii_letters + string.digits
    return f"{prefix}_" + "".join(random.choice(chars) for _ in range(length))


def _pick_vpa(forced_bank=None):
    """
    Returns (vpa_string, join_category, resolvable_bank).
    If forced_bank is given (outage injection), pick a Category A handle
    whose bank matches it, so the cluster is actually bank-resolvable —
    otherwise the outage's join key wouldn't correlate to anything.
    """
    if forced_bank is not None:
        matching = [h for h, b in VPA_CATEGORY_A.items() if b == forced_bank]
        if matching:
            handle = random.choice(matching)
            return f"{random.choice(FIRST_NAMES)}@{handle}", "A", forced_bank
        # no Category A handle exists for this bank code in our mapping —
        # fall through to unconstrained pick rather than silently mismatch
    if random.random() < 0.55:  # Category A more common (Google Pay/Paytm bank-linked handles)
        handle, bank = random.choice(list(VPA_CATEGORY_A.items()))
        return f"{random.choice(FIRST_NAMES)}@{handle}", "A", bank
    else:
        handle = random.choice(VPA_CATEGORY_B)
        return f"{random.choice(FIRST_NAMES)}@{handle}", "B", None


def _join_key_for(method, upi_bank_hint=None):
    """Returns (join_key_type, join_key_value, resolvable_bank_code_or_None)."""
    if method == "card":
        bank = upi_bank_hint or random.choice(BANK_CODES)
        return "issuer", bank, bank
    elif method == "emandate":
        bank = upi_bank_hint or random.choice(BANK_CODES)
        return "bank", bank, bank
    elif method == "upi":
        vpa, category, bank = _pick_vpa(forced_bank=upi_bank_hint)
        return "vpa", vpa, bank  # bank is None if category B and unforced
    return None, None, None


# ---------------------------------------------------------------------------
# 3. EVENT GENERATION
# ---------------------------------------------------------------------------

def _base_event(method, phase, reason_row, timestamp, outage_id=None, forced_bank=None, latency_ms=None):
    join_type, join_value, resolvable_bank = _join_key_for(method, upi_bank_hint=forced_bank)
    return {
        "transaction_id": _rand_id("pay"),
        "timestamp": int(timestamp.timestamp()),
        "timestamp_iso": timestamp.isoformat(),
        "method": method,
        "payment_phase": phase,
        "amount": random.randint(500, 500000),  # paise
        "currency": "INR",
        "status": "failed",
        "error_source": reason_row["error_source"],
        "error_step": reason_row["error_step"],
        "error_reason": reason_row["error_reason"],
        "join_key_type": join_type,
        "join_key_value": join_value,
        "join_key_resolvable_bank": resolvable_bank,  # None for Category B VPAs
        "latency_ms": latency_ms if latency_ms is not None else random.randint(300, 2500),
        "outage_id": outage_id,
        # --- ground truth, for eval harness only — pipeline must not read these directly ---
        "ground_truth_bucket": reason_row["bucket"],
        "ground_truth_hard_decline": reason_row["hard_decline"],
    }


def generate_background_events(n, start_time, span_hours=72):
    """Scattered, uncorrelated failures across the full time span — the 'normal noise' floor."""
    events = []
    for _ in range(n):
        row = random.choice(REASON_TABLE)
        ts = start_time + timedelta(seconds=random.randint(0, span_hours * 3600))
        events.append(_base_event(row["method"], row["payment_phase"], row, ts))
    return events


def generate_outage_cluster(start_time, cluster_size_range=(15, 60), window_minutes_range=(5, 20)):
    """
    Injects a correlated burst: same method, same bank/issuer, all drawing a
    bank_outage-bucket reason for that method, within a tight window, with
    elevated latency. This is what Stage 1's clustering + Downtime API join
    is meant to catch.
    """
    method = random.choice(["card", "upi", "emandate"])
    outage_reasons = [r for r in REASON_TABLE if r["method"] == method and r["bucket"] == "bank_outage"]
    if not outage_reasons:
        return []
    row = random.choice(outage_reasons)
    if method == "upi":
        # Only banks with a real Category A VPA handle can be made fully
        # resolvable — constrain here rather than let _pick_vpa fall through.
        bank = random.choice(list(set(VPA_CATEGORY_A.values())))
    else:
        bank = random.choice(BANK_CODES)
    outage_id = _rand_id("down", 10)
    window_minutes = random.randint(*window_minutes_range)
    size = random.randint(*cluster_size_range)

    events = []
    for _ in range(size):
        ts = start_time + timedelta(seconds=random.randint(0, window_minutes * 60))
        # For UPI, force a Category-A VPA on the same bank so the cluster is bank-resolvable
        forced_bank = bank
        ev = _base_event(method, row["payment_phase"], row, ts, outage_id=outage_id,
                          forced_bank=forced_bank, latency_ms=random.randint(4000, 12000))
        events.append(ev)
    return events, {"outage_id": outage_id, "method": method, "bank": bank,
                     "reason": row["error_reason"], "start": start_time.isoformat(),
                     "window_minutes": window_minutes, "size": size}


def generate_dataset(num_background=400, num_outage_clusters=4, start_str="2026-08-25T00:00:00"):
    start_time = datetime.fromisoformat(start_str)
    all_events = generate_background_events(num_background, start_time)
    outage_manifest = []

    for _ in range(num_outage_clusters):
        cluster_start = start_time + timedelta(hours=random.randint(0, 68))
        result = generate_outage_cluster(cluster_start)
        if result:
            events, manifest = result
            all_events.extend(events)
            outage_manifest.append(manifest)

    all_events.sort(key=lambda e: e["timestamp"])
    return all_events, outage_manifest


# ---------------------------------------------------------------------------
# 4. RUN + EXPORT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    events, outage_manifest = generate_dataset(num_background=400, num_outage_clusters=4)

    fieldnames = list(events[0].keys())
    with open("data/generated/synthetic_payment_events.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(events)

    with open("data/generated/outage_manifest.json", "w") as f:
        json.dump(outage_manifest, f, indent=2)

    with open("data/generated/reason_table.json", "w") as f:
        json.dump(REASON_TABLE, f, indent=2)

    # Summary stats for a sanity check
    print(f"Total events: {len(events)}")
    from collections import Counter
    print("By method:", Counter(e["method"] for e in events))
    print("By bucket:", Counter(e["ground_truth_bucket"] for e in events))
    print("Hard decline count:", sum(1 for e in events if e["ground_truth_hard_decline"]))
    print("Outage clusters injected:", len(outage_manifest))
    for m in outage_manifest:
        print(" -", m)
