"""
ISDO Lab C2 — smoke test for both mock APIs (covers lab Steps 3-6 + PATCH/PUT loop).
Start both shims first, then from the project root run:

    python labs/C2/test_c2_apis.py

Any PATCH/PUT made here is reverted at the end, so the shims stay in their original state.
"""
import json
import sys

import requests

SNOW = "http://localhost:5001"
JIRA = "http://localhost:5002"
passed, failed = 0, 0


def check(label, method, url, expect_status=200, body=None, test=None):
    global passed, failed
    print(f"\n--- Testing {method} {url.replace(SNOW, '').replace(JIRA, '')} ---")
    try:
        r = requests.request(method, url, json=body, timeout=5)
        data = r.json()
    except requests.ConnectionError:
        print(f"  FAIL  cannot connect — is the shim running? ({url})")
        failed += 1
        return None
    ok = r.status_code == expect_status and (test is None or test(data))
    preview = json.dumps(data)
    print(f"  {preview[:220]}{'...' if len(preview) > 220 else ''}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label} (HTTP {r.status_code})")
    passed, failed = passed + ok, failed + (not ok)
    return data


# Step 6 — health
check("ServiceNow health", "GET", f"{SNOW}/health",
      test=lambda d: d["status"] == "ok" and d["incidents_loaded"] >= 15)
check("Jira health", "GET", f"{JIRA}/health",
      test=lambda d: d["status"] == "ok" and d["requests_loaded"] >= 10)

# Steps 3-4 — ServiceNow
check("All incidents", "GET", f"{SNOW}/api/now/table/incident", test=lambda d: d["total"] >= 15)
check("P1 filter", "GET", f"{SNOW}/api/now/table/incident?priority=P1",
      test=lambda d: d["total"] > 0 and all(r["priority"] == "P1" for r in d["result"]))
check("Category filter", "GET", f"{SNOW}/api/now/table/incident?category=Network",
      test=lambda d: all(r["category"] == "Network" for r in d["result"]))
check("Single incident", "GET", f"{SNOW}/api/now/table/incident/INC0001001",
      test=lambda d: d["result"]["number"] == "INC0001001")
check("Unknown incident -> 404", "GET", f"{SNOW}/api/now/table/incident/INC9999999", 404)

# PATCH round-trip (used by SLA Agent in Lab C5)
orig = (check("Read before PATCH", "GET", f"{SNOW}/api/now/table/incident/INC0001002") or {}).get("result")
if orig:
    check("PATCH state -> Escalated", "PATCH", f"{SNOW}/api/now/table/incident/INC0001002",
          body={"state": "Escalated"}, test=lambda d: d["result"]["state"] == "Escalated")
    check("Read back after PATCH", "GET", f"{SNOW}/api/now/table/incident/INC0001002",
          test=lambda d: d["result"]["state"] == "Escalated")
    check("Revert PATCH", "PATCH", f"{SNOW}/api/now/table/incident/INC0001002",
          body={"state": orig["state"]})

# Step 5 — Jira
check("All requests", "GET", f"{JIRA}/rest/agile/1.0/board/requests", test=lambda d: d["total"] >= 10)
check("request_type filter", "GET", f"{JIRA}/rest/agile/1.0/board/requests?request_type=Access Grant",
      test=lambda d: d["total"] > 0 and all(r["request_type"] == "Access Grant" for r in d["issues"]))
check("Issue with nested fields", "GET", f"{JIRA}/rest/api/2/issue/REQ-1002",
      test=lambda d: d["fields"]["priority"]["name"] == "High")
check("Unknown issue -> 404", "GET", f"{JIRA}/rest/api/2/issue/REQ-9999", 404)

before = check("Read before PUT", "GET", f"{JIRA}/rest/api/2/issue/REQ-1005")
if before:
    check("PUT Jira-style status", "PUT", f"{JIRA}/rest/api/2/issue/REQ-1005",
          body={"fields": {"status": {"name": "In Progress"}}})
    check("Status filter still works after PUT", "GET", f"{JIRA}/rest/agile/1.0/board/requests?status=In Progress",
          test=lambda d: any(r["key"] == "REQ-1005" for r in d["issues"]))
    check("Revert PUT", "PUT", f"{JIRA}/rest/api/2/issue/REQ-1005",
          body={"status": before["fields"]["status"]["name"]})

print(f"\n==== {passed} passed, {failed} failed ====")
sys.exit(1 if failed else 0)
