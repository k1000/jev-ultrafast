"""P7 producer hashes actual chooser observations without publishing their raw contents."""

import hashlib
import json
from copy import deepcopy
from unittest.mock import Mock

from test_objective import FakeBrowser, chosen

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint
from jev_ultrafast.evaluation import record_trajectory


def digest(marker):
    return hashlib.sha256(json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MarkerBrowser(FakeBrowser):
    def __init__(self, url):
        super().__init__(url)
        self.page.update(modal_open=False, semantic_marker=[1, "SECRET initial", False, [], []])
        self.page["fingerprint"] = fingerprint(self.page)

    def act(self, action, page, text=None):
        super().act(action, page, text)
        self.page["semantic_marker"] = [1, "SECRET final", False, [], []]
        self.page["fingerprint"] = fingerprint(self.page)


def test_streamed_objective_decision_has_digest_and_predecision_context_not_raw_page(monkeypatch):
    monkeypatch.setattr(loop, "Browser", MarkerBrowser)
    seen = []

    def choose(_page, _goal, _history, *, execution):
        seen.append(deepcopy(execution))
        return chosen("e2", "CLICK")

    monkeypatch.setattr(loop, "choose", choose)
    planner = Mock(side_effect=AssertionError("No paid planner"))
    goal = "Open article"
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/article"),),
                        prepared_plan={"objective": goal, "plan": []}, planner=planner) as controller:
        report = record_trajectory(controller.run())
        assert report["outcome"] == "verified"
        assert report["decision_states"] == [{
            "tick": 1, "semantic_digest": digest([1, "SECRET initial", False, [], []]),
            "modal_open": False, "verification": [False], "check_evidence_transition": {
                "before": ["unmet"], "after": ["met"]}, "completed_checks_count": 0,
            "index": 0, "last_mutation_present": False, "receipt_category": "executed",
        }]
        decision = controller.agent.state["decisions"][0]
        assert decision["state_digest"] == report["decision_states"][0]["semantic_digest"]
        assert decision["state_context"]["verification"] == [False]
        assert "SECRET" not in json.dumps(report)
        assert "SECRET" not in json.dumps(decision)
        assert all("state_digest" not in json.dumps(ctx) and "state_context" not in json.dumps(ctx)
                   for ctx in seen)
    planner.assert_not_called()


def test_refreshed_chooser_page_not_prior_objective_page_is_hashed(monkeypatch):
    class Refresh(MarkerBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.reads = 0
            self.refreshed = False

        def observe(self, screenshot=False):
            self.reads += 1
            if self.reads == 3:  # Agent.predict re-observation, after constructor and Objective.observe.
                self.page["semantic_marker"] = [2, "SECRET refreshed", True, [], []]
                self.page["modal_open"] = True
                self.page["fingerprint"] = fingerprint(self.page)
            return super().observe(screenshot)

        def fresh(self, page, action=None, *, terminal=False):
            if action is None and not terminal and self.reads == 2 and not self.refreshed:
                self.refreshed = True
                return False
            return super().fresh(page, action, terminal=terminal)

    monkeypatch.setattr(loop, "Browser", Refresh)
    chosen_pages = []

    def choose(page, _goal, _history, *, execution):
        chosen_pages.append(deepcopy(page))
        return chosen("e2", "CLICK")

    monkeypatch.setattr(loop, "choose", choose)
    goal = "Open article"
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/article"),),
                        prepared_plan={"objective": goal, "plan": []}) as controller:
        result = controller.command()
        decision = result["decisions"][0]
        assert len(chosen_pages) == 1
        assert chosen_pages[0]["semantic_marker"] == [2, "SECRET refreshed", True, [], []]
        assert decision["state_digest"] == digest(chosen_pages[0]["semantic_marker"])
        assert decision["state_digest"] != digest([1, "SECRET initial", False, [], []])
        assert decision["state_context"]["modal_open"] is True
        assert decision["state_context"]["verification"] is None
        assert decision["state_context"]["check_evidence_states"] is None
        assert "SECRET" not in json.dumps(decision)


def test_failed_prediction_and_legacy_no_marker_never_invent_decision_state(monkeypatch):
    monkeypatch.setattr(loop, "Browser", MarkerBrowser)
    fail = Mock(side_effect=ValueError("Offline model interruption"))
    monkeypatch.setattr(loop, "choose", fail)
    with loop.Agent("https://example.test/", "Open article") as agent:
        agent.decision_context = {"verification": [False], "check_evidence_states": ["unmet"],
                                  "completed_checks_count": 0, "index": 0, "last_mutation_present": False}
        try:
            agent.command("predict")
        except ValueError:
            pass
        assert agent.state["decisions"] == []
        assert agent.state["prediction_calls"][-1]["status"] == "error"
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("BLOCKED", "BLOCKED")))
    with loop.Agent("https://example.test/", "Open article") as agent:
        agent.command("predict")
        assert "state_digest" not in agent.state["decisions"][0]
        assert "state_context" not in agent.state["decisions"][0]
