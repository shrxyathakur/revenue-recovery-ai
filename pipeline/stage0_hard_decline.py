"""
Stage 0 — Hard Decline Pre-Filter

Rule-based, deterministic. No LLM call happens here — that's Stage 2's job.
Looks up each event's (method, payment_phase, error_reason) against the
reason table (reason_table.json, generated earlier from
reason_bucket_map_card_upi_emandate.md) and exits it immediately if it's a
confirmed Hard Decline. Everything else passes through to Stage 1.

Fail-safe rule: an event whose reason isn't found in the table at all is
NEVER auto-exited here — it's passed through, flagged as "unmapped", so an
unknown case still gets a chance at Stage 1/2 rather than being silently
dropped on a rule we're not confident about.
"""

import csv
import json


def _norm_phase(value):
    """CSV empty strings and JSON nulls both mean 'no phase' — normalize to None."""
    return value if value else None


def load_lookup_table(reason_table_path):
    """
    Builds a dict keyed on (method, payment_phase, error_reason) -> hard_decline bool.
    This is the entire rule set Stage 0 runs against.
    """
    with open(reason_table_path, encoding="utf-8") as f:
        rows = json.load(f)

    lookup = {}
    for row in rows:
        key = (row["method"], _norm_phase(row["payment_phase"]), row["error_reason"])
        lookup[key] = row["hard_decline"]
    return lookup


def apply_stage0(event, lookup):
    """
    Returns a dict describing Stage 0's decision for one event.
    Does not mutate the event.
    """
    key = (event["method"], _norm_phase(event["payment_phase"]), event["error_reason"])

    if key not in lookup:
        return {
            "stage0_decision": "pass_through_unmapped",
            "exited_pipeline": False,
            "hard_decline_confirmed": None,  # unknown, not False — important distinction
            "lookup_key": key,
        }

    is_hard_decline = lookup[key]
    return {
        "stage0_decision": "exit_hard_decline" if is_hard_decline else "pass_to_stage1",
        "exited_pipeline": is_hard_decline,
        "hard_decline_confirmed": is_hard_decline,
        "lookup_key": key,
    }


def run_stage0_over_dataset(events_csv_path, reason_table_path):
    lookup = load_lookup_table(reason_table_path)

    with open(events_csv_path, encoding="utf-8") as f:
        events = list(csv.DictReader(f))

    exited = []
    passed = []
    unmapped = []

    for event in events:
        decision = apply_stage0(event, lookup)
        enriched = {**event, **decision}
        # lookup_key is a tuple, not CSV/JSON-safe as-is — stringify for output
        enriched["lookup_key"] = str(decision["lookup_key"])

        if decision["stage0_decision"] == "exit_hard_decline":
            exited.append(enriched)
        elif decision["stage0_decision"] == "pass_through_unmapped":
            unmapped.append(enriched)
            passed.append(enriched)  # unmapped still proceeds to Stage 1
        else:
            passed.append(enriched)

    return exited, passed, unmapped


if __name__ == "__main__":
    exited, passed, unmapped = run_stage0_over_dataset(
        "data/generated/synthetic_payment_events.csv",
        "data/generated/reason_table.json",
    )

    print(f"Total events in: {len(exited) + len(passed)}")
    print(f"Exited at Stage 0 (Hard Decline): {len(exited)}")
    print(f"Passed to Stage 1: {len(passed)}")
    print(f"  of which unmapped (reason not in table): {len(unmapped)}")
    print()

    # Sanity check against the generator's own ground_truth_hard_decline flag —
    # Stage 0's decision should agree with it 100% of the time, since both
    # ultimately derive from the same reason table. If they disagree, that's
    # a real bug worth catching, not something to gloss over.
    with open("data/generated/synthetic_payment_events.csv", encoding="utf-8") as f:
        all_events = list(csv.DictReader(f))
    lookup = load_lookup_table("data/generated/reason_table.json")

    mismatches = 0
    for event in all_events:
        decision = apply_stage0(event, lookup)
        ground_truth = event["ground_truth_hard_decline"] == "True"
        if decision["hard_decline_confirmed"] is not None and decision["hard_decline_confirmed"] != ground_truth:
            mismatches += 1
    print(f"Mismatches vs generator's ground_truth_hard_decline (should be 0): {mismatches}")

    # Write outputs
    if exited:
        with open("data/generated/stage0_exited_hard_decline.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(exited[0].keys()))
            writer.writeheader()
            writer.writerows(exited)

    if passed:
        with open("data/generated/stage0_passed_to_stage1.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(passed[0].keys()))
            writer.writeheader()
            writer.writerows(passed)

    print()
    print("Exited events, breakdown by method:")
    from collections import Counter
    print(Counter(e["method"] for e in exited))
    print("Exited events, breakdown by reason (top 5):")
    print(Counter(e["error_reason"] for e in exited).most_common(5))
