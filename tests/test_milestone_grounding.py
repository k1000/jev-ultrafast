"""Canonical autocomplete milestones use caller-owned exact values, not fuzzy verification."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_objective import FakeBrowser, chosen

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint
from jev_ultrafast.planning import parse_plan, verify

GOAL = "Select Zurich and open the results"
PLAN = {"objective": GOAL, "plan": [
    {"goal": "Select Zurich", "texts": [{"label": "Search", "value": "Zurich"}],
     "checks": [{"kind": "value", "label": "Search", "value": "Zurich"}]},
    {"goal": "Open results", "texts": [], "checks": [{"kind": "url", "value": "https://example.test/article"}]},
]}


class Autocomplete(FakeBrowser):
    def __init__(self, url):
        super().__init__(url)
        self.page["actions"][0]["role"] = "combobox"
        self.page["fingerprint"] = fingerprint(self.page)

    def act(self, action, page, text=None):
        if action["id"] == "suggestion":
            self.calls.append((action["id"], text))
            self.page["actions"][0]["value"] = "Zürich"
            self.page["actions"] = [a for a in self.page["actions"] if a["id"] != "suggestion"]
            self.page["fingerprint"] = fingerprint(self.page)
        else:
            super().act(action, page, text)
            if action["kind"] == "fill":
                self.page["actions"].append({"id": "suggestion", "kind": "click", "node": 3,
                                             "label": "Zürich, Switzerland", "role": "option", "value": ""})
                self.page["fingerprint"] = fingerprint(self.page)


def checks():
    return (Check("value", "Zürich", label="Search"), Check("url", "https://example.test/article"))


def test_query_does_not_advance_until_exact_caller_canonical_value_is_observed(monkeypatch):
    monkeypatch.setattr(loop, "Browser", Autocomplete)
    choices = Mock(side_effect=[chosen("e1"), chosen("suggestion", "CLICK"), chosen("e2", "CLICK")])
    monkeypatch.setattr(loop, "choose", choices)
    helper = Mock(side_effect=AssertionError("No text helper"))
    monkeypatch.setattr(loop, "field_texts", helper)
    planner = Mock(side_effect=AssertionError("No replan needed"))
    with ObjectiveAgent("https://example.test/", GOAL, checks=checks(), prepared_plan=deepcopy(PLAN),
                        planner=planner) as controller:
        typed = controller.command()
        assert typed["plan_index"] == 0 and controller.browser.calls == [("e1", "Zurich")]
        selected = controller.command()
        assert selected["plan_index"] == 1 and selected["verification"] == [True, False]
        assert controller.command()["status"] == "done"
        assert choices.call_args_list[0].kwargs["execution"]["step"]["checks"][0]["value"] == "Zürich"
        assert choices.call_args_list[2].args[1] == "Open results"
        assert controller.checks == checks()
        assert controller.plan.steps[0].texts[0].value == "Zurich"
        assert any(e["kind"] == "milestone_grounded" for e in controller.events)
    assert parse_plan(PLAN).steps[0].checks[0].value == "Zurich"  # Input plan not mutated.
    helper.assert_not_called()
    planner.assert_not_called()


@pytest.mark.parametrize("case", ["no_caller_value", "different_intent", "conflicting_callers", "ambiguous_control",
                                   "textbox", "native_select", "wrong_group"])
def test_grounding_does_not_guess_values_controls_or_unrelated_intents(monkeypatch, case):
    monkeypatch.setattr(loop, "Browser", Autocomplete)
    final = list(checks())
    plan = deepcopy(PLAN)
    if case == "no_caller_value":
        final = final[1:]
    elif case == "different_intent":
        plan["plan"][0]["checks"][0]["value"] = "Paris"
    elif case == "conflicting_callers":
        final.append(Check("value", "Zurich", label="Search"))
    elif case == "wrong_group":
        final[0] = Check("value", "Zürich", label="Search", group="different")
    with ObjectiveAgent("https://example.test/", GOAL, checks=final, prepared_plan=plan) as controller:
        actions = controller.browser.page["actions"]
        actions[0]["value"] = "Zürich"
        if case == "ambiguous_control":
            actions.append({**actions[0], "id": "other", "node": 99})
        elif case == "textbox":
            actions[0]["role"] = "textbox"
        elif case == "native_select":
            actions[0].update(kind="select", control_value="0", current_value="All")
        controller.observe()
        assert controller.plan.steps[0].checks[0].value == plan["plan"][0]["checks"][0]["value"]
        assert controller.index == 0


def test_initial_and_replanned_contracts_are_retained_before_grounding(monkeypatch):
    monkeypatch.setattr(loop, "Browser", Autocomplete)
    planner = Mock(side_effect=[(deepcopy(PLAN), {}), (deepcopy(PLAN), {})])
    with ObjectiveAgent("https://example.test/", GOAL, checks=checks(), planner=planner) as controller:
        controller.observe()
        controller.request_plan("initial")
        controller.observe()
        assert controller.plan.steps[0].checks[0].value == "Zürich"
        controller.request_plan("test_recovery")
        controller.observe()
        assert len(controller.planner_calls) == 2
        assert all(parse_plan(c["plan"]) == parse_plan(PLAN) for c in controller.planner_calls)
        assert controller.plan.steps[0].checks[0].value == "Zürich"


def test_final_verification_is_still_exact_even_for_accent_variants():
    page = {"actions": [{"kind": "fill", "node": 1, "label": "Search", "role": "combobox", "value": "Zurich"}]}
    assert verify(page, (Check("value", "Zürich", label="Search"),)) == [False]
