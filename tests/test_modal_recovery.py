"""Modal-hidden milestones are historical progress, never fresh final success. Offline boundaries."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_objective import FakeBrowser, chosen, search_plan

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import StalePage, fingerprint

GOAL = "Set Search to Ready and Filter to Approved"
SEARCH = {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "node": 1, "value": "Ready"}
FILTER = {"id": "e3", "kind": "fill", "label": "Filter", "role": "textbox", "node": 3, "value": ""}
SEARCH_CHECK = {"kind": "value", "label": "Search", "value": "Ready"}
FILTER_STEP = {"goal": "Set the dialog filter", "texts": [{"label": "Filter", "value": "Approved"}],
               "checks": [{"kind": "value", "label": "Filter", "value": "Approved"}]}
PLAN = {"objective": GOAL, "plan": [
    {"goal": "Set Search", "texts": [], "checks": [SEARCH_CHECK]}, FILTER_STEP,
]}
REPLAN = {"objective": GOAL, "plan": [
    {"goal": "Confirm Search remains Ready", "texts": [], "checks": [SEARCH_CHECK]}, FILTER_STEP,
]}
FINAL = (Check("value", "Ready", label="Search"), Check("value", "Approved", label="Filter"))


class ModalBrowser(FakeBrowser):
    def __init__(self, url):
        super().__init__(url)
        self.page.update(document_id=1, modal_open=False, actions=[deepcopy(SEARCH), deepcopy(FILTER)])
        self.refresh()

    def refresh(self):
        self.page["fingerprint"] = fingerprint(self.page)

    def open_modal(self):
        self.page.update(modal_open=True, actions=[deepcopy(FILTER)])
        self.refresh()

    def act(self, action, page, text=None):
        self.calls.append((action["id"], text))
        self.page["actions"][0]["value"] = text
        self.refresh()


def test_replan_confirmation_of_modal_hidden_progress_does_not_block_remaining_work(monkeypatch):
    monkeypatch.setattr(loop, "Browser", ModalBrowser)
    choices = Mock(side_effect=[chosen("BLOCKED", "BLOCKED"), chosen("e3")])
    monkeypatch.setattr(loop, "choose", choices)
    planner = Mock(return_value=(deepcopy(REPLAN), {}))
    with ObjectiveAgent("https://example.test/", GOAL, checks=FINAL, prepared_plan=deepcopy(PLAN),
                        planner=planner, local_attempts=0, max_replans=1) as controller:
        controller.observe()
        assert controller.index == 1
        controller.browser.open_modal()
        assert controller.command()["replans_used"] == 1
        state = controller.command()
        assert choices.call_args_list[-1].args[1] == "Set the dialog filter"
        assert controller.browser.calls == [("e3", "Approved")]
        assert state["status"] == "ready" and state["verification"] == [False, True]
        assert any(e["kind"] == "milestone_preserved" for e in state["events"])
    planner.assert_called_once()


@pytest.mark.parametrize("case", ["new_document", "unknown_document", "not_modal", "contradiction", "ambiguity"])
def test_stale_or_contradicted_progress_cannot_skip_a_replan_check(monkeypatch, case):
    monkeypatch.setattr(loop, "Browser", ModalBrowser)
    choices = Mock(return_value=chosen("BLOCKED", "BLOCKED"))
    monkeypatch.setattr(loop, "choose", choices)
    planner = Mock(return_value=(deepcopy(REPLAN), {}))
    with ObjectiveAgent("https://example.test/", GOAL, checks=FINAL, prepared_plan=deepcopy(PLAN),
                        planner=planner, local_attempts=0, max_replans=1) as controller:
        controller.observe()
        controller.browser.open_modal()
        if case == "new_document":
            controller.browser.page["document_id"] = 2
        elif case == "unknown_document":
            controller.browser.page.pop("document_id")
        elif case == "not_modal":
            controller.browser.page["modal_open"] = False
        else:
            wrong = {**SEARCH, "value": "Wrong"}
            controller.browser.page["actions"].append(wrong)
            if case == "ambiguity":
                controller.browser.page["actions"].append({**SEARCH, "node": 99, "id": "other"})
        controller.browser.refresh()
        controller.command()
        deferred = controller.command()
        assert choices.call_args_list[-1].args[1] == "Confirm Search remains Ready"
        assert deferred["status"] == "ready" and deferred["verification"] != [True, True]
        assert deferred["deferred_steps"] == [0] and deferred["verified_steps"] == []
        assert any(e["kind"] == "milestone_deferred" and e["index"] == 0 and
                   e["reason"] == "model_blocked" for e in deferred["events"])
        assert controller.browser.calls == []
        result = controller.command()
        assert choices.call_args_list[-1].args[1] == "Set the dialog filter"
        assert result["status"] == "abandoned" and result["stop_reason"] == "replanning_exhausted"
        assert result["verification"] != [True, True] and result["verified_steps"] == []
        assert result["deferred_steps"] == [0] and controller.browser.calls == []
        assert choices.call_count == 3


def test_transient_pre_input_change_settles_without_spending_recovery_or_replaying_input(monkeypatch):
    class SettlingBrowser(FakeBrowser):
        rejected = False

        def act(self, action, page, text=None):
            if not self.rejected:
                self.rejected = True
                self.page["text"] = "Stable new context"
                self.page["fingerprint"] = fingerprint(self.page)
                raise StalePage("Changed before any input")
            super().act(action, page, text)

    monkeypatch.setattr(loop, "Browser", SettlingBrowser)
    choices = Mock(return_value=chosen("e1"))
    monkeypatch.setattr(loop, "choose", choices)
    planner = Mock(side_effect=AssertionError("No replan needed for settled readonly changes"))
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner, local_attempts=0, max_replans=0) as controller:
        first = controller.command()
        assert first["status"] == "ready" and first["corrections_used"] == 0
        assert first["replans_used"] == 0 and controller.browser.calls == []
        assert first["events"][-1]["kind"] == "pre_input_settled"
        assert controller.command()["status"] == "done"
        assert controller.browser.calls == [("e1", "Ada Lovelace")]
        assert choices.call_count == 2  # Fresh decision required; rejected decision never replayed.
    planner.assert_not_called()


@pytest.mark.parametrize("changing", [False, True])
def test_persistent_staleness_has_bounded_reads_and_still_exhausts_configured_recovery(monkeypatch, changing):
    from jev_ultrafast import objective

    class NeverSettles(FakeBrowser):
        rejected = False
        reads = 0

        def act(self, *_args, **_kwargs):
            self.rejected = True
            raise StalePage("No input issued")

        def observe(self, screenshot=False):
            if self.rejected:
                self.reads += 1
                if changing:
                    self.page["text"] = f"New context {self.reads}"
                    self.page["fingerprint"] = fingerprint(self.page)
            return super().observe(screenshot)

    monkeypatch.setattr(loop, "Browser", NeverSettles)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    monkeypatch.setattr(objective.time, "sleep", Mock())
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner, local_attempts=0, max_replans=0) as controller:
        state = controller.command()
        assert state["status"] == "abandoned" and state["stop_reason"] == "unstable_observation"
        assert 2 <= controller.browser.reads <= 4 and controller.browser.calls == []
    planner.assert_not_called()


def test_final_success_still_requires_all_controls_freshly_visible_and_correct(monkeypatch):
    monkeypatch.setattr(loop, "Browser", ModalBrowser)
    choices = Mock(side_effect=[chosen("BLOCKED", "BLOCKED"), chosen("e3")])
    monkeypatch.setattr(loop, "choose", choices)
    planner = Mock(return_value=(deepcopy(REPLAN), {}))
    with ObjectiveAgent("https://example.test/", GOAL, checks=FINAL, prepared_plan=deepcopy(PLAN),
                        planner=planner, local_attempts=0, max_replans=1) as controller:
        controller.observe()
        controller.browser.open_modal()
        controller.command()
        assert controller.command()["status"] == "ready"
        controller.browser.page.update(modal_open=False, actions=[deepcopy(SEARCH), {**FILTER, "value": "Approved"}])
        controller.browser.refresh()
        state = controller.command()
        assert state["status"] == "done" and state["verification"] == [True, True]
        assert controller.browser.calls == [("e3", "Approved")]
