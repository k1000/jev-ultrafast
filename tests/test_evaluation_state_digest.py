"""P7 redacted, pre-decision state and following-observation telemetry (offline)."""

import hashlib
import json
from copy import deepcopy

from jev_ultrafast.evaluation import record_trajectory


def _digest(marker):
    return hashlib.sha256(json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _state(*, marker, before, after, receipt=None, index=0, modal=False, tick=1):
    decision = {"operation": "CLICK", "state_digest": _digest(marker), "state_context": {
        "modal_open": modal, "verification": [value == "met" for value in before],
        "check_evidence_states": before, "completed_checks_count": tick - 1,
        "index": index, "last_mutation_present": tick > 1,
    }}
    attempts = [] if receipt is None else [{"status": receipt, "action": "Secret target"}]
    return {"decisions": [decision], "history": [], "attempts": attempts,
            "verification": None if after is None else [value == "met" for value in after],
            "check_evidence": None if after is None else [{"state": value} for value in after],
            "status": "ready" if after is not None else "needs_attention", "stop_reason": None,
            "additional_verified": None, "prediction_calls": [], "planner_calls": [], "text_calls": [],
            "page": {"url": "https://private.test/SECRET?token=SECRET", "text": "SECRET",
                     "semantic_marker": ["SECRET", marker]},
            "goal": "SECRET", "plan_index": 99}


def test_predecision_digest_redacted_and_follows_same_decision_receipt_and_observation():
    marker = [1, "https://private.test/SECRET", False, [{"value": "SECRET"}], ["click"]]
    first = _state(marker=marker, before=["unmet", "unknown"], after=["met", "unknown"],
                   receipt="executed")
    report = record_trajectory([first])
    assert report["decision_states"] == [{
        "tick": 1, "semantic_digest": _digest(marker), "modal_open": False,
        "verification": [False, False], "completed_checks_count": 0, "index": 0,
        "last_mutation_present": False, "receipt_category": "executed",
        "check_evidence_transition": {"before": ["unmet", "unknown"], "after": ["met", "unknown"]},
    }]
    encoded = json.dumps(report)
    assert "SECRET" not in encoded and "private.test" not in encoded
    assert "Secret target" not in encoded
    assert first["page"]["semantic_marker"] != report["decision_states"][0]["semantic_digest"]


def test_predecision_digest_is_stable_across_text_scroll_churn_and_changes_with_semantics():
    marker = [1, "https://private.test/SECRET", False, [{"checked": False}], ["click"]]
    first = _state(marker=marker, before=["unmet"], after=["unmet"])
    second = deepcopy(first)
    second["page"].update(text="a noisy counter", scroll={"y": 500})
    second["page"]["semantic_marker"] = ["post-tick different marker"]
    assert record_trajectory([first])["decision_states"][0]["semantic_digest"] == (
        record_trajectory([second])["decision_states"][0]["semantic_digest"])
    changed = _state(marker=[1, "https://private.test/SECRET", True,
                             [{"checked": False}], ["click"]],
                     before=["unmet"], after=["unmet"], modal=True)
    assert record_trajectory([first])["decision_states"][0]["semantic_digest"] != (
        record_trajectory([changed])["decision_states"][0]["semantic_digest"])


def test_missing_following_observation_is_unknown_not_an_unmet_transition():
    marker = [1, "https://private.test/SECRET", False, [], []]
    state = _state(marker=marker, before=["unknown"], after=None, receipt="outcome_unknown")
    state["check_evidence"] = [{"state": "met"}]  # Stale evidence cannot stand in for a failed read.
    entry = record_trajectory([state])["decision_states"][0]
    assert entry["receipt_category"] == "outcome_unknown"
    assert entry["check_evidence_transition"] == {"before": ["unknown"], "after": None}


def test_old_snapshot_never_guesses_predecision_state_from_post_tick_page():
    state = _state(marker=[1, "SECRET"], before=["unmet"], after=["met"])
    del state["decisions"][0]["state_digest"]
    state["decisions"][0]["state_context"]["index"] = 7
    assert record_trajectory([state])["decision_states"] == []


def test_invalid_producer_metadata_cannot_leak_private_values():
    state = _state(marker=[1, "SECRET"], before=["unmet"], after=["met"])
    state["decisions"][0]["state_digest"] = "SECRET"
    assert record_trajectory([state])["decision_states"] == []


def test_only_new_decisions_emit_state_and_attempts_cannot_be_misattributed():
    marker = [1, "https://private.test/SECRET", False, [], []]
    first = _state(marker=marker, before=["unmet"], after=["unmet"], receipt="executed")
    later = deepcopy(first)
    later["attempts"].append({"status": "rejected_before_input"})
    later["check_evidence"] = [{"state": "met"}]
    later["verification"] = [True]
    report = record_trajectory([first, later])
    assert len(report["decision_states"]) == 1
    assert report["decision_states"][0]["receipt_category"] == "executed"


def test_two_decisions_each_pair_with_their_own_following_evidence_and_receipt():
    first = _state(marker=[1], before=["unknown"], after=["unmet"], receipt="executed")
    second = _state(marker=[2], before=["unmet"], after=["met"],
                    receipt="rejected_before_input", index=1, modal=True, tick=2)
    second["decisions"] = first["decisions"] + second["decisions"]
    second["attempts"] = first["attempts"] + second["attempts"]
    entries = record_trajectory([first, second])["decision_states"]
    assert [entry["tick"] for entry in entries] == [1, 2]
    assert [entry["receipt_category"] for entry in entries] == ["executed", "rejected_before_input"]
    assert [entry["check_evidence_transition"] for entry in entries] == [
        {"before": ["unknown"], "after": ["unmet"]},
        {"before": ["unmet"], "after": ["met"]},
    ]
    assert entries[1]["index"] == 1 and entries[1]["modal_open"] is True
