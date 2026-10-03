"""Offline, redacted policy trajectory reporting; no provider or browser calls."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_objective import FakeBrowser, chosen, search_plan

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import ExecutionUncertain, fingerprint
from jev_ultrafast.evaluation import evaluate_trajectories, record_trajectory


def test_verified_trajectory_uses_only_safe_decision_and_outcome_categories(monkeypatch):
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    planner = Mock(side_effect=AssertionError("Prepared plan must not call a larger model"))
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner) as controller:
        report = record_trajectory(controller.run())
    assert report["outcome"] == "verified" and report["score"] == 1
    assert report["decisions"] == report["prediction_attempts"] == report["actions"] == 1
    assert report["planner_calls"] == report["runtime_replans"] == report["text_calls"] == 0
    assert report["transitions"] == [{"tick": 1, "operation": "TYPE_TEXT", "action": "fill",
                                       "verification": "met", "status": "done", "stop_reason": "verified"}]
    encoded = json.dumps(report)
    assert "Ada Lovelace" not in encoded and "example.test" not in encoded
    assert "Search" not in encoded and "e1" not in encoded
    planner.assert_not_called()


def test_blocked_policy_is_unverified_without_a_runtime_replan(monkeypatch):
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("BLOCKED", "BLOCKED")))
    planner = Mock(return_value=({"objective": "Open missing article", "plan": []}, {}))
    with ObjectiveAgent("https://example.test/", "Open missing article",
                        checks=(Check("url", "https://example.test/missing"),), planner=planner) as controller:
        report = record_trajectory(controller.run())
    assert report["outcome"] == "abandoned" and report["score"] == 0
    assert report["planner_calls"] == 1 and report["runtime_replans"] == 0
    assert report["decisions"] == 3 and report["actions"] == 0
    assert all(t["operation"] == "BLOCKED" and t["verification"] == "unmet"
               for t in report["transitions"])
    planner.assert_called_once()


def test_uncertain_execution_is_inconclusive_not_a_failed_reward(monkeypatch):
    class Uncertain(FakeBrowser):
        def act(self, action, page, text=None):
            raise RuntimeError("Possibly sent secret value")

    monkeypatch.setattr(loop, "Browser", Uncertain)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan()) as controller:
        report = record_trajectory(controller.run())
    assert report["outcome"] == "inconclusive" and report["score"] is None
    assert report["decisions"] == 1 and report["actions"] == 0
    assert report["mutation_attempts"] == 1 and report["uncertain_attempts"] == 1
    assert "secret" not in json.dumps(report).lower()


def test_uncertain_scroll_attempt_is_not_a_confirmed_action_or_retried(monkeypatch):
    class UncertainScroll(FakeBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page["actions"][0].update(kind="scroll_to", role="button", label="Reveal target")
            self.page["fingerprint"] = fingerprint(self.page)

        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))
            self.page["scroll"]["y"] = 120
            self._uncertain_receipt = {"status": "outcome_unknown", "phase": "scroll_to", "input_started": True}
            raise ExecutionUncertain(self._uncertain_receipt)

    monkeypatch.setattr(loop, "Browser", UncertainScroll)
    chooser = Mock(return_value=chosen("e1", "SCROLL_TO"))
    monkeypatch.setattr(loop, "choose", chooser)
    planner = Mock(side_effect=AssertionError("No planning during execution"))
    with ObjectiveAgent("https://example.test/", "Reveal target", checks=(Check("text", "Missing"),),
                        prepared_plan={"objective": "Reveal target", "plan": []}, planner=planner) as controller:
        report = record_trajectory(controller.run())
        assert len(controller.agent.state["history"]) == 1
        assert controller.agent.state["history"][0]["attempted"] is True
        assert report["outcome"] == "inconclusive" and report["score"] is None
        assert report["actions"] == 0 and report["mutation_attempts"] == report["uncertain_attempts"] == 1
        assert report["transitions"][0]["operation"] == "SCROLL_TO"
        assert report["transitions"][0]["action"] is None
        assert controller.browser.page["scroll"]["y"] == 120 and len(controller.browser.calls) == 1
    assert controller.browser.closed is False
    chooser.assert_called_once()
    planner.assert_not_called()


@pytest.mark.parametrize("read_fails", [False, True])
def test_acknowledged_scroll_stays_counted_even_if_result_observation_fails(monkeypatch, read_fails):
    class AcknowledgedScroll(FakeBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page["actions"][0].update(kind="scroll_to", role="button", label="Reveal target")
            self.page["fingerprint"] = fingerprint(self.page)

        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))
            self.page.update(text="Complete")
            self.page["scroll"]["y"] = 120
            self.page["fingerprint"] = fingerprint(self.page)

        def observe(self, screenshot=False):
            if read_fails and self.calls:
                raise RuntimeError("Post-input observation unavailable")
            return super().observe(screenshot)

    monkeypatch.setattr(loop, "Browser", AcknowledgedScroll)
    chooser = Mock(return_value=chosen("e1", "SCROLL_TO"))
    monkeypatch.setattr(loop, "choose", chooser)
    with ObjectiveAgent("https://example.test/", "Reveal target", checks=(Check("text", "Complete"),),
                        prepared_plan={"objective": "Reveal target", "plan": []}) as controller:
        report = record_trajectory(controller.run())
        assert report["outcome"] == ("inconclusive" if read_fails else "verified")
        assert report["actions"] == report["mutation_attempts"] == 1 and report["uncertain_attempts"] == 0
        assert report["transitions"][0]["action"] == "scroll_to"
        assert "attempted" not in controller.agent.state["history"][0]
    chooser.assert_called_once()


def test_precollected_mutable_snapshots_are_rejected_not_miscounted(monkeypatch):
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("BLOCKED", "BLOCKED")))
    with ObjectiveAgent("https://example.test/", "Open missing article",
                        checks=(Check("url", "https://example.test/missing"),),
                        prepared_plan={"objective": "Open missing article", "plan": []}) as controller:
        snapshots = list(controller.run())
    with pytest.raises(ValueError, match="stream"):
        record_trajectory(snapshots)


def test_report_cannot_promote_a_done_label_without_met_caller_evidence(monkeypatch):
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    with ObjectiveAgent("https://example.test/", "Open missing article",
                        checks=(Check("url", "https://example.test/missing"),),
                        prepared_plan={"objective": "Open missing article", "plan": []}) as controller:
        state = deepcopy(controller.snapshot())
        state.update(status="done", stop_reason="verified", verification=[False],
                     check_evidence=[{"state": "unmet"}])
        report = record_trajectory([state])
        assert report["outcome"] == "inconclusive" and report["score"] is None
        assert report["transitions"][0]["verification"] == "unmet"


def test_aggregate_reports_counts_inconclusive_and_incomplete_runs_in_denominator():
    def report(outcome, score):
        return {"outcome": outcome, "score": score, "decisions": 2, "actions": 1,
                "planner_calls": 1, "runtime_replans": 0, "text_calls": 0}

    summary = evaluate_trajectories([report("verified", 1), report("abandoned", 0),
                                     report("inconclusive", None), report("incomplete", None)])
    assert summary == {"runs": 4, "verified": 1, "abandoned": 1, "inconclusive": 1,
                       "incomplete": 1, "verified_rate": 0.25, "decisions": 8, "actions": 4,
                       "planner_calls": 4, "runtime_replans": 0, "text_calls": 0}
    assert evaluate_trajectories([])["verified_rate"] is None
