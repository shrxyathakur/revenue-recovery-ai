"""
audit_trail.py — shared audit log, used by every pipeline stage.

Append-only JSONL. One line per logged event: {timestamp, stage, event_id, data}.
Each stage calls log_event() with its own record shape in `data` — this
module doesn't care what's inside, just that it's JSON-serializable.
"""

import json
import os
from datetime import datetime, timezone

DEFAULT_LOG_PATH = "audit_log.jsonl"


def log_event(stage: str, event_id: str, data: dict, log_path: str = DEFAULT_LOG_PATH) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "event_id": event_id,
        "data": data,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_events(stage: str = None, event_id: str = None, log_path: str = DEFAULT_LOG_PATH) -> list[dict]:
    """Read back logged records, optionally filtered. Returns [] if the log doesn't exist yet."""
    if not os.path.exists(log_path):
        return []
    results = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if stage and record["stage"] != stage:
                continue
            if event_id and record["event_id"] != event_id:
                continue
            results.append(record)
    return results


if __name__ == "__main__":
    test_path = "audit_log_test.jsonl"
    if os.path.exists(test_path):
        os.remove(test_path)
    log_event("stage2_diagnosis", "pay_test001", {"diagnosis_bucket": "bank_outage"}, log_path=test_path)
    log_event("stage2_diagnosis", "pay_test002", {"diagnosis_bucket": "uncertain"}, log_path=test_path)
    all_records = read_events(log_path=test_path)
    stage2_only = read_events(stage="stage2_diagnosis", log_path=test_path)
    one_event = read_events(event_id="pay_test001", log_path=test_path)
    print(f"all: {len(all_records)}, stage2_only: {len(stage2_only)}, one_event: {len(one_event)}")
    assert len(all_records) == 2 and len(stage2_only) == 2 and len(one_event) == 1
    os.remove(test_path)
    print("audit_trail.py smoke test passed")
