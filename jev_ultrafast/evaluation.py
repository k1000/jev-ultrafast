"""Offline, redacted evaluation of streamed ObjectiveAgent snapshots (not an RL trainer)."""

import re

_OPERATIONS = {"CLICK", "TYPE_TEXT", "SELECT", "SCROLL_TO", "SCROLL_UP", "SCROLL_DOWN",
               "PRESS_KEY", "SET_RANGE", "WAIT", "DONE", "BLOCKED"}
_ACTIONS = {"click", "fill", "select", "scroll_to", "scroll_up", "scroll_down", "press_key", "set_range", "wait"}
_REASONS = {"verified", "replanning_exhausted", "policy_exhausted", "unstable_observation", "decision_budget",
            "action_budget", "time_budget", "planning_failed", "observation_unavailable",
            "execution_error", "interrupted", "closed"}
_RECEIPTS = {"issued", "executed", "rejected_before_input", "rejected_by_policy", "outcome_unknown"}
_EVIDENCE_STATES = {"met", "unmet", "unknown"}


def _safe(value, allowed):
    return value if isinstance(value, str) and value in allowed else "other"


def _verified(state):
    checks, evidence = state.get("verification"), state.get("check_evidence")
    return (state.get("status") == "done" and state.get("stop_reason") == "verified"
            and isinstance(checks, list) and bool(checks) and all(v is True for v in checks)
            and isinstance(evidence, list) and len(evidence) == len(checks)
            and all(r.get("state") == "met" for r in evidence)
            and state.get("additional_verified") is not False)


def _evidence_states(value):
    if not isinstance(value, (list, tuple)) or any(type(v) is not str or v not in _EVIDENCE_STATES for v in value):
        return None
    return list(value)


def _decision_state(decision, attempt, state, tick):
    """Only consume the predecision digest/scalars; never inspect a raw page marker."""
    digest, context = decision.get("state_digest"), decision.get("state_context")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None or not isinstance(context, dict):
        return None  # Old snapshots without the P7 producer cannot supply predecision evidence.
    verification = context.get("verification")
    verification = (list(verification) if isinstance(verification, (list, tuple)) and
                    all(type(v) is bool for v in verification) else None)
    before = _evidence_states(context.get("check_evidence_states"))
    after_rows = state.get("check_evidence") if state.get("verification") is not None else None
    after = (_evidence_states([row.get("state") for row in after_rows])
             if isinstance(after_rows, list) and all(isinstance(row, dict) for row in after_rows) else None)
    count, index = context.get("completed_checks_count"), context.get("index")
    return {"tick": tick, "semantic_digest": digest,
            "modal_open": context["modal_open"] if type(context.get("modal_open")) is bool else None,
            "verification": verification,
            "completed_checks_count": count if type(count) is int and count >= 0 else None,
            "index": index if type(index) is int and index >= 0 else None,
            "last_mutation_present": (context["last_mutation_present"]
                                      if type(context.get("last_mutation_present")) is bool else None),
            "receipt_category": _safe(attempt.get("status"), _RECEIPTS) if attempt else None,
            "check_evidence_transition": {"before": before, "after": after}}


def record_trajectory(states):
    """Consume run() *as it yields*; retain no page, URL, label, target, or text values.

    Snapshots contain mutable cumulative histories. Do not collect them in a list before calling this.
    Actions count acknowledged executions from the attempt ledger, not uncertain history entries.
    A score is only a controller-reported outcome label, not independent ground truth.
    """
    transitions, decision_states = [], []
    prior_decisions = prior_actions = prior_attempts = 0
    final = None
    for tick, state in enumerate(states, 1):
        decisions, actions = state["decisions"], state["history"]
        if not 0 <= len(decisions) - prior_decisions <= 1 or not 0 <= len(actions) - prior_actions <= 1:
            raise ValueError("Supply streamed ObjectiveAgent.run() snapshots, not precollected mutable snapshots")
        decision = decisions[-1] if len(decisions) > prior_decisions else None
        action = actions[-1] if len(actions) > prior_actions and actions[-1].get("attempted") is not True else None
        attempts = state["attempts"]
        attempt = attempts[-1] if len(attempts) > prior_attempts else None
        if decision is not None:
            z_t = _decision_state(decision, attempt, state, tick)
            if z_t is not None:
                decision_states.append(z_t)
        verification = state["verification"]
        if verification is None or any(r.get("state") == "unknown" for r in state.get("check_evidence") or []):
            evidence = "unknown"
        elif _verified(state):
            evidence = "met"
        else:
            evidence = "unmet"
        transitions.append({"tick": tick,
                            "operation": _safe(decision.get("operation"), _OPERATIONS) if decision else None,
                            "action": _safe(action.get("kind"), _ACTIONS) if action else None,
                            "verification": evidence,
                            "status": _safe(state["status"], {"ready", "done", "abandoned", "needs_attention"}),
                            "stop_reason": _safe(state.get("stop_reason"), _REASONS)
                            if state.get("stop_reason") is not None else None})
        prior_decisions, prior_actions, prior_attempts = len(decisions), len(actions), len(attempts)
        final = state
    if final is None:
        raise ValueError("Supply snapshots from an objective run")
    outcome = ("verified" if _verified(final) else "abandoned" if final["status"] == "abandoned"
               else "incomplete" if final["status"] == "ready" else "inconclusive")
    attempts = final["attempts"]
    return {"outcome": outcome, "score": {"verified": 1, "abandoned": 0}.get(outcome),
            "transitions": transitions, "decision_states": decision_states,
            "decisions": len(final["decisions"]),
            "prediction_attempts": len(final["prediction_calls"]),
            "actions": sum(a.get("status") == "executed" for a in attempts),
            "mutation_attempts": len(attempts),
            "uncertain_attempts": sum(a.get("status") == "outcome_unknown" for a in attempts),
            "planner_calls": len(final["planner_calls"]),
            "runtime_replans": sum(c.get("phase") == "replan" for c in final["planner_calls"]),
            "text_calls": len(final["text_calls"])}


def evaluate_trajectories(reports):
    """Aggregate redacted runs; unknown/incomplete runs stay in the rate denominator."""
    totals = {"runs": 0, "verified": 0, "abandoned": 0, "inconclusive": 0, "incomplete": 0,
              "verified_rate": None, "decisions": 0, "actions": 0, "planner_calls": 0,
              "runtime_replans": 0, "text_calls": 0}
    for report in reports:
        totals["runs"] += 1
        totals[report["outcome"]] += 1
        for field in ("decisions", "actions", "planner_calls", "runtime_replans", "text_calls"):
            totals[field] += report[field]
    if totals["runs"]:
        totals["verified_rate"] = totals["verified"] / totals["runs"]
    return totals
