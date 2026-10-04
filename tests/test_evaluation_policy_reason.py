"""P2 policy exhaustion is an honest redacted evaluation category, never page prose."""

import json

from jev_ultrafast.evaluation import record_trajectory


def _state(reason):
    return {"status": "abandoned", "stop_reason": reason, "verification": [False],
            "check_evidence": [{"state": "unmet", "reason": "observed"}],
            "additional_verified": None, "decisions": [{"operation": "BLOCKED", "label": "Private action"}],
            "history": [], "attempts": [], "prediction_calls": [], "planner_calls": [], "text_calls": [],
            "page": {"url": "https://private.test/secret", "text": "Secret account"}}


def test_policy_exhausted_survives_redacted_trajectory_as_code_owned_reason():
    report = record_trajectory(iter((_state("policy_exhausted"),)))
    assert report["outcome"] == "abandoned" and report["score"] == 0
    assert report["transitions"][0]["stop_reason"] == "policy_exhausted"
    encoded = json.dumps(report)
    assert "Private action" not in encoded and "private.test" not in encoded
    assert "Secret account" not in encoded


def test_unknown_page_supplied_reason_cannot_escape_allowlist():
    report = record_trajectory(iter((_state("secret account: Select flight now"),)))
    assert report["transitions"][0]["stop_reason"] == "other"
    assert "secret account" not in json.dumps(report)
