"""
ISDO Lab C2 — Mock Jira Service Management REST API (Flask Shim)  |  port 5002

  GET  /rest/agile/1.0/board/requests   list (filters: request_type, priority, assignee, status)
  GET  /rest/api/2/issue?status=Open    same list, filtered (alias)
  GET  /rest/api/2/issue/<key>          one request, Jira-style nested "fields"
  PUT  /rest/api/2/issue/<key>          update (flat or Jira-style {"fields": {...}})
  POST /rest/api/2/issue                create
  GET  /health                          health check

Run from the project root:  python mcp_server/jira_shim.py
"""
import csv
import os

from flask import Flask, jsonify, request

app = Flask(__name__)
app.json.sort_keys = False

DATA_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "requests.csv"))
PORT = int(os.getenv("JIRA_PORT", "5002"))
FILTERS = ["request_type", "priority", "assignee", "status"]
# Jira field name -> flat CSV column
FIELD_MAP = {"summary": "summary", "priority": "priority", "status": "status",
             "assignee": "assignee", "customfield_sla": "sla", "issuetype": "request_type",
             "request_type": "request_type", "sla": "sla"}


def load_requests():
    try:
        with open(DATA_FILE, newline="", encoding="utf-8") as f:
            return {row["key"]: dict(row) for row in csv.DictReader(f)}
    except FileNotFoundError:
        print(f"Warning: {DATA_FILE} not found. Starting with empty dataset.")
        return {}


REQUESTS = load_requests()  # in-memory store — resets on restart


def flatten(fields):
    """{"status": {"name": "Done"}} -> {"status": "Done"}; unknown keys kept as-is."""
    flat = {}
    for k, v in fields.items():
        if isinstance(v, dict):
            v = v.get("name") or v.get("displayName") or v.get("value") or ""
        flat[FIELD_MAP.get(k, k)] = v
    return flat


def to_jira(req):
    return {"key": req["key"], "fields": {
        "summary": req.get("summary"),
        "issuetype": {"name": req.get("request_type")},
        "priority": {"name": req.get("priority")},
        "status": {"name": req.get("status")},
        "assignee": {"displayName": req.get("assignee")},
        "customfield_sla": req.get("sla"),
    }}


def not_found(key):
    return jsonify({"errorMessages": [f"Issue {key} does not exist"], "errors": {}}), 404


@app.get("/rest/agile/1.0/board/requests")
@app.get("/rest/api/2/issue")
def list_requests():
    results = list(REQUESTS.values())
    for key in FILTERS:
        val = request.args.get(key)
        if val:
            results = [r for r in results if str(r.get(key, "")).lower() == val.lower()]
    return jsonify({"issues": results, "total": len(results)})


@app.get("/rest/api/2/issue/<key>")
def get_request(key):
    req = REQUESTS.get(key.upper())
    return jsonify(to_jira(req)) if req else not_found(key)


@app.put("/rest/api/2/issue/<key>")
def update_request(key):
    key = key.upper()
    if key not in REQUESTS:
        return not_found(key)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"errorMessages": ["No update body provided"]}), 400
    updates = flatten(data.get("fields", data))
    updates.pop("key", None)
    REQUESTS[key].update(updates)
    print(f"[Jira Mock] Updated {key}: {updates}")
    return jsonify({"key": key, "message": "Updated successfully", "issue": REQUESTS[key]})


@app.post("/rest/api/2/issue")
def create_request():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"errorMessages": ["JSON body required"]}), 400
    fields = flatten(data.get("fields", data))
    if not fields.get("summary"):
        return jsonify({"errorMessages": ["Field 'summary' is required"]}), 400
    nums = [int(k.split("-")[1]) for k in REQUESTS if k.split("-")[-1].isdigit()] or [1000]
    key = f"REQ-{max(nums) + 1}"
    REQUESTS[key] = {"key": key, "summary": fields["summary"],
                     "request_type": fields.get("request_type", ""),
                     "priority": fields.get("priority") or "Medium",
                     "assignee": fields.get("assignee", ""), "sla": fields.get("sla", ""),
                     "status": "Open"}
    print(f"[Jira Mock] Created request: {key}")
    return jsonify({"key": key, "message": "Request created"}), 201


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Jira Mock", "requests_loaded": len(REQUESTS)})


if __name__ == "__main__":
    print(f"Jira Mock API starting on http://localhost:{PORT}")
    print(f"Loaded {len(REQUESTS)} requests from {DATA_FILE}")
    print("Endpoints: GET /rest/agile/1.0/board/requests | GET/PUT /rest/api/2/issue/<key> | GET /health")
    app.run(host="127.0.0.1", port=PORT, debug=True, use_reloader=False)
