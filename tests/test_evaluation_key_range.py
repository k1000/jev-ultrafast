"""P5 actions retain only redacted P7 trajectory categories (offline)."""

import hashlib
import json
from copy import deepcopy

from jev_ultrafast.evaluation import record_trajectory

SECRET = "sensitive-target-0.3"
MARKERS = [["https://private.test/" + SECRET, "modal"], ["https://private.test/" + SECRET, "range"]]


def decision(operation, marker, *, before, index):
    digest = hashlib.sha256(json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"operation": operation, "choice": SECRET, "target": SECRET,
            "state_digest": digest, "state_context": {
                "modal_open": index == 0, "verification": [False], "check_evidence_states": [before],
                "completed_checks_count": 0, "index": index, "last_mutation_present": index > 0}}


def state(*, decisions, history, attempts, proof, status="ready", reason=None):
    return {"decisions": decisions, "history": history, "attempts": attempts,
            "verification": None if proof is None else [proof == "met"],
            "check_evidence": None if proof is None else [{"state": proof}],
            "status": status, "stop_reason": reason, "additional_verified": None,
            "prediction_calls": [], "planner_calls": [], "text_calls": [],
            "page": {"url": "https://private.test/" + SECRET, "text": SECRET,
                     "semantic_marker": ["post", SECRET]}, "goal": SECRET}


def test_streamed_key_then_uncertain_range_align_prestate_receipt_and_following_evidence():
    first_decision = decision("PRESS_KEY", MARKERS[0], before="unknown", index=0)
    second_decision = decision("SET_RANGE", MARKERS[1], before="unmet", index=1)
    key_action = {"kind": "press_key", "action": SECRET, "key": "Escape"}
    key_attempt = {"status": "executed", "action": SECRET}
    range_attempt = {"status": "outcome_unknown", "action": SECRET, "text": "0.3"}
    first = state(decisions=[first_decision], history=[key_action], attempts=[key_attempt], proof="unmet")
    second = state(decisions=[first_decision, second_decision], history=[key_action],
                   attempts=[key_attempt, range_attempt], proof=None, status="needs_attention",
                   reason="execution_error")
    report = record_trajectory(iter([first, second]))
    assert [(row["operation"], row["action"]) for row in report["transitions"]] == [
        ("PRESS_KEY", "press_key"), ("SET_RANGE", None)]
    assert [row["receipt_category"] for row in report["decision_states"]] == ["executed", "outcome_unknown"]
    assert [row["semantic_digest"] for row in report["decision_states"]] == [
        first_decision["state_digest"], second_decision["state_digest"]]
    assert [row["check_evidence_transition"] for row in report["decision_states"]] == [
        {"before": ["unknown"], "after": ["unmet"]}, {"before": ["unmet"], "after": None}]
    assert report["actions"] == 1 and report["mutation_attempts"] == 2
    assert report["uncertain_attempts"] == 1 and report["outcome"] == "inconclusive"
    assert SECRET not in json.dumps(report) and "private.test" not in json.dumps(report)
    assert "0.3" not in json.dumps(report) and "Escape" not in json.dumps(report)


def test_executed_range_retains_category_but_never_exports_prepared_value():
    row = decision("SET_RANGE", MARKERS[1], before="unmet", index=0)
    snapshot = state(decisions=[row], history=[{"kind": "set_range", "text": SECRET}],
                     attempts=[{"status": "executed", "text": SECRET}], proof="met",
                     status="done", reason="verified")
    report = record_trajectory([snapshot])
    assert report["transitions"][0]["action"] == "set_range"
    assert report["transitions"][0]["operation"] == "SET_RANGE"
    assert report["outcome"] == "verified" and report["actions"] == 1
    assert SECRET not in json.dumps(report)


def test_policy_exhausted_and_unknown_string_fallback_are_unchanged():
    row = decision("PRESS_KEYS", MARKERS[0], before="unmet", index=0)
    policy = state(decisions=[row], history=[{"kind": "not_a_key", "target": SECRET}],
                   attempts=[{"status": "rejected_by_policy"}], proof="unmet",
                   status="abandoned", reason="policy_exhausted")
    report = record_trajectory([policy])
    assert report["transitions"][0]["operation"] == "other"
    assert report["transitions"][0]["action"] == "other"
    assert report["transitions"][0]["stop_reason"] == "policy_exhausted"
    unexpected = deepcopy(policy)
    unexpected["stop_reason"] = "unknown_stop_reason"
    assert record_trajectory([unexpected])["transitions"][0]["stop_reason"] == "other"
