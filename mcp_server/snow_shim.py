"""
ISDO Lab C2 — Mock ServiceNow Table API (Flask Shim)  |  port 5001
Lets the MCP server / agents make real HTTP calls without touching production.

  GET   /api/now/table/incident                 list (filters: category, priority, state,
                                                 assignment_group, sysparm_limit)
  GET   /api/now/table/incident/<number>        get one
  PATCH /api/now/table/incident/<number>        update fields in memory (e.g. state)
  POST  /api/now/table/incident                 create (number auto-assigned if omitted)
  GET   /health                                 health check

Run from the project root:  python mcp_server/snow_shim.py
"""
import csv
import os

from flask import Flask, jsonify, request

app = Flask(__name__)
app.json.sort_keys = False  # keep CSV column order in JSON output

DATA_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "incidents.csv"))
PORT = int(os.getenv("SNOW_PORT", "5001"))
FILTERS = ["category", "priority", "state", "assignment_group"]


def load_incidents():
    try:
        with open(DATA_FILE, newline="", encoding="utf-8") as f:
            return {row["number"]: dict(row) for row in csv.DictReader(f)}
    except FileNotFoundError:
        print(f"Warning: {DATA_FILE} not found. Starting with empty dataset.")
        return {}


INCIDENTS = load_incidents()  # in-memory store — resets on restart


def not_found(number):
    return jsonify({"error": {"message": f"Incident {number} not found"}, "status": "failure"}), 404


@app.get("/api/now/table/incident")
def list_incidents():
    results = list(INCIDENTS.values())
    for key in FILTERS:
        val = request.args.get(key)
        if val:
            results = [r for r in results if str(r.get(key, "")).lower() == val.lower()]
    total = len(results)
    limit = request.args.get("sysparm_limit", type=int)
    if limit:
        results = results[:limit]
    return jsonify({"result": results, "total": total})


@app.get("/api/now/table/incident/<number>")
def get_incident(number):
    inc = INCIDENTS.get(number.upper())
    return jsonify({"result": inc}) if inc else not_found(number)


@app.patch("/api/now/table/incident/<number>")
def update_incident(number):
    number = number.upper()
    if number not in INCIDENTS:
        return not_found(number)
    updates = request.get_json(silent=True)
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": {"message": "JSON body with fields to update is required"}}), 400
    updates.pop("number", None)  # primary key cannot change
    INCIDENTS[number].update(updates)
    print(f"[ServiceNow Mock] Updated {number}: {updates}")
    return jsonify({"result": INCIDENTS[number], "message": "Updated successfully"})


@app.post("/api/now/table/incident")
def create_incident():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data.get("short_description"):
        return jsonify({"error": {"message": "Missing required field: short_description"}}), 400
    if not data.get("number"):
        nums = [int(n[3:]) for n in INCIDENTS if n[3:].isdigit()] or [1000]
        data["number"] = f"INC{max(nums) + 1:07d}"
    if data["number"] in INCIDENTS:
        return jsonify({"error": {"message": f"Incident {data['number']} already exists"}}), 409
    data.setdefault("state", "Open")
    INCIDENTS[data["number"]] = data
    print(f"[ServiceNow Mock] Created incident: {data['number']}")
    return jsonify({"result": data, "message": "Incident created"}), 201


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "ServiceNow Mock", "incidents_loaded": len(INCIDENTS)})


if __name__ == "__main__":
    print(f"ServiceNow Mock API starting on http://localhost:{PORT}")
    print(f"Loaded {len(INCIDENTS)} incidents from {DATA_FILE}")
    print("Endpoints: GET/POST /api/now/table/incident | GET/PATCH /api/now/table/incident/<number> | GET /health")
    # use_reloader=False: the reloader would restart the process and wipe in-memory PATCHes
    app.run(host="127.0.0.1", port=PORT, debug=True, use_reloader=False)
