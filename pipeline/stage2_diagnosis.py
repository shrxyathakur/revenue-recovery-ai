"""
Stage 2 — Diagnosis

Two-path design (deliberate, not a simplification):

  Path A — Deterministic table lookup.
    Most error_reasons resolve to a bucket unambiguously just from reading
    Razorpay's own doc text (see reason_bucket_map_card_upi_emandate.md).
    Card: 13/15 non-hard-decline reasons resolve this way. UPI: 6/10.
    E-mandate: 24/32. No LLM call, no cost, no hallucination risk, fully
    auditable as a pure function of (method, payment_phase, error_reason).

  Path B — LLM reasoning over cluster context.
    Reserved for the ~25% of reasons the table itself marks `uncertain`
    (opaque reason text, ambiguous root cause) PLUS any error_reason not
    found in the table at all (schema drift / new failure mode Razorpay
    hasn't documented, or a data bug). The LLM's job here is NOT "read the
    reason code" (a lookup already tried and failed) — it's "given cluster
    corroboration (size, timing, Downtime API hit), does this isolated
    ambiguity resolve into bank_outage, or does it genuinely stay uncertain."

  Every event's output carries `resolved_via` so the audit trail can prove
  the split actually happened, not just claim it in the pitch.

Output contract per event:
  {
    "diagnosis_bucket": "genuine_decline" | "bank_outage" | "fraud_fp" | "uncertain",
    "resolution_pending_on": str | None,   # set only when bucket == "uncertain"
    "resolved_via": "table_lookup" | "table_lookup_miss" | "llm_cluster_reasoning",
    "reasoning": str | None,               # LLM's stated reasoning, path B only
  }
"""

import json
import os
import sys
from typing import Optional, TypedDict

# Make the repo root importable regardless of the current working directory —
# audit_trail.py lives at the repo root, one level up from pipeline/, and
# `python .\pipeline\stage2_diagnosis.py` only puts pipeline/ itself on
# sys.path by default.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Deterministic tables — transcribed directly from
# reason_bucket_map_card_upi_emandate.md. Keep this file and that doc in
# sync; if you edit one, edit the other (file-sync discipline).
# ---------------------------------------------------------------------------

CARD_TABLE = {
    "authentication_failed": "genuine_decline",
    "insufficient_funds": "genuine_decline",
    "incorrect_cvv": "genuine_decline",
    "payment_cancelled": "genuine_decline",
    "card_not_enrolled": "genuine_decline",
    "card_disabled_for_online_payments": "genuine_decline",
    "debit_instrument_inactive": "genuine_decline",
    "debit_instrument_blocked": "genuine_decline",
    "card_expired": "genuine_decline",
    "transaction_limit_exceeded": "genuine_decline",
    "bank_technical_error": "bank_outage",
    "gateway_technical_error": "bank_outage",
    "payment_risk_check_failed": "fraud_fp",
    "card_declined": "uncertain",
    "payment_failed": "uncertain",
}

UPI_TABLE = {
    "insufficient_funds": "genuine_decline",
    "payment_cancelled": "genuine_decline",
    "invalid_vpa": "genuine_decline",
    "bank_technical_error": "bank_outage",
    "gateway_technical_error": "bank_outage",
    "credit_failed": "bank_outage",
    "payment_collect_request_expired": "uncertain",
    "payment_declined": "uncertain",
    "payment_timed_out": "uncertain",
    "vpa_resolution_failed": "uncertain",
}

EMANDATE_SHARED = {
    "bank_account_invalid": "genuine_decline",
    "bank_account_validation_failed": "uncertain",
    "bank_technical_error": "bank_outage",
    "debit_instrument_blocked": "genuine_decline",
    "debit_instrument_inactive": "genuine_decline",
    "gateway_technical_error": "bank_outage",
    "insufficient_funds": "genuine_decline",
    "payment_cancelled": "genuine_decline",
    "payment_failed": "uncertain",
    "payment_timed_out": "uncertain",
    "server_error": "bank_outage",
    "transaction_limit_exceeded": "genuine_decline",
}

EMANDATE_REGISTRATION_ONLY = {
    "already_declined": "genuine_decline",
    "authentication_failed": "genuine_decline",
    "card_expired": "genuine_decline",
    "card_number_invalid": "genuine_decline",
    "duplicate_request": "uncertain",
    "incorrect_card_expiry_date": "genuine_decline",
    "incorrect_cvv": "genuine_decline",
    "incorrect_otp": "genuine_decline",
    "incorrect_pin": "genuine_decline",
    "joint_account_not_allowed": "genuine_decline",
    "otp_attempts_exceeded": "genuine_decline",
    "payment_pending_approval": "uncertain",
    "payment_risk_check_failed": "fraud_fp",
    "user_not_registered_for_netbanking": "genuine_decline",
}

EMANDATE_SUBSEQUENT_ONLY = {
    "incorrect_ifsc": "genuine_decline",
    "input_validation_failed": "uncertain",
    "invalid_amount": "uncertain",
    "mandate_not_active": "genuine_decline",
    "payment_declined": "uncertain",
    "payment_mandate_not_active": "bank_outage",
}

EMANDATE_REGISTRATION = {**EMANDATE_SHARED, **EMANDATE_REGISTRATION_ONLY}
EMANDATE_SUBSEQUENT = {**EMANDATE_SHARED, **EMANDATE_SUBSEQUENT_ONLY}

# Reasons that should NEVER appear for a given (method, phase) combo.
# Schema violations worth asserting against — from the table's cross-method
# takeaway #2.
SCHEMA_ILLEGAL = {
    ("upi", None, "payment_risk_check_failed"),
    ("emandate", "subsequent", "payment_risk_check_failed"),
}

# Short human-readable hint for what data WOULD resolve each uncertain
# reason, if cluster corroboration doesn't. Used to populate
# resolution_pending_on when the LLM path also can't resolve it.
RESOLUTION_HINTS = {
    "card_declined": "error_description text needed (Razorpay doesn't expose specific failure detail for this reason)",
    "payment_failed": "error_description text needed / cluster corroboration against Downtime API",
    "payment_collect_request_expired": "cannot distinguish customer abandonment vs. stuck-waiting from reason alone",
    "payment_declined": "error_description text needed (generic 'funds could not be debited')",
    "payment_timed_out": "cluster corroboration against Downtime API",
    "vpa_resolution_failed": "requires manual ticket per Razorpay's own documented next step",
    "bank_account_validation_failed": "cannot distinguish bad customer input vs. flaky third-party validator",
    "payment_pending_approval": "not actually a failure yet — recheck payment status before diagnosing",
    "duplicate_request": "integration/business-side signal, not a customer failure — flag error_source",
    "input_validation_failed": "integration-side error (check request payload), not a customer decline",
    "invalid_amount": "integration-side error (check request payload), not a customer decline",
}


def _lookup(method: str, payment_phase: Optional[str], error_reason: str) -> Optional[str]:
    if method == "card":
        return CARD_TABLE.get(error_reason)
    if method == "upi":
        return UPI_TABLE.get(error_reason)
    if method == "emandate":
        table = EMANDATE_REGISTRATION if payment_phase == "registration" else EMANDATE_SUBSEQUENT
        return table.get(error_reason)
    return None


# ---------------------------------------------------------------------------
# Cluster context — this is the REAL shape stage1_detection.py emits per
# event (confirmed by running it against real synthetic data, not assumed).
# Solo events (never corroborated) have cluster_id=None, cluster_size=1,
# downtime_api_hit=None, entity=<bank or None>, window_start/end=None.
# ---------------------------------------------------------------------------

class ClusterContext(TypedDict, total=False):
    cluster_id: Optional[str]
    cluster_size: int
    same_reason_count: int
    downtime_api_hit: Optional[bool]
    entity: Optional[str]
    window_start: Optional[str]
    window_end: Optional[str]


# ---------------------------------------------------------------------------
# Path B — LLM reasoning, called only for table-uncertain / table-miss cases.
# ---------------------------------------------------------------------------

MODEL_NAME = "claude-sonnet-5"

SYSTEM_PROMPT = """You are the Stage 2 diagnosis component of a payment degradation \
detection pipeline. You are called ONLY for error_reasons that a deterministic \
lookup table already flagged as ambiguous from the reason code alone. Your job is \
NOT to re-derive the reason code's meaning — it's to decide, using cluster \
corroboration evidence, whether this specific instance resolves to bank_outage, \
or whether it must honestly remain uncertain.

Rules:
- Only output "bank_outage" if the cluster evidence supports it: a meaningful \
cluster_size/same_reason_count AND (ideally) a positive downtime_api_hit against \
the same entity in the same window. A single isolated event with no corroboration \
should almost always stay "uncertain" — do not guess.
- Never output "genuine_decline" or "fraud_fp" from this path. Those buckets are \
reason-code-deterministic and already handled before you were called; if you \
believe the evidence points that way, say so in reasoning but still return \
"uncertain" with resolution_pending_on explaining why the table's uncertain \
classification may need re-review — do not silently override the table.
- Respond ONLY with a JSON object, no preamble, no markdown fences:
{"diagnosis_bucket": "bank_outage" | "uncertain", "resolution_pending_on": string or null, "reasoning": string}
"""


def _build_user_prompt(event: dict, cluster: ClusterContext) -> str:
    return json.dumps({
        "method": event.get("method"),
        "payment_phase": event.get("payment_phase"),
        "error_reason": event.get("error_reason"),
        "error_source": event.get("error_source"),
        "error_step": event.get("error_step"),
        "table_hint": RESOLUTION_HINTS.get(event.get("error_reason"), "reason not in reference table at all — possible schema drift"),
        "cluster_context": cluster,
    }, indent=2)


def _llm_diagnose_live(event: dict, cluster: ClusterContext) -> dict:
    # Requires: pip install anthropic --break-system-packages
    # Requires: ANTHROPIC_API_KEY set in environment
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(event, cluster)}],
    )
    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(text)
    return parsed


def _llm_diagnose_mock(event: dict, cluster: ClusterContext) -> dict:
    """
    Deterministic stand-in for the LLM call, so you can develop/demo Stage 2
    without burning API calls or depending on network access. Mirrors the
    SYSTEM_PROMPT's rule set exactly, just without an actual model.
    """
    size = cluster.get("cluster_size", 1)
    same_reason = cluster.get("same_reason_count", 1)
    downtime_hit = cluster.get("downtime_api_hit")

    if size >= 5 and same_reason >= 5 and downtime_hit is True:
        return {
            "diagnosis_bucket": "bank_outage",
            "resolution_pending_on": None,
            "reasoning": (
                f"[mock] cluster_size={size}, same_reason_count={same_reason}, "
                f"downtime_api_hit=True against entity={cluster.get('entity')} — "
                "corroborated outage, resolving uncertain reason to bank_outage."
            ),
        }

    reason = event.get("error_reason")
    hint = RESOLUTION_HINTS.get(reason, "reason not found in reference table at all — possible schema drift, needs manual review")
    return {
        "diagnosis_bucket": "uncertain",
        "resolution_pending_on": hint,
        "reasoning": (
            f"[mock] cluster_size={size}, same_reason_count={same_reason}, "
            f"downtime_api_hit={downtime_hit} — insufficient corroboration to "
            "resolve; staying uncertain rather than guessing."
        ),
    }


def _llm_diagnose(event: dict, cluster: ClusterContext) -> dict:
    mode = os.environ.get("STAGE2_LLM_MODE", "mock")
    if mode == "live":
        return _llm_diagnose_live(event, cluster)
    return _llm_diagnose_mock(event, cluster)


# ---------------------------------------------------------------------------
# Audit trail hook — wired to the real audit_trail.py interface:
#   log_event(stage: str, event_id: str, data: dict, log_path: str = ...)
# ---------------------------------------------------------------------------

def _log_audit(event_id: str, event: dict, cluster: dict, result: dict) -> None:
    import audit_trail
    audit_trail.log_event(
        stage="stage2_diagnosis",
        event_id=event_id,
        data={"event": event, "cluster": cluster, "result": result},
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def diagnose_event(event: dict, cluster: Optional[ClusterContext] = None) -> dict:
    """
    event: dict with at least method, error_reason, and payment_phase
           (payment_phase should be None for card/upi).
    cluster: cluster context from Stage 1; pass an empty dict / None if this
             event wasn't part of any detected cluster (isolated event).
    """
    cluster = cluster or {"cluster_size": 1, "same_reason_count": 1, "downtime_api_hit": None, "entity": None}

    method = event.get("method")
    phase = event.get("payment_phase")
    reason = event.get("error_reason")

    # Schema validation — assert against known-illegal combinations rather
    # than silently diagnosing them.
    key = (method, phase, reason)
    if key in SCHEMA_ILLEGAL or (method, None, reason) in SCHEMA_ILLEGAL:
        raise ValueError(f"Schema violation: {reason} should never appear for method={method}, phase={phase}")

    bucket = _lookup(method, phase, reason)

    event_id = event.get("transaction_id", "unknown")

    if bucket is None:
        # Reason not in our reference table at all — treat as uncertain and
        # let the LLM path flag it as possible schema drift, don't crash.
        result = _llm_diagnose(event, cluster)
        result["resolved_via"] = "table_lookup_miss"
        _log_audit(event_id, event, cluster, result)
        return result

    if bucket != "uncertain":
        result = {
            "diagnosis_bucket": bucket,
            "resolution_pending_on": None,
            "resolved_via": "table_lookup",
            "reasoning": None,
        }
        _log_audit(event_id, event, cluster, result)
        return result

    # Path B — table says uncertain, cluster context goes to the LLM.
    result = _llm_diagnose(event, cluster)
    result["resolved_via"] = "llm_cluster_reasoning"
    _log_audit(event_id, event, cluster, result)
    return result


def run_stage2_over_csv(enriched_csv_path: str) -> list[dict]:
    """
    Reads stage1_enriched_events.csv (real Stage 1 output) and runs every
    event through diagnose_event, reconstructing the ClusterContext from
    each row's own cluster columns.
    """
    import csv as csv_module

    with open(enriched_csv_path) as f:
        rows = list(csv_module.DictReader(f))

    results = []
    for row in rows:
        event = {
            "transaction_id": row["transaction_id"],
            "method": row["method"],
            "payment_phase": row["payment_phase"] or None,
            "error_reason": row["error_reason"],
            "error_source": row["error_source"],
            "error_step": row["error_step"],
        }
        cluster = {
            "cluster_id": row["cluster_id"] or None,
            "cluster_size": int(row["cluster_size"]),
            "same_reason_count": int(row["same_reason_count"]),
            "downtime_api_hit": {"True": True, "False": False, "": None}.get(row["downtime_api_hit"], None),
            "entity": row["entity"] or None,
            "window_start": row["window_start"] or None,
            "window_end": row["window_end"] or None,
        }
        out = diagnose_event(event, cluster)
        results.append({**event, **out})
    return results


if __name__ == "__main__":
    import argparse
    import csv as csv_module
    from collections import Counter

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/generated/stage1_enriched_events.csv")
    parser.add_argument("--output", default="data/generated/stage2_diagnosed_events.csv")
    args = parser.parse_args()

    results = run_stage2_over_csv(args.input)

    fieldnames = list(results[0].keys())
    with open(args.output, "w", newline="") as f:
        writer = csv_module.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"Total events diagnosed: {len(results)}")
    print("By bucket:", Counter(r["diagnosis_bucket"] for r in results))
    print("By resolved_via:", Counter(r["resolved_via"] for r in results))
    uncertain_count = sum(1 for r in results if r["diagnosis_bucket"] == "uncertain")
    print(f"Uncertain rate: {uncertain_count}/{len(results)} = {uncertain_count/len(results):.1%}")