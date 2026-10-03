"""Plans guide execution; fresh caller criteria alone establish completion. Offline boundaries."""

from copy import deepcopy
from unittest.mock import Mock

from test_objective import FakeBrowser, chosen

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import StalePage


def test_default_policy_uses_one_upfront_plan_and_no_runtime_llm_on_blocked(monkeypatch):
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    decisions = Mock(return_value=chosen("BLOCKED", "BLOCKED"))
    monkeypatch.setattr(loop, "choose", decisions)
    helper = Mock(side_effect=AssertionError("No per-action text helper"))
    monkeypatch.setattr(loop, "field_texts", helper)
    goal = "Open the missing article"
    planner = Mock(return_value=({"objective": goal, "plan": []}, {"model": "upfront-only"}))
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/missing"),),
                        planner=planner) as controller:
        result = list(controller.run())[-1]
        assert result["status"] == "abandoned" and result["stop_reason"] == "replanning_exhausted"
        assert result["verification"] == [False] and result["replans_used"] == 0
        assert len(result["planner_calls"]) == 1
        assert result["planner_calls"][0]["phase"] == "initial"
        assert result["text_calls"] == [] and controller.browser.calls == []
        assert len(decisions.call_args_list) == 3  # Bounded local corrections, then stop.
    planner.assert_called_once()
    helper.assert_not_called()


def test_bad_intermediate_check_cannot_lock_jev_out_of_remaining_work(monkeypatch):
    objective = "Search Ada Lovelace and open the article"
    plan = {"objective": objective, "plan": [
        {"goal": "Enter the query", "texts": [{"label": "Search", "value": "Ada Lovelace"}],
         "checks": [{"kind": "value", "label": "Search", "value": "Invented display spelling"}]},
        {"goal": "Open the article", "texts": [],
         "checks": [{"kind": "url", "value": "https://example.test/article"}]},
    ]}
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    choices = Mock(side_effect=[chosen("e1"), chosen("DONE", "DONE"), chosen("e2", "CLICK")])
    monkeypatch.setattr(loop, "choose", choices)
    planner = Mock(side_effect=AssertionError("An advisory check mismatch must not cause replanning"))
    with ObjectiveAgent("https://example.test/", objective,
                        checks=(Check("url", "https://example.test/article"),),
                        prepared_plan=deepcopy(plan), planner=planner) as controller:
        assert controller.command()["status"] == "ready"
        deferred = controller.command()
        assert deferred["status"] == "ready" and deferred["corrections_used"] == 0
        assert deferred["verification"] == [False]
        assert any(e["kind"] == "milestone_deferred" for e in deferred["events"])
        final = controller.command()
        assert choices.call_args_list[-1].args[1] == "Open the article"
        assert final["status"] == "done" and final["verification"] == [True]
        assert controller.browser.calls == [("e1", "Ada Lovelace"), ("e2", None)]
    planner.assert_not_called()


def test_new_semantic_state_gets_local_correction_before_expensive_replanning(monkeypatch):
    from jev_ultrafast.browser import fingerprint

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("BLOCKED", "BLOCKED")))
    planner = Mock(side_effect=AssertionError("A changed control is not a stable dead-end"))
    goal = "Open the missing article"
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/missing"),),
                        prepared_plan={"objective": goal, "plan": []}, planner=planner) as controller:
        controller.command()
        controller.command()
        controller.browser.page["actions"][0]["value"] = "New selection"
        controller.browser.page["fingerprint"] = fingerprint(controller.browser.page)
        assert controller.command()["status"] == "ready"
        assert controller.snapshot()["corrections_used"] == 3
    planner.assert_not_called()


def test_dom_identity_and_text_churn_do_not_reset_stable_blocker_budget(monkeypatch):
    from jev_ultrafast.browser import fingerprint

    class Churn(FakeBrowser):
        reads = 0

        def observe(self, screenshot=False):
            self.reads += 1
            self.page["text"] = f"Unrelated ticker {self.reads}"
            self.page["actions"][0]["node"] = self.reads
            self.page["actions"][0]["rect"] = {"x": self.reads, "y": 0}
            self.page["fingerprint"] = fingerprint(self.page)
            return super().observe(screenshot)

    monkeypatch.setattr(loop, "Browser", Churn)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("BLOCKED", "BLOCKED")))
    goal = "Open the missing article"
    planner = Mock(return_value=({"objective": goal, "plan": []}, {}))
    with ObjectiveAgent("https://example.test/", goal, checks=(Check("url", "https://example.test/missing"),),
                        prepared_plan={"objective": goal, "plan": []}, planner=planner,
                        max_replans=1) as controller:
        controller.command()
        controller.command()
        controller.command()
        planner.assert_called_once()
        assert controller.browser.calls == []


def test_persistent_pre_input_staleness_stops_without_a_semantic_replan(monkeypatch):
    class Rejected(FakeBrowser):
        def act(self, *_args, **_kwargs):
            raise StalePage("No input issued")

    monkeypatch.setattr(loop, "Browser", Rejected)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    planner = Mock(side_effect=AssertionError("The planner cannot fix an unstable input boundary"))
    goal = "Enter Ada in Search"
    plan = {"objective": goal, "plan": [{"goal": goal, "texts": [{"label": "Search", "value": "Ada"}],
            "checks": [{"kind": "value", "label": "Search", "value": "Ada"}]}]}
    with ObjectiveAgent("https://example.test/", goal, checks=(Check("value", "Ada", label="Search"),),
                        prepared_plan=plan, planner=planner) as controller:
        states = list(controller.run())
        assert states[-1]["status"] == "abandoned" and states[-1]["stop_reason"] == "unstable_observation"
        assert len(states) == 3 and controller.browser.calls == []
        assert states[-1]["corrections_used"] == 0
    planner.assert_not_called()


def test_jev_can_use_a_future_prepared_binding_to_resolve_current_modal(monkeypatch):
    class DialogBrowser(FakeBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page.update(modal_open=True, document_id=1, actions=[
                {"id": "e3", "kind": "fill", "label": "Filter", "role": "textbox", "value": "", "node": 3},
            ])

        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))
            self.page["actions"][0]["value"] = text

    objective = "Set Search to Ready and Filter to Approved"
    plan = {"objective": objective, "plan": [
        {"goal": "Confirm Search", "texts": [], "checks": [{"kind": "value", "label": "Search", "value": "Ready"}]},
        {"goal": "Set Filter", "texts": [{"label": "Filter", "value": "Approved"}],
         "checks": [{"kind": "value", "label": "Filter", "value": "Approved"}]},
    ]}
    monkeypatch.setattr(loop, "Browser", DialogBrowser)
    choice = Mock(return_value=chosen("e3"))
    monkeypatch.setattr(loop, "choose", choice)
    planner = Mock(side_effect=AssertionError("Modal resolution must not call the planner"))
    with ObjectiveAgent("https://example.test/", objective,
                        checks=(Check("value", "Ready", label="Search"), Check("value", "Approved", label="Filter")),
                        prepared_plan=plan, planner=planner) as controller:
        state = controller.command()
        assert controller.browser.calls == [("e3", "Approved")]
        assert state["status"] == "ready" and state["verification"] == [False, True]
        assert state["check_evidence"][0]["state"] == "unknown"
        assert state["corrections_used"] == 0 and state["text_calls"] == []
    planner.assert_not_called()
