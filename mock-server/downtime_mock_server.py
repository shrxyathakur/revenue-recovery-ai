"""
Downtime API mock server.

Why this exists: the real Razorpay Downtime API requires sandbox access,
which needs PAN/KYC submission you don't have. This mocks the *shape* of
that API — a lookup by bank/VPA handle returning active downtime status —
well enough for Stage 1's clustering to correlate against, and Stage 2's
`downtime_api_hit` field to be a real HTTP call instead of a hardcoded
lookup into outage_manifest.json.

IMPORTANT — schema fidelity caveat: the field names below (`entity`,
`status`, `started_at`) are a reasonable approximation based on general
downtime-API conventions, NOT verified line-by-line against the real
Razorpay Downtime API doc or Postman workspace in this session. Before you
put "matches Razorpay's real schema" in your pitch, actually fetch and
diff against https://razorpay.com/docs/ (search "Downtime API") or the
Postman workspace — don't assert fidelity you haven't checked.

Demo trick: instead of the outage state being static from process start,
use POST /admin/inject-outage to flip an entity into "active downtime"
live during your demo, right before you show Stage 1/2 picking it up.
This is much stronger for "what broke at 2am" Q&A than a fixture file,
because you can actually show the break happening.

Run:
    pip install flask --break-system-packages
    python3 downtime_mock_server.py
    # serves on http://localhost:5001

Endpoints:
    GET  /downtime?entity=hdfc&method=upi   -> current status for one entity
    GET  /downtime                          -> all active downtime entries
    POST /admin/inject-outage               -> body: {"entity": "hdfc", "method": "upi", "reason": "core banking maintenance"}
    POST /admin/resolve-outage              -> body: {"entity": "hdfc"}
    GET  /admin/state                       -> full in-memory state, for debugging
"""

import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request

app = Flask(__name__)

# In-memory store: entity (bank code or vpa handle) -> outage record.
# Kept deliberately simple (dict, not a DB) since this only needs to
# survive one demo process's lifetime.
_OUTAGES: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed():
    """A couple of pre-seeded outages so the server isn't empty on cold start."""
    _OUTAGES["hdfc"] = {
        "id": str(uuid.uuid4()),
        "entity": "hdfc",
        "method": "upi",
        "status": "active",
        "reason": "seeded demo outage — core banking maintenance",
        "started_at": _now(),
        "resolved_at": None,
    }


@app.route("/downtime", methods=["GET"])
def get_downtime():
    entity = request.args.get("entity")
    method = request.args.get("method")

    records = list(_OUTAGES.values())
    if entity:
        records = [r for r in records if r["entity"] == entity]
    if method:
        records = [r for r in records if r["method"] == method]

    # Mirror a plausible real-API shape: only return currently-active
    # entries by default, unless caller explicitly wants history.
    include_resolved = request.args.get("include_resolved", "false").lower() == "true"
    if not include_resolved:
        records = [r for r in records if r["status"] == "active"]

    return jsonify({"downtimes": records, "count": len(records)})


@app.route("/admin/inject-outage", methods=["POST"])
def inject_outage():
    body = request.get_json(force=True) or {}
    entity = body.get("entity")
    method = body.get("method", "upi")
    reason = body.get("reason", "injected demo outage")

    if not entity:
        return jsonify({"error": "entity is required"}), 400

    record = {
        "id": str(uuid.uuid4()),
        "entity": entity,
        "method": method,
        "status": "active",
        "reason": reason,
        "started_at": _now(),
        "resolved_at": None,
    }
    _OUTAGES[entity] = record
    return jsonify({"injected": record}), 201


@app.route("/admin/resolve-outage", methods=["POST"])
def resolve_outage():
    body = request.get_json(force=True) or {}
    entity = body.get("entity")

    if not entity or entity not in _OUTAGES:
        return jsonify({"error": f"no active outage found for entity={entity}"}), 404

    _OUTAGES[entity]["status"] = "resolved"
    _OUTAGES[entity]["resolved_at"] = _now()
    return jsonify({"resolved": _OUTAGES[entity]})


@app.route("/admin/state", methods=["GET"])
def get_state():
    return jsonify({"outages": _OUTAGES})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    _seed()
    app.run(host="0.0.0.0", port=5001, debug=True)
