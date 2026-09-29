"""
ISDO Lab C5 - SLA & Escalation Agent
------------------------------------
Monitors SLA deadlines, predicts breach risk and escalates tickets that are about
to breach (or already have). Every P1 escalation pauses at a Human-in-the-Loop
(HITL) gate - no P1 ticket is escalated without a human typing 'y'.

Tools exposed to Claude:
  1. get_sla_status  - minutes remaining vs. SLA target -> BREACHED / CRITICAL / AT_RISK / ON_TRACK
  2. update_ticket   - ServiceNow update (escalate / add_note / update_state).
                       PATCHes the C2 ServiceNow shim on :5001 if it is running,
                       otherwise falls back to a local mock.

Guardrails enforced in code (not left to the model):
  - Escalation only allowed for P1/P2 tickets whose last SLA check was CRITICAL or BREACHED
  - Every P1 escalation requires human approval (decision is written to logs/hitl_audit.jsonl)
  - The agent may only act on the ticket it was asked to monitor

Run from the project root:   python agents/sla_agent.py
"""

import json
import os
import sys
from datetime import datetime

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to cp1252 - make printing safe
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# ── CONFIG ───────────────────────────────────────────────────────────────────

MODEL = os.getenv("ISDO_MODEL", "claude-opus-5")
MAX_TOKENS = 1024
MAX_AGENT_TURNS = 6                      # safety stop for the tool loop

SNOW_BASE_URL = os.getenv("SNOW_URL", f"http://localhost:{os.getenv('SNOW_PORT', '5001')}")
SNOW_TIMEOUT = 3                         # seconds

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
AUDIT_LOG = os.path.join(PROJECT_ROOT, "logs", "hitl_audit.jsonl")

# Simulated "now" so every run gives the same, reproducible result
SIMULATED_NOW = datetime(2024, 1, 15, 10, 30)

# SLA targets by priority (minutes to resolve)
SLA_MINUTES = {"P1": 60, "P2": 240, "P3": 480, "P4": 1440}

ESCALATE_RISKS = {"BREACHED", "CRITICAL"}
ESCALATE_PRIORITIES = {"P1", "P2"}
HITL_PRIORITIES = {"P1"}

# Escalation team by ticket category (used as fallback if the model omits one)
ESCALATION_TEAMS = {
    "network": "L2-Network-Ops",
    "application": "L2-App-Support",
    "server": "L2-Server-Ops",
    "access": "L2-Security-Ops",
    "security": "L2-Security-Ops",
}
DEFAULT_ESCALATION_TEAM = "L2-Service-Desk"

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ── TOOL DEFINITIONS ─────────────────────────────────────────────────────────

tools = [
    {
        "name": "get_sla_status",
        "description": (
            "Check the SLA status of a ticket. Returns minutes remaining, the breach risk level "
            "(BREACHED, CRITICAL, AT_RISK, ON_TRACK) and whether escalation is required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_number": {"type": "string", "description": "e.g. INC0001002"},
                "sla_due": {
                    "type": "string",
                    "description": "SLA due datetime in format YYYY-MM-DD HH:MM:SS",
                },
                "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
            },
            "required": ["ticket_number", "sla_due", "priority"],
        },
    },
    {
        "name": "update_ticket",
        "description": (
            "Update a ticket in ServiceNow: escalate it to a team, add a work note, or change its state. "
            "Only escalate when get_sla_status says requires_escalation is true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_number": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["escalate", "add_note", "update_state"],
                    "description": "Action to perform on the ticket",
                },
                "escalation_team": {
                    "type": "string",
                    "description": "Team to escalate to (e.g. L2-Network-Ops, L2-App-Support)",
                },
                "note": {"type": "string", "description": "Work note to add to the ticket"},
                "new_state": {
                    "type": "string",
                    "description": "New state, e.g. In Progress, Escalated, Resolved",
                },
            },
            "required": ["ticket_number", "action"],
        },
    },
]

# ── TOOL IMPLEMENTATIONS ─────────────────────────────────────────────────────


def get_sla_status(ticket_number, sla_due, priority):
    """Calculate minutes remaining vs. the SLA target and classify breach risk."""
    try:
        due_dt = datetime.strptime(sla_due, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return {"error": f"Invalid sla_due format: {sla_due!r} (expected YYYY-MM-DD HH:MM:SS)"}

    if priority not in SLA_MINUTES:
        return {"error": f"Unknown priority: {priority!r}"}

    target_minutes = SLA_MINUTES[priority]
    minutes_remaining = int((due_dt - SIMULATED_NOW).total_seconds() // 60)
    pct_remaining = round(minutes_remaining / target_minutes * 100, 1)

    if minutes_remaining < 0:
        risk = "BREACHED"
        msg = f"SLA BREACHED by {abs(minutes_remaining)} minutes"
    elif minutes_remaining < target_minutes * 0.2:
        risk = "CRITICAL"
        msg = f"Only {minutes_remaining} minutes remaining -- breach imminent"
    elif minutes_remaining < target_minutes * 0.5:
        risk = "AT_RISK"
        msg = f"{minutes_remaining} minutes remaining -- at risk"
    else:
        risk = "ON_TRACK"
        msg = f"{minutes_remaining} minutes remaining -- on track"

    return {
        "ticket_number": ticket_number,
        "priority": priority,
        "sla_due": sla_due,
        "sla_target_minutes": target_minutes,
        "minutes_remaining": minutes_remaining,
        "percent_remaining": pct_remaining,
        "breach_risk": risk,
        "status_message": msg,
        "requires_escalation": risk in ESCALATE_RISKS and priority in ESCALATE_PRIORITIES,
    }


def _patch_servicenow(ticket_number, fields):
    """PATCH the C2 ServiceNow shim. Returns (ok, source) - falls back to mock if shim is down."""
    url = f"{SNOW_BASE_URL}/api/now/table/incident/{ticket_number}"
    try:
        resp = requests.patch(url, json=fields, timeout=SNOW_TIMEOUT)
        if resp.status_code == 200:
            return True, "servicenow-shim"
        return False, f"servicenow-shim HTTP {resp.status_code}: {resp.text[:120]}"
    except requests.RequestException:
        return True, "local-mock (shim not reachable)"


def update_ticket(ticket_number, action, escalation_team=None, note=None, new_state=None):
    """Escalate / add note / change state on a ServiceNow ticket."""
    timestamp = datetime.now().isoformat(timespec="seconds")

    if action == "escalate":
        team = escalation_team or DEFAULT_ESCALATION_TEAM
        fields = {"state": "Escalated", "assignment_group": team,
                  "work_notes": note or f"Escalated to {team} by ISDO SLA Agent"}
        label = f"ESCALATED {ticket_number} -> {team}"
        message = f"Ticket {ticket_number} escalated to {team}"
    elif action == "add_note":
        if not note:
            return {"success": False, "error": "add_note requires a 'note'"}
        fields = {"work_notes": note}
        label = f"NOTE ADDED to {ticket_number}"
        message = f"Work note added to {ticket_number}: {note[:50]}"
    elif action == "update_state":
        if not new_state:
            return {"success": False, "error": "update_state requires 'new_state'"}
        fields = {"state": new_state}
        label = f"STATE CHANGED {ticket_number} -> {new_state}"
        message = f"Ticket {ticket_number} state changed to {new_state}"
    else:
        return {"success": False, "error": f"Unknown action: {action!r}"}

    ok, source = _patch_servicenow(ticket_number, fields)
    if ok:
        print(f"  [ServiceNow Mock] {label}")
    else:
        print(f"  [ServiceNow Mock] FAILED {label} ({source})")

    return {"ticket_number": ticket_number, "action": action, "success": ok,
            "message": message if ok else source, "fields": fields,
            "source": source, "timestamp": timestamp}


# ── HITL GATE & AUDIT ────────────────────────────────────────────────────────


def audit(event):
    """Append a HITL decision to logs/hitl_audit.jsonl."""
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    event["logged_at"] = datetime.now().isoformat(timespec="seconds")
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def hitl_approve(ticket_number, action, detail):
    """Pause and ask a human to approve before a P1 escalation."""
    banner = "!!! " * 10
    print(f"\n  {banner}")
    print("  HITL APPROVAL REQUIRED")
    print(f"  Ticket:  {ticket_number}")
    print(f"  Action:  {action}")
    print(f"  Detail:  {detail}")
    print(f"  {banner}")
    try:
        answer = input("  Approve escalation? [y/n]: ").strip().lower()
    except EOFError:                       # non-interactive run -> never auto-approve
        answer = "n"
    approved = answer in ("y", "yes")
    print(f"  Decision: {'APPROVED' if approved else 'REJECTED'}")
    return approved


# ── GUARDED TOOL DISPATCH ────────────────────────────────────────────────────


def escalation_team_for(category):
    return ESCALATION_TEAMS.get((category or "").strip().lower(), DEFAULT_ESCALATION_TEAM)


def run_tool(name, inp, ticket, state):
    """Execute one tool call with the code-level guardrails applied."""
    if name == "get_sla_status":
        result = get_sla_status(inp.get("ticket_number"), inp.get("sla_due"), inp.get("priority"))
        if "error" not in result:
            state["sla"] = result
            print(f"  -> Risk Level: {result['breach_risk']}")
            print(f"  -> Status:     {result['status_message']}")
        return result

    if name != "update_ticket":
        return {"success": False, "error": f"Unknown tool: {name}"}

    # Guardrail: the agent may only touch the ticket it is monitoring
    if inp.get("ticket_number") != ticket["ticket_number"]:
        return {"success": False, "error": f"Not allowed: can only update {ticket['ticket_number']}"}

    if inp.get("action") == "escalate":
        sla = state.get("sla")
        # Guardrail: escalation policy is enforced in code, not trusted to the model
        if not sla:
            return {"success": False, "error": "Call get_sla_status before escalating"}
        if not sla["requires_escalation"]:
            print(f"  [Guardrail] Escalation blocked: {ticket['priority']} / {sla['breach_risk']} "
                  "does not meet escalation policy")
            return {"success": False,
                    "error": "Escalation blocked by policy: only P1/P2 tickets that are CRITICAL or BREACHED"}

        inp = dict(inp)
        inp.setdefault("escalation_team", escalation_team_for(ticket["category"]))

        # HITL gate: every P1 escalation needs a human decision.
        # Priority comes from the ticket record, not from the model's tool input.
        if ticket["priority"] in HITL_PRIORITIES:
            approved = hitl_approve(ticket["ticket_number"], "Escalate ticket",
                                    f"Escalate to {inp['escalation_team']}")
            state["hitl_decision"] = "APPROVED" if approved else "REJECTED"
            audit({"ticket_number": ticket["ticket_number"], "priority": ticket["priority"],
                   "breach_risk": sla["breach_risk"], "escalation_team": inp["escalation_team"],
                   "decision": state["hitl_decision"]})
            if not approved:
                print("  Escalation cancelled and logged.")
                return {"success": False,
                        "message": "Escalation rejected by human approver. Do not retry the escalation."}

        result = update_ticket(inp["ticket_number"], "escalate", inp["escalation_team"], inp.get("note"))
        if result.get("success"):
            state["escalated_to"] = inp["escalation_team"]
        return result

    return update_ticket(inp["ticket_number"], inp.get("action"), inp.get("escalation_team"),
                         inp.get("note"), inp.get("new_state"))


# ── SLA AGENT ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the ISDO SLA & Escalation Agent for Zensar's IT Service Desk.

For each ticket:
1. Always call get_sla_status first to check breach risk.
2. If requires_escalation is true (priority P1 or P2 AND risk CRITICAL or BREACHED),
   call update_ticket with action "escalate" and the correct escalation_team, plus a short note
   explaining the SLA situation.
3. P3/P4 tickets and ON_TRACK/AT_RISK tickets are monitored only - do not escalate them.
4. If an escalation is rejected by the human approver, do not retry it. Add a work note
   recording that escalation was declined, then stop.

Escalation teams by category:
- Network -> L2-Network-Ops
- Application -> L2-App-Support
- Server -> L2-Server-Ops
- Access / Security -> L2-Security-Ops
- Anything else -> L2-Service-Desk

Finish with a 1-2 sentence summary: risk level, and what action was taken."""

def call_claude(messages):
    """One Claude turn with the SLA tools attached."""
    return client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
                                  tools=tools, messages=messages)


def monitor_ticket(ticket_number, short_description, category, priority, sla_due):
    """Run SLA monitoring for one ticket. Returns a summary dict (used as a graph node in C6)."""
    print(f"\n{'=' * 55}")
    print(f"SLA Check: {ticket_number} | {priority} | Category: {category}")
    print(f"{'=' * 55}")

    ticket = {"ticket_number": ticket_number, "short_description": short_description,
              "category": category, "priority": priority, "sla_due": sla_due}
    state = {"sla": None, "hitl_decision": None, "escalated_to": None}

    messages = [{
        "role": "user",
        "content": ("Monitor SLA for this ticket and escalate if needed:\n\n"
                    f"Ticket: {ticket_number}\nDescription: {short_description}\n"
                    f"Category: {category}\nPriority: {priority}\nSLA Due: {sla_due}"),
    }]

    summary = ""
    for _ in range(MAX_AGENT_TURNS):
        response = call_claude(messages)

        if response.stop_reason != "tool_use":
            summary = " ".join(b.text for b in response.content if b.type == "text").strip()
            if summary:
                print(f"\n  Agent: {summary}")
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = run_tool(block.name, block.input, ticket, state)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
                "is_error": bool(result.get("error")),
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        print("  [Stopped] Reached max agent turns.")

    sla = state["sla"] or {}
    return {
        "ticket_number": ticket_number,
        "priority": priority,
        "breach_risk": sla.get("breach_risk"),
        "minutes_remaining": sla.get("minutes_remaining"),
        "escalated": state["escalated_to"] is not None,
        "escalated_to": state["escalated_to"],
        "hitl_decision": state["hitl_decision"],
        "summary": summary,
    }


# ── RUN SLA MONITORING ───────────────────────────────────────────────────────

# Simulated "now" = 2024-01-15 10:30. One ticket per SLA state.
# NOTE: the CSV due times (INC0001002 = 11:00, INC0001001 = 14:00) give 50% and 87.5% of the
# SLA left, which the documented rules classify as ON_TRACK. The demo times below are adjusted
# so the four tickets show CRITICAL, BREACHED, AT_RISK and ON_TRACK.
test_tickets = [
    # P1 ERP, due 10:40 -> 10 of 60 min left (17%) -> CRITICAL -> HITL prompt (type 'y')
    {"ticket_number": "INC0001002", "short_description": "Cannot access ERP system - login error",
     "category": "Application", "priority": "P1", "sla_due": "2024-01-15 10:40:00"},
    # P1 Exchange, due 09:30 -> BREACHED by 60 min -> HITL prompt (type 'n')
    {"ticket_number": "INC0001010", "short_description": "Exchange server high CPU alert",
     "category": "Server", "priority": "P1", "sla_due": "2024-01-15 09:30:00"},
    # P2 VPN, due 12:00 -> 90 of 240 min left (37.5%) -> AT_RISK -> monitored, no escalation
    # Step 5: change sla_due to "2024-01-15 10:00:00" -> BREACHED -> auto-escalated to
    #         L2-Network-Ops (P2, so no HITL gate)
    {"ticket_number": "INC0001001", "short_description": "VPN not connecting after password change",
     "category": "Network", "priority": "P2", "sla_due": "2024-01-15 12:00:00"},
    # P3 laptop, due 17 Jan -> ON_TRACK -> monitored only
    {"ticket_number": "INC0001003", "short_description": "Laptop running very slowly",
     "category": "Hardware", "priority": "P3", "sla_due": "2024-01-17 09:00:00"},
]

if __name__ == "__main__":
    print(f"ISDO SLA Agent | model={MODEL} | simulated now={SIMULATED_NOW:%Y-%m-%d %H:%M}")
    results = [monitor_ticket(**t) for t in test_tickets]

    print(f"\n{'=' * 55}\nSLA MONITORING SUMMARY\n{'=' * 55}")
    print(f"{'Ticket':<12}{'Pri':<5}{'Risk':<10}{'Mins':>6}  {'Escalated to':<16}{'HITL'}")
    for r in results:
        print(f"{r['ticket_number']:<12}{r['priority']:<5}{str(r['breach_risk']):<10}"
              f"{str(r['minutes_remaining']):>6}  {str(r['escalated_to'] or '-'):<16}"
              f"{r['hitl_decision'] or '-'}")
    print(f"\nHITL decisions logged to: {AUDIT_LOG}")
