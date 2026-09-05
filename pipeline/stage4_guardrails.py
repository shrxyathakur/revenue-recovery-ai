"""
Stage 4 — Guardrails / Stopping Rules

Watches Stage 3's proposed actions at the BATCH level, not the per-event
level — this is the key distinction from Stages 0-3. A single event can look
perfectly fine in isolation while the aggregate pattern across all events is
a red flag (diagnosis system going haywire, a compliance rule silently
broken, a retry storm building against one bank). Stage 4 exists to catch
that class of problem.

Three guardrails, each with a concrete rationale:

1. UNCERTAIN-RATE SPIKE DETECTOR
   reason_bucket_map_card_upi_emandate.md predicts a ~25% baseline uncertain
   rate across the reason table. If the observed rate in a real batch blows
   well past that, it's not "normal ambiguity" anymore — it's a signal
   something upstream broke (bad data, a new undocumented failure mode,
   Stage 2 itself malfunctioning). Response: downgrade `uncertain` events'
   action from single_delayed_retry to hold_for_manual_review until a human
   looks, rather than quietly auto-retrying into an anomaly.

2. FRAUD NEVER-AUTO-RETRY COMPLIANCE CHECK
   Stage 3 already sets max_auto_retries=0 for fraud_fp. This guardrail is
   defense-in-depth: it re-asserts that invariant independently, so a future
   change to Stage 3's logic that accidentally breaks this gets caught here
   and force-corrected, rather than silently auto-retrying a risk-flagged
   transaction.

3. PER-ENTITY RETRY CIRCUIT BREAKER
   If one bank/entity accumulates too many simultaneous auto-retry actions,
   firing them all at once risks a "retry storm" against a bank that may
   already be struggling — the opposite of the intended effect. Excess
   retries beyond the cap get downgraded to throttled_hold rather than
   fired immediately. The cap is a CLI parameter, not a hardcoded constant —
   same reasoning as Stage 1's clustering window: a tunable, statable
   architectural choice, not a magic number.

Every override this stage makes is logged to the audit trail with the
guardrail name and rationale — this is what makes "we watch for problems"
a provable claim in the audit log, not just something said in the pitch.
"""

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audit_trail

UNCERTAIN_RATE_BASELINE = 0.25
UNCERTAIN_RATE_ALERT_MULTIPLIER = 2.0  # trip if observed rate > 2x baseline

RETRY_ACTIONS = {"customer_retry_prompt", "scheduled_retry"}


def _check_uncertain_rate(rows: list[dict]) -> dict:
    total = len(rows)
    uncertain_count = sum(1 for r in rows if r["diagnosis_bucket"] == "uncertain")
    rate = uncertain_count / total if total else 0.0
    threshold = UNCERTAIN_RATE_BASELINE * UNCERTAIN_RATE_ALERT_MULTIPLIER
    tripped = rate > threshold
    return {
        "guardrail": "uncertain_rate_spike",
        "observed_rate": round(rate, 4),
        "baseline": UNCERTAIN_RATE_BASELINE,
        "threshold": threshold,
        "tripped": tripped,
    }


def _apply_uncertain_rate_guardrail(rows: list[dict], check: dict) -> list[dict]:
    if not check["tripped"]:
        return rows
    for row in rows:
        if row["diagnosis_bucket"] == "uncertain" and row["recovery_action"] == "single_delayed_retry":
            row["recovery_action"] = "hold_for_manual_review"
            row["retry_after_minutes"] = None
            row["max_auto_retries"] = 0
            row["guardrail_override"] = "uncertain_rate_spike"
            audit_trail.log_event(
                stage="stage4_guardrails",
                event_id=row["transaction_id"],
                data={"guardrail": "uncertain_rate_spike", "action_before": "single_delayed_retry",
                      "action_after": "hold_for_manual_review", "observed_rate": check["observed_rate"]},
            )
    return rows


def _check_fraud_compliance(rows: list[dict]) -> dict:
    violations = [r for r in rows if r["diagnosis_bucket"] == "fraud_fp" and int(r["max_auto_retries"]) != 0]
    return {"guardrail": "fraud_never_auto_retry", "violations_found": len(violations), "tripped": len(violations) > 0}


def _apply_fraud_compliance_guardrail(rows: list[dict], check: dict) -> list[dict]:
    if not check["tripped"]:
        return rows
    for row in rows:
        if row["diagnosis_bucket"] == "fraud_fp" and int(row["max_auto_retries"]) != 0:
            row["recovery_action"] = "escalate_manual_review"
            row["retry_after_minutes"] = None
            row["max_auto_retries"] = 0
            row["guardrail_override"] = "fraud_never_auto_retry"
            audit_trail.log_event(
                stage="stage4_guardrails",
                event_id=row["transaction_id"],
                data={"guardrail": "fraud_never_auto_retry",
                      "note": "compliance invariant violated upstream, force-corrected here"},
            )
    return rows


def _check_entity_retry_volume(rows: list[dict], cap: int) -> dict:
    counts = Counter(r["entity"] for r in rows if r.get("entity") and r["recovery_action"] in RETRY_ACTIONS)
    tripped_entities = {entity: count for entity, count in counts.items() if count > cap}
    return {
        "guardrail": "entity_retry_circuit_breaker",
        "cap": cap,
        "tripped_entities": tripped_entities,
        "tripped": len(tripped_entities) > 0,
    }


def _apply_entity_retry_guardrail(rows: list[dict], check: dict) -> list[dict]:
    if not check["tripped"]:
        return rows
    by_entity = defaultdict(list)
    for row in rows:
        if row.get("entity") and row["recovery_action"] in RETRY_ACTIONS:
            by_entity[row["entity"]].append(row)

    for entity, entity_rows in by_entity.items():
        cap = check["cap"]
        if len(entity_rows) <= cap:
            continue
        for row in entity_rows[cap:]:
            row["recovery_action"] = "throttled_hold"
            row["guardrail_override"] = "entity_retry_circuit_breaker"
            audit_trail.log_event(
                stage="stage4_guardrails",
                event_id=row["transaction_id"],
                data={"guardrail": "entity_retry_circuit_breaker", "entity": entity,
                      "cap": cap, "entity_total_retries": len(entity_rows)},
            )
    return rows


def run_stage4(recovery_csv_path: str, entity_retry_cap: int = 30) -> tuple[list[dict], dict]:
    with open(recovery_csv_path) as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        row.setdefault("guardrail_override", None)

    uncertain_check = _check_uncertain_rate(rows)
    rows = _apply_uncertain_rate_guardrail(rows, uncertain_check)

    fraud_check = _check_fraud_compliance(rows)
    rows = _apply_fraud_compliance_guardrail(rows, fraud_check)

    entity_check = _check_entity_retry_volume(rows, entity_retry_cap)
    rows = _apply_entity_retry_guardrail(rows, entity_check)

    report = {
        "uncertain_rate_check": uncertain_check,
        "fraud_compliance_check": fraud_check,
        "entity_retry_check": entity_check,
    }
    return rows, report


if __name__ == "__main__":
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/generated/stage3_recovery_actions.csv")
    parser.add_argument("--output", default="data/generated/stage4_final_actions.csv")
    parser.add_argument("--report-output", default="data/generated/stage4_guardrail_report.json")
    parser.add_argument("--entity-retry-cap", type=int, default=30)
    args = parser.parse_args()

    rows, report = run_stage4(args.input, entity_retry_cap=args.entity_retry_cap)

    fieldnames = list(rows[0].keys())
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with open(args.report_output, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Total events: {len(rows)}")
    print()
    print("Guardrail report:")
    print(json.dumps(report, indent=2))
    print()
    print("Final recovery_action breakdown:", Counter(r["recovery_action"] for r in rows))
    overridden = sum(1 for r in rows if r.get("guardrail_override"))
    print(f"Events overridden by a guardrail: {overridden}")
