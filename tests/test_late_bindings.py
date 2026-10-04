"""Opt-in observed-target binding reuses immutable values, never text generation or fuzzy checks."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_objective import FakeBrowser, chosen

from jev_ultrafast import ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint
from jev_ultrafast.planning import Check

GOAL = "Open the editor and enter the supplied tracking reference"
VALUE = "TRK-42"
PLAN = {"objective": GOAL, "plan": [
    {"goal": "Open the editor", "texts": [], "checks": [{"kind": "url", "value": "https://example.test/article"}]},
    {"goal": "Enter the tracking reference", "texts": [{"label": "Search", "value": VALUE}],
     "checks": [{"kind": "value", "label": "Search", "value": VALUE}]},
]}


class ChangingView(FakeBrowser):
    def __init__(self, url):
        super().__init__(url)
        self.page.update(modal_open=False, omitted_actions=0, omitted_controls=0, document_id="owned-document")

    def act(self, action, page, text=None):
        if action["kind"] == "fill":
            self.calls.append((action["id"], text))
            next(a for a in self.page["actions"] if a["id"] == action["id"])["value"] = text
        else:
            super().act(action, page, text)
        if action["kind"] == "click":
            current = {"id": "e3", "node": 3, "kind": "fill", "role": "textbox",
                       "label": "Tracking reference", "value": "", "group": "Editor",
                       "observable": True, "disabled": False, "readonly": False}
            other = {**current, "id": "e4", "node": 4, "label": "Notes"}
            self.page["actions"] = [current, other]
            self.page["controls"] = [
                {"node": 1, "kind": "fill", "role": "textbox", "label": "Search",
                 "value": None, "observable": False},
                current, other,
            ]
        self.page["fingerprint"] = fingerprint(self.page)


def finite(choice, execution):
    """Offline chooser stub explicitly chooses one offered source-slot ID for an unbound fill."""
    options = execution.get("_binding_choices", {}).get(choice["choice"], {})
    return {**choice, **({"binding_choice": next(iter(options))} if options else {})}


def install(monkeypatch, browser):
    monkeypatch.setattr(loop, "Browser", lambda url: browser)
    decisions = []

    def decide(page, goal, history, *, execution):
        decisions.append(deepcopy(execution))
        return chosen("e2", "CLICK") if not history else finite(chosen("e3"), execution)

    monkeypatch.setattr(loop, "choose", decide)
    helper = Mock(side_effect=AssertionError("No runtime text generation"))
    monkeypatch.setattr(loop, "field_texts", helper)
    return decisions, helper


@pytest.mark.parametrize("enabled", [False, True])
def test_changing_view_selected_field_can_receive_one_current_step_value_only_with_opt_in(monkeypatch, enabled):
    browser = ChangingView("https://example.test/")
    decisions, helper = install(monkeypatch, browser)
    planner = Mock(side_effect=AssertionError("No replanning"))
    options = {"late_bindings": True} if enabled else {}
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), planner=planner,
                        checks=(Check("value", VALUE, label="Tracking reference"),), **options) as agent:
        states = list(agent.run())
        last = states[-1]
        if enabled:
            assert last["status"] == "done" and last["verification"] == [True]
            assert browser.calls == [("e2", None), ("e3", VALUE)]
            assert browser.page["actions"][1]["value"] == ""
            event = next(e for e in last["events"] if e["kind"] == "binding_late_bound")
            assert event["node"] == 3 and event["binding_step"] == 1 and event["binding_text"] == 0
            assert "label" not in event and "value" not in event
            assert decisions[-1]["late_bindings"] is True
            assert agent.plan.steps[1].texts[0].label == "Search"
            assert agent.plan.steps[1].checks[0].label == "Search"
        else:
            assert last["status"] == "abandoned" and browser.calls == [("e2", None)]
        assert last["text_calls"] == [] and last["replans_used"] == 0
        planner.assert_not_called()
        helper.assert_not_called()


@pytest.mark.parametrize("patch_page", [
    lambda p: p.update(modal_open=True),
    lambda p: p.pop("modal_open"),
    lambda p: p.pop("document_id"),
    lambda p: p.update(document_id=None),
    lambda p: p.update(document_id=True),
    lambda p: p.update(document_id=float("nan")),
    lambda p: p.update(document_id=float("inf")),
    lambda p: p.update(omitted_controls=1),
    lambda p: p.update(omitted_actions=1),
    lambda p: p.update(omitted_actions=False),
    lambda p: p.pop("omitted_controls"),
    lambda p: p.update(controls=[]),
    lambda p: p["controls"][0].update(observable=True),
    lambda p: p["controls"].append({**p["controls"][0], "node": 10}),
    lambda p: p["controls"].append({**p["controls"][1], "node": 10, "observable": False}),
    lambda p: p["controls"][1].update(readonly=True),
    lambda p: p["controls"][1].update(disabled=True),
    lambda p: p["controls"][1].update(observable=None),
    lambda p: p["controls"][1].update(value="different from the offered action"),
    lambda p: p["actions"][0].update(role="slider"),
    lambda p: p["actions"][0].update(label=""),
    lambda p: p["actions"].append(dict(p["actions"][0])),
])
def test_uncertain_or_conflicting_observations_do_not_authorize_late_input(monkeypatch, patch_page):
    class InvalidView(ChangingView):
        def act(self, action, page, text=None):
            super().act(action, page, text)
            if action["kind"] == "click":
                self.page["controls"] = deepcopy(self.page["controls"])
                patch_page(self.page)
                self.page["fingerprint"] = fingerprint(self.page)

    browser = InvalidView("https://example.test/")
    _, helper = install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] != "done" and browser.calls == [("e2", None)]
        assert last["replans_used"] == 0 and last["text_calls"] == []
        assert not any(e["kind"] == "binding_late_bound" for e in last["events"])
    helper.assert_not_called()


@pytest.mark.parametrize("text_patch", [dict(role="combobox"), dict(group="Lookup")])
def test_explicit_role_and_group_are_not_overridden_by_late_binding(monkeypatch, text_patch):
    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    plan = deepcopy(PLAN)
    plan["plan"][1]["texts"][0].update(text_patch)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=plan, late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] != "done" and browser.calls == [("e2", None)]


def test_late_binding_never_invents_a_value_without_current_prepared_text(monkeypatch):
    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    plan = deepcopy(PLAN)
    plan["plan"][1]["texts"] = []
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=plan, late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] != "done" and browser.calls == [("e2", None)]


def test_late_mapping_does_not_remap_caller_checks_or_prove_success(monkeypatch):
    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Search"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] != "done" and last["verification"] == [False]
        assert browser.calls == [("e2", None), ("e3", VALUE)]
        assert agent.checks[0].label == "Search"
        assert len([e for e in last["events"] if e["kind"] == "binding_late_bound"]) == 1


@pytest.mark.parametrize("bad", [0, 1, "true", None])
def test_late_binding_option_is_a_literal_boolean_validated_before_browser_setup(monkeypatch, bad):
    browser = Mock(side_effect=AssertionError("No browser for invalid settings"))
    monkeypatch.setattr(loop, "Browser", browser)
    with pytest.raises(ValueError, match="late binding"):
        ObjectiveAgent("https://example.test/", GOAL, late_bindings=bad,
                       checks=(Check("title", "Editor"),))
    browser.assert_not_called()


def two_fields_plan(*, same_value=False):
    plan = deepcopy(PLAN)
    plan["plan"].append({"goal": "Enter the delivery reference", "texts": [
        {"label": "Search" if same_value else "Old delivery field", "value": VALUE if same_value else "DROP-7"}],
        "checks": [{"kind": "value", "label": "Search" if same_value else "Old delivery field",
                    "value": VALUE if same_value else "DROP-7"}]})
    return plan


@pytest.mark.parametrize("same_value", [False, True])
def test_acknowledged_bindings_ground_only_their_own_step_then_use_the_next_value(monkeypatch, same_value):
    browser = ChangingView("https://example.test/")
    _, helper = install(monkeypatch, browser)
    selected_steps = []

    def decide(page, goal, history, *, execution):
        selected_steps.append(execution["step_index"])
        return chosen("e2", "CLICK") if not history else finite(
            chosen("e3" if len(history) == 1 else "e4"), execution) | {
                "value": "MODEL OUTPUT IS NOT INPUT", "text": "MODEL OUTPUT IS NOT INPUT"}

    monkeypatch.setattr(loop, "choose", decide)
    final_value = VALUE if same_value else "DROP-7"
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=two_fields_plan(same_value=same_value),
                        late_bindings=True, checks=(Check("value", VALUE, label="Tracking reference"),
                                                   Check("value", final_value, label="Notes"))) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "done" and last["verification"] == [True, True]
        assert browser.calls == [("e2", None), ("e3", VALUE), ("e4", final_value)]
        assert selected_steps == [0, 1, 2] and last["verified_steps"] == [0, 1, 2]
        assert [s.texts[0].label for s in agent.plan.steps[1:]] == [
            "Search", "Search" if same_value else "Old delivery field"]
    helper.assert_not_called()


@pytest.mark.parametrize("change_after_fill", [
    lambda p: p["actions"][0].update(value="application changed or cleared the value"),
    lambda p: p.update(document_id="replacement-document"),
    lambda p: p["actions"][0].update(label="Recycled as a different field"),
    lambda p: p["actions"][0].update(role="combobox"),
    lambda p: p["actions"][0].update(node=30),
    lambda p: p["actions"].append({**p["actions"][0], "node": 30}),
])
def test_failed_fresh_bound_value_proof_stops_without_typing_any_destination(monkeypatch, change_after_fill):
    class ChangedAfterFill(ChangingView):
        def act(self, action, page, text=None):
            super().act(action, page, text)
            if action["kind"] == "fill":
                change_after_fill(self.page)
                self.page["controls"] = [self.page["controls"][0], *self.page["actions"]]
                self.page["fingerprint"] = fingerprint(self.page)

    browser = ChangedAfterFill("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=two_fields_plan(), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),
                                Check("value", "DROP-7", label="Notes"))) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "needs_attention" and last["stop_reason"] == "late_binding_unverified"
        assert browser.calls == [("e2", None), ("e3", VALUE)]
        assert browser.page["actions"][1]["value"] == ""
        assert last["verified_steps"] == [0]
        assert (1, 0) not in agent.value_owners and agent.pending_binding is not None


def test_uncertain_native_input_cannot_create_acknowledged_evidence_or_rebind(monkeypatch):
    class LostAcknowledgment(ChangingView):
        def act(self, action, page, text=None):
            super().act(action, page, text)
            if action["kind"] == "fill":
                raise RuntimeError("Native acknowledgment lost after insertion")

    browser = LostAcknowledgment("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=two_fields_plan(), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),
                                Check("value", "DROP-7", label="Notes"))) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "needs_attention" and last["verified_steps"] == [0]
        assert browser.calls == [("e2", None), ("e3", VALUE)]
        assert (1, 0) not in agent.value_owners and agent.pending_binding is not None
        assert last["attempts"][-1]["status"] == "outcome_unknown"
        with pytest.raises(ValueError, match="active objective"):
            agent.command()
        assert browser.calls == [("e2", None), ("e3", VALUE)]


def test_known_pre_input_rejection_does_not_rebind_a_reserved_value(monkeypatch):
    from jev_ultrafast.browser import StalePage

    class Rejected(ChangingView):
        def act(self, action, page, text=None):
            if action["kind"] == "fill":
                raise StalePage("Rejected by the execution guard before native input")
            super().act(action, page, text)

    browser = Rejected("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=two_fields_plan(), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "needs_attention" and last["stop_reason"] == "late_binding_rejected"
        assert browser.calls == [("e2", None)] and (1, 0) not in agent.value_owners
        assert last["attempts"][-1]["status"] == "rejected_before_input"


@pytest.mark.parametrize("checks", [[{"kind": "value", "label": "Search", "value": "DIFFERENT"}],
    [{"kind": "value", "label": "Different contract", "value": VALUE}],
    [{"kind": "value", "label": "Search", "value": VALUE}] * 2])
def test_missing_or_duplicate_corresponding_planner_predicate_cannot_authorize_input(monkeypatch, checks):
    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    plan = deepcopy(PLAN)
    plan["plan"][1]["checks"] = checks
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=plan, late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] != "done" and browser.calls == [("e2", None)]
        assert agent.value_owners == {}


def test_nonfield_milestone_requires_original_fresh_evidence_not_a_value_alias(monkeypatch):
    class Suggestions(ChangingView):
        def act(self, action, page, text=None):
            super().act(action, page, text)
            if action["kind"] == "fill":
                self.page["text"] = "Suggestions: " + text
                self.page["fingerprint"] = fingerprint(self.page)

    browser = Suggestions("https://example.test/")
    install(monkeypatch, browser)
    plan = deepcopy(PLAN)
    plan["plan"][1]["checks"] = [{"kind": "text", "value": VALUE}]
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=plan, late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),
                                Check("text", VALUE))) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "done" and last["verification"] == [True, True]
        assert browser.calls == [("e2", None), ("e3", VALUE)]
        assert agent.plan.steps[1].checks == (Check("text", VALUE),)


def test_unmet_nonfield_milestone_cannot_move_the_reserved_value_into_destination(monkeypatch):
    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    selected = []

    def decide(page, goal, history, *, execution):
        selected.append(execution["step_index"])
        return chosen("e2", "CLICK") if not history else finite(
            chosen("e3" if len(history) == 1 else "e4"), execution)

    monkeypatch.setattr(loop, "choose", decide)
    plan = two_fields_plan()
    plan["plan"][1]["checks"] = [{"kind": "text", "value": "Still no selected result"}]
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=plan, late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),
                                Check("value", "DROP-7", label="Notes"))) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "abandoned" and last["verified_steps"] == [0]
        assert browser.calls == [("e2", None), ("e3", VALUE)]
        assert browser.page["actions"][1]["value"] == ""
        assert all(index == 1 for index in selected[1:])


@pytest.mark.parametrize("document", [1770000000000, 1770000000000.25])
def test_native_numeric_document_identities_can_reserve_and_verify_a_binding(monkeypatch, document):
    browser = ChangingView("https://example.test/")
    browser.page["document_id"] = document
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "done" and browser.calls == [("e2", None), ("e3", VALUE)]
        assert agent.value_owners[(1, 0)]["document"] == document


@pytest.mark.parametrize("status", ["error", "rejected_before_input", "outcome_unknown"])
def test_nonexecuted_adapter_receipt_never_acknowledges_or_proves_a_binding(monkeypatch, status):
    class BadAdapter(ChangingView):
        def act(self, action, page, text=None):
            super().act(action, page, text)
            if action["kind"] == "fill":
                return {"status": status}

    browser = BadAdapter("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "needs_attention" and last["stop_reason"] == "late_binding_unacknowledged"
        assert (1, 0) not in agent.value_owners and last["verified_steps"] == [0]
        assert browser.calls == [("e2", None), ("e3", VALUE)]


def test_second_post_action_read_cannot_keep_contradicted_first_read_evidence(monkeypatch):
    class ContradictoryReads(ChangingView):
        reads_after_fill = 0

        def observe(self, screenshot=False):
            if len(self.calls) == 2:
                self.reads_after_fill += 1
                if self.reads_after_fill == 2:
                    self.page["actions"][0]["value"] = "Changed after the first result read"
                    self.page["fingerprint"] = fingerprint(self.page)
            return super().observe(screenshot=screenshot)

    browser = ContradictoryReads("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] == "needs_attention" and last["stop_reason"] == "late_binding_unverified"
        assert last["verified_steps"] == [0] and last["verification"] == [False]
        assert browser.calls == [("e2", None), ("e3", VALUE)]


def test_returning_original_caption_cannot_move_an_unresolved_reservation(monkeypatch):
    class OriginalReturns(ChangingView):
        def act(self, action, page, text=None):
            super().act(action, page, text)
            if action["kind"] == "fill":
                old = {**self.page["actions"][0], "id": "e5", "node": 1, "label": "Search", "value": ""}
                self.page["actions"].append(old)
                self.page["controls"] = list(self.page["actions"])
                self.page["fingerprint"] = fingerprint(self.page)

    browser = OriginalReturns("https://example.test/")
    install(monkeypatch, browser)

    def decide(page, goal, history, *, execution):
        return chosen("e2", "CLICK") if not history else finite(
            chosen("e3" if len(history) == 1 else "e5"), execution)

    monkeypatch.setattr(loop, "choose", decide)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=two_fields_plan(), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),
                                Check("value", "DROP-7", label="Notes"))) as agent:
        last = list(agent.run())[-1]
        assert last["status"] != "done" and last["verified_steps"] == [0]
        assert browser.calls == [("e2", None), ("e3", VALUE)]
        assert browser.page["actions"][-1]["value"] == ""
