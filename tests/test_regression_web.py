"""Live-suite bookkeeping is exercised with fake boundaries; never calls paid APIs."""

from unittest.mock import Mock

import pytest

from scripts import regression_web as suite


def result_for(case):
    return {"status": case.expected, "stop_reason": "verified" if case.expected == "done" else "replanning_exhausted",
            "verified": case.expected == "done", "controller_verified": case.expected == "done",
            "safety_verified": True, "limitation_verified": case.expected != "done", "actions": 0,
            "text_requests": 0}


def test_negative_goal_is_distinct_from_verified_limitation():
    case = suite.CASES[-1]
    result = result_for(case)
    assert suite.passed(case, result)
    assert result["verified"] is False and result["limitation_verified"] is True
    assert case.check != case.limitation


@pytest.mark.parametrize("reason", ["time_budget", "action_budget", "decision_budget"])
def test_budget_exhaustion_cannot_pass_a_negative_case(reason):
    case = suite.CASES[-1]
    assert not suite.passed(case, {**result_for(case), "stop_reason": reason})


@pytest.mark.parametrize("override", [
    {"actions": 1}, {"safety_verified": False}, {"limitation_verified": False},
    {"verified": True}, {"controller_verified": None}, {"text_requests": 1},
])
def test_negative_cases_require_unchanged_page_and_unmet_goal(override):
    case = suite.CASES[-1]
    assert not suite.passed(case, {**result_for(case), **override})


def test_positive_model_status_never_replaces_independent_outcome():
    case = suite.CASES[3]
    assert not suite.passed(case, {**result_for(case), "verified": False})
    assert not suite.passed(case, {**result_for(case), "controller_verified": False})


def fake_agent(status="done", stop_reason="verified", verification=None):
    agent = Mock()
    agent.status, agent.stop_reason = status, stop_reason
    state = {"status": status, "stop_reason": stop_reason, "page": {"url": "https://example.test/"},
             "verification": verification or [True, True],
             "stale_retries": 0, "history": [{"operation": "TYPE_TEXT"}],
             "decisions": [{"operation": "TYPE_TEXT", "latency_ms": 3}], "text_calls": [],
             "planner_calls": [{"latency_ms": 10}], "replans_used": 0, "corrections_used": 0}
    agent.run.return_value = iter([state])
    agent.snapshot.return_value = state
    agent.browser.target = "test-tab"
    return agent


def test_runner_supplies_caller_checks_and_counts_planning_without_helper_credentials(monkeypatch):
    case = suite.CASES[3]
    agent = fake_agent()
    agent.browser.evaluate.side_effect = [True, '{"url":"form","fields":[]}', True, '{"url":"form","fields":[]}']
    constructor = Mock(return_value=agent)
    monkeypatch.setattr(suite, "ObjectiveAgent", constructor)
    result = suite.run_case(case)
    assert result["passed"] and result["verified"]
    assert constructor.call_args.kwargs["checks"] == case.checks
    assert constructor.call_args.kwargs["max_replans"] == 1
    assert result["planner_requests"] == 1 and result["decisions"] == 1 and result["text_requests"] == 0
    assert "fields" not in result  # Public evidence must not persist form baselines.
    agent.close.assert_called_once()


def test_missing_fixture_does_not_start_paid_execution(monkeypatch):
    agent = fake_agent()
    agent.browser.evaluate.return_value = False
    monkeypatch.setattr(suite, "ObjectiveAgent", Mock(return_value=agent))
    result = suite.run_case(suite.CASES[1])
    assert result["preflight_unavailable"] and not result["passed"]
    agent.run.assert_not_called()
    agent.close.assert_called_once()


def test_uncertain_input_is_retained_not_repeated_or_closed(monkeypatch):
    agent = fake_agent("needs_attention", "execution_error")
    agent.snapshot.return_value["verification"] = None
    agent.browser.evaluate.side_effect = [True, "baseline", False, "baseline"]
    monkeypatch.setattr(suite, "ObjectiveAgent", Mock(return_value=agent))
    result = suite.run_case(suite.CASES[3])
    assert result["uncertain_execution"] and result["retained_tab"] == "test-tab"
    assert not result["passed"]
    agent.close.assert_not_called()


def test_uncertain_receipt_records_only_safe_diagnostics_and_retains_tab(monkeypatch):
    agent = fake_agent("needs_attention", "execution_error")
    agent.browser.receipts = [{"status": "outcome_unknown", "phase": "input_click",
                               "input_started": True, "error": "TimeoutError", "action_id": "e7",
                               "text": "do not persist input text", "calls": ["private"]}]
    agent.browser.evaluate.side_effect = [True, "baseline", False, "baseline"]
    monkeypatch.setattr(suite, "ObjectiveAgent", Mock(return_value=agent))
    result = suite.run_case(suite.CASES[0])
    assert result["input_receipts"] == [{"status": "outcome_unknown", "phase": "input_click",
                                         "input_started": True, "error": "TimeoutError",
                                         "action_id": "e7"}]
    assert "do not persist" not in str(result) and result["retained_tab"] == "test-tab"
    agent.close.assert_not_called()


def test_main_skips_uncertain_repeat_and_keeps_credentials_out_of_report(monkeypatch, tmp_path):
    import json
    import sys

    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["regression", "--output", str(output), "--repeats", "2"])
    monkeypatch.setattr(suite, "preflight_environment", Mock())
    monkeypatch.setattr(suite, "CASES", (suite.CASES[3],))
    run = Mock(return_value={"case": "form-text", "passed": False, "uncertain_execution": True})
    monkeypatch.setattr(suite, "run_case", run)
    monkeypatch.setenv("PLANNER_API_KEY", "never-persist-this-test-key")
    assert suite.main() == 1
    run.assert_called_once()
    report = json.loads(output.read_text())
    assert report["runs"][1]["skipped"] and not report["runs"][1]["passed"]
    assert "never-persist-this-test-key" not in output.read_text()


def test_hn_article_fixture_uses_permalink_and_keeps_destination_checks():
    case = suite.CASES[1]
    assert case.url == "https://news.ycombinator.com/item?id=49922437"
    assert case.checks == (suite.Check("url", "https://arxiv.org/abs/2609.37725"),
                           suite.Check("title", "Context Language Models"))


def test_browser_setup_failure_records_safe_phase_and_cleanup_evidence(monkeypatch):
    from jev_ultrafast.browser import BrowserSetupError

    failure = BrowserSetupError("navigate", TimeoutError("do not persist raw server contents"))
    failure.cleanup_error, failure.retained_target = "RuntimeError", "owned-tab"
    monkeypatch.setattr(suite, "ObjectiveAgent", Mock(side_effect=failure))
    result = suite.run_case(suite.CASES[0])
    assert result["setup_phase"] == "navigate" and result["setup_cause"] == "TimeoutError"
    assert result["cleanup_error"] == "RuntimeError" and result["retained_tab"] == "owned-tab"
    assert "do not persist" not in str(result) and not result["passed"]


def test_navigation_does_not_claim_unassessed_form_safety():
    case = suite.CASES[0]
    before = '{"url":"source","fields":[["query","original"]]}'
    after = '{"url":"destination","fields":[["query","unexpected mutation"]]}'
    assert suite.safety_passed(case, before, after) is None
    # Navigation PASS means destination verified, not no-form-mutation proof.
    assert suite.passed(case, {**result_for(case), "safety_verified": None})


def test_escaped_post_action_exception_is_retained_even_if_controller_status_is_ready(monkeypatch):
    agent = fake_agent("ready", None)
    agent.run.side_effect = RuntimeError("Unexpected failure after input")
    agent.browser.evaluate.side_effect = [True, "baseline", False, "baseline"]
    monkeypatch.setattr(suite, "ObjectiveAgent", Mock(return_value=agent))
    result = suite.run_case(suite.CASES[3])
    assert result["run_error"] == "RuntimeError"
    assert result["uncertain_execution"] and result["retained_tab"] == "test-tab"
    assert not result["passed"]
    agent.close.assert_not_called()


def test_unavailable_fixture_cleanup_failure_is_preserved(monkeypatch):
    agent = fake_agent()
    agent.browser.evaluate.return_value = False
    agent.close.side_effect = RuntimeError("Cleanup failed")
    monkeypatch.setattr(suite, "ObjectiveAgent", Mock(return_value=agent))
    result = suite.run_case(suite.CASES[1])
    assert result["preflight_unavailable"] and result["cleanup_error"] == "RuntimeError"
    assert not result["passed"]
    agent.run.assert_not_called()


def test_snapshot_failure_preserves_original_execution_error(monkeypatch):
    agent = fake_agent("ready", None)
    agent.run.side_effect = RuntimeError("Original input failure")
    agent.snapshot.side_effect = ValueError("Later snapshot failure")
    agent.browser.evaluate.side_effect = [True, "baseline"]
    monkeypatch.setattr(suite, "ObjectiveAgent", Mock(return_value=agent))
    result = suite.run_case(suite.CASES[3])
    assert result["run_error"] == result["error"] == "RuntimeError"
    assert result["evidence_error"] == "ValueError"
    assert result["uncertain_execution"] and result["retained_tab"] == "test-tab"
    agent.close.assert_not_called()


def test_cancelled_suite_flushes_current_failure_and_stops(monkeypatch, tmp_path):
    import json
    import sys

    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["regression", "--output", str(output)])
    monkeypatch.setattr(suite, "preflight_environment", Mock())
    run = Mock(return_value={"case": "hn-new", "passed": False, "interrupted": True})
    monkeypatch.setattr(suite, "run_case", run)
    assert suite.main() == 130
    run.assert_called_once()
    assert json.loads(output.read_text())["runs"][0]["interrupted"]
