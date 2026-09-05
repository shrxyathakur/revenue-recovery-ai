"""
Stage 3 — Recovery Action Execution

Deterministic, bounded action selection based on Stage 2's diagnosis_bucket.
"Bounded" means: this stage picks exactly ONE action per event, with an
explicit max_auto_retries cap — it never loops or auto-schedules unlimited
retries itself. Enforcing that cap over time (e.g. across repeated failures
of the same transaction) is Stage 4's job (guardrails), not this stage's.

Action policy per bucket, and why:

  genuine_decline -> customer_retry_prompt, immediate (retry_after_minutes=0)
    The failure is customer-side and actionable right now (wrong OTP,
    insufficient funds, wrong CVV) — there's nothing to wait for. Delaying
    this would just be worse UX with no upside.

  bank_outage -> scheduled_retry, timed past the outage window
    Retrying during a live outage is pointless. If Stage 1 gave us a real
    cluster window_end, schedule the retry a safety buffer after it closes.
    If bank_outage came from a table-lookup on an isolated event with no
    cluster window, fall back to a conservative fixed delay instead of
    guessing at an outage duration we have no evidence for.

  fraud_fp -> escalate_manual_review, no auto-retry ever
    Auto-retrying a risk-check failure could compound the flag or bypass a
    control the bank deliberately raised. This always goes to a human.

  uncertain -> single_delayed_retry, one narrow attempt
    Per the reason_bucket_map's own stated policy: don't take no action,
    but don't overcommit either. One retry, modest delay, and critically —
    no customer-facing claim about *why* it failed, since we don't
    actually know.
"""

import argparse
import csv
import os
import sys
from typing import Optional

# Make the repo root importable regardless of current working directory —
# audit_trail.py lives one level up from pipeline/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit_trail

GENUINE_DECLINE_DELAY_MINUTES = 0
BANK_OUTAGE_WINDOW_BUFFER_MINUTES = 15
BANK_OUTAGE_DEFAULT_DELAY_MINUTES = 30
UNCERTAIN_DELAY_MINUTES = 60

MAX_AUTO_RETRIES = {
    "genuine_decline": 1,
    "bank_outage": 1,
    "fraud_fp": 0,
    "uncertain": 1,
}


def decide_recovery_action(diagnosis: dict, cluster: dict) -> dict:
    bucket = diagnosis["diagnosis_bucket"]

    if bucket == "genuine_decline":
        return {
            "recovery_action": "customer_retry_prompt",
            "retry_after_minutes": GENUINE_DECLINE_DELAY_MINUTES,
            "max_auto_retries": MAX_AUTO_RETRIES["genuine_decline"],
            "action_rationale": "Customer-actionable failure — prompt immediate retry, nothing to wait on.",
        }

    if bucket == "bank_outage":
        window_end = cluster.get("window_end")
        if window_end:
            return {
                "recovery_action": "scheduled_retry",
                "retry_after_minutes": BANK_OUTAGE_WINDOW_BUFFER_MINUTES,
                "max_auto_retries": MAX_AUTO_RETRIES["bank_outage"],
                "action_rationale": (
                    f"Corroborated outage, window closed at {window_end} — "
                    f"scheduling retry {BANK_OUTAGE_WINDOW_BUFFER_MINUTES} min after window "
                    "close as a safety buffer, not immediately at close."
                ),
            }
        return {
            "recovery_action": "scheduled_retry",
            "retry_after_minutes": BANK_OUTAGE_DEFAULT_DELAY_MINUTES,
            "max_auto_retries": MAX_AUTO_RETRIES["bank_outage"],
            "action_rationale": (
                "bank_outage diagnosed via table lookup with no cluster window available — "
                "using a conservative fixed delay rather than guessing at outage duration."
            ),
        }

    if bucket == "fraud_fp":
        return {
            "recovery_action": "escalate_manual_review",
            "retry_after_minutes": None,
            "max_auto_retries": MAX_AUTO_RETRIES["fraud_fp"],
            "action_rationale": "Risk-check failure — never auto-retried, routed to manual review.",
        }

    if bucket == "uncertain":
        return {
            "recovery_action": "single_delayed_retry",
            "retry_after_minutes": UNCERTAIN_DELAY_MINUTES,
            "max_auto_retries": MAX_AUTO_RETRIES["uncertain"],
            "action_rationale": (
                f"Diagnosis stayed uncertain ({diagnosis.get('resolution_pending_on')}) — "
                "one narrow retry, no claim made about root cause."
            ),
        }

    raise ValueError(f"Unknown diagnosis_bucket: {bucket}")


def run_stage3_over_csv(diagnosed_csv_path: str) -> list[dict]:
    with open(diagnosed_csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    results = []
    for row in rows:
        diagnosis = {
            "diagnosis_bucket": row["diagnosis_bucket"],
            "resolution_pending_on": row["resolution_pending_on"] or None,
            "resolved_via": row["resolved_via"],
        }
        cluster = {
            "cluster_id": row.get("cluster_id") or None,
            "window_end": row.get("window_end") or None,
        }
        action = decide_recovery_action(diagnosis, cluster)

        event_id = row["transaction_id"]
        audit_trail.log_event(
            stage="stage3_recovery",
            event_id=event_id,
            data={"diagnosis_bucket": diagnosis["diagnosis_bucket"], "action": action},
        )

        results.append({**row, **action})
    return results


if __name__ == "__main__":
    from collections import Counter

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/generated/stage2_diagnosed_events.csv")
    parser.add_argument("--output", default="data/generated/stage3_recovery_actions.csv")
    args = parser.parse_args()

    results = run_stage3_over_csv(args.input)

    fieldnames = list(results[0].keys())
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"Total events: {len(results)}")
    print("By recovery_action:", Counter(r["recovery_action"] for r in results))
    print("By bucket -> action:")
    combo = Counter((r["diagnosis_bucket"], r["recovery_action"]) for r in results)
    for (bucket, action), count in sorted(combo.items()):
        print(f"  {bucket:17s} -> {action:23s} : {count}")
