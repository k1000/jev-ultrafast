"""Policy guards deny before input; rejection is neither staleness nor execution uncertainty."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import choice
from test_late_bindings import GOAL, PLAN, VALUE, ChangingView, install
from test_objective import FakeBrowser

from jev_ultrafast import Check, ObjectiveAgent, model
from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.browser import Browser, PolicyRejected, StalePage, fingerprint


class GuardedBrowser(Browser):
    def validate_action(self, action, page, text=None):
        super().validate_action(action, page, text)
        raise PolicyRejected("Secret caller policy")


def test_policy_receipt_is_distinct_and_never_issues_mutation(monkeypatch):
    browser = GuardedBrowser.__new__(GuardedBrowser)
    browser.session = "offline"
    browser.call = Mock(side_effect=AssertionError("Never contact the browser"))
    action = {"id": "e1", "kind": "click", "node": 1, "label": "Dismiss dialog", "value": ""}
    page = {"actions": [action]}
    receipt = browser.execute(action, page)
    assert receipt["status"] == "rejected_by_policy" and receipt["phase"] == "validation"
    assert receipt["input_started"] is False and receipt["calls"] == []
    browser.call.assert_not_called()
    with pytest.raises(PolicyRejected) as error:
        browser.act(action, page)
    assert error.value.receipt["status"] == "rejected_by_policy"
    assert "Secret" not in str(error.value) and "Secret" not in str(error.value.receipt)
    assert not hasattr(browser, "_uncertain_receipt")
    browser.call.assert_not_called()


def test_native_validation_is_not_misclassified_as_caller_policy():
    browser = GuardedBrowser.__new__(GuardedBrowser)
    browser.session = "offline"
    browser.call = Mock(side_effect=AssertionError("No input"))
    action = {"id": "e1", "kind": "click", "node": 1, "label": "Dismiss dialog"}
    with pytest.raises(StalePage) as error:
        browser.act(action, {"actions": []})
    assert error.value.receipt["status"] == "rejected_before_input"
    browser.call.assert_not_called()


class DialogBrowser(FakeBrowser):
    forbidden = frozenset({"Dismiss sign in information"})

    def __init__(self, url):
        super().__init__(url)
        self.page.update(text="Sign in information", modal_open=True, semantic_marker=["dialog", "unchanged"],
                         actions=[
                             {"id": "first", "kind": "click", "node": 1, "role": "button", "label":
                              "Dismiss sign in information", "value": ""},
                             {"id": "second", "kind": "click", "node": 2, "role": "button", "label":
                              "Dismiss dialog", "value": ""},
                         ])
        self.page["fingerprint"] = fingerprint(self.page)

    def act(self, action, page, text=None):
        if action["label"] in self.forbidden:
            raise PolicyRejected("Not allowed by test policy")
        self.calls.append((action["id"], text))
        self.page.update(url="https://example.test/closed", modal_open=False,
                         semantic_marker=["closed", "changed"])
        self.page["fingerprint"] = fingerprint(self.page)
        return {"status": "executed", "input_started": True}


def policy_choices(monkeypatch):
    requests = []

    def post(_url, _key, body):
        requests.append(deepcopy(body))
        criteria = body["questions"]["click_target"]["criteria"]
        selected = next((i for i, c in criteria.items() if "Dismiss sign in information" in c["element"]), None)
        if selected is None:
            selected = next(i for i, c in criteria.items() if "Dismiss dialog" in c["element"])
        return {"model": "offline-stub", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
            "click_target": choice(criteria, selected),
        }}

    blocked = Mock(side_effect=AssertionError("No live browser, planner or text helper"))
    monkeypatch.setattr(loop, "Browser", DialogBrowser)
    monkeypatch.setattr(loop, "choose", model.choose)
    monkeypatch.setattr(loop, "field_texts", blocked)
    monkeypatch.setattr(browser_module, "cdp", blocked)
    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    return requests, blocked


def test_denied_dismissal_excludes_only_it_then_other_dismissal_executes(monkeypatch):
    requests, blocked = policy_choices(monkeypatch)
    goal = "Dismiss information dialog"
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/closed"),),
                        prepared_plan={"objective": goal, "plan": []}, planner=blocked) as controller:
        first = controller.command()
        assert first["status"] == "ready" and controller.stale_failures == 0
        assert first["attempts"][0]["status"] == "rejected_by_policy"
        assert controller.feedback["reason"] == "policy_rejected"
        assert controller.feedback["target"] == "Dismiss sign in information"
        assert controller.browser.calls == []
        last = controller.command()
        assert last["status"] == "done" and last["stop_reason"] == "verified"
        assert controller.browser.calls == [("second", None)]
        assert last["attempts"][-1]["status"] == "executed"
        assert not any(e.get("reason") == "unstable_observation" for e in last["events"])
        assert "_forbidden_targets" not in requests[-1]["state"]["execution"]
        assert len(requests) == 2
    blocked.assert_not_called()


def test_every_forbidden_target_terminates_with_policy_exhausted_without_retries(monkeypatch):
    requests, blocked = policy_choices(monkeypatch)
    class AllForbidden(DialogBrowser):
        forbidden = frozenset({"Dismiss sign in information", "Dismiss dialog"})

    monkeypatch.setattr(loop, "Browser", AllForbidden)
    goal = "Dismiss information dialog"
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/closed"),),
                        prepared_plan={"objective": goal, "plan": []}, planner=blocked) as controller:
        result = list(controller.run())[-1]
        assert result["status"] == "abandoned" and result["stop_reason"] == "policy_exhausted"
        assert [a["status"] for a in result["attempts"]] == ["rejected_by_policy"] * 2
        assert controller.browser.calls == [] and controller.stale_failures == 0
        assert len(requests) == 2
    blocked.assert_not_called()


def test_denied_reserved_late_binding_stops_instead_of_rebinding(monkeypatch):
    class DenyLateFill(ChangingView):
        def act(self, action, page, text=None):
            if action["kind"] == "fill":
                raise PolicyRejected("Caller forbids this recipient")
            return super().act(action, page, text)

    browser = DenyLateFill("https://example.test/")
    _, helper = install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as controller:
        result = list(controller.run())[-1]
        assert result["status"] == "needs_attention" and result["stop_reason"] == "late_binding_rejected"
        assert browser.calls == [("e2", None)]
        assert result["attempts"][-1]["status"] == "rejected_by_policy"
        assert not controller.forbidden_targets
    helper.assert_not_called()


def test_semantic_change_reenables_exactly_previous_policy_denial(monkeypatch):
    requests, blocked = policy_choices(monkeypatch)
    goal = "Dismiss information dialog"
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/closed"),),
                        prepared_plan={"objective": goal, "plan": []}, planner=blocked) as controller:
        controller.command()
        controller.browser.page["semantic_marker"] = ["dialog", "new controls"]
        controller.browser.page["fingerprint"] = fingerprint(controller.browser.page)
        controller.command()
        assert controller.agent.state["attempts"][-1]["status"] == "rejected_by_policy"
        assert len(requests) == 2
    blocked.assert_not_called()
