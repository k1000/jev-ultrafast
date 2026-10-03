"""Terminal outcome waits use real settling with offline browser/model boundaries."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_objective import FakeBrowser, chosen

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast import objective as module


class Clock:
    now = 0.0

    def read(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def boundaries(monkeypatch, choices):
    clock = Clock()
    monkeypatch.setattr(module.time, "perf_counter", clock.read)
    monkeypatch.setattr(module.time, "monotonic", clock.read)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)

    class DelayedBrowser(FakeBrowser):
        settle = browser_module.Browser.settle
        ready_at = .3

        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))

        def observe(self, screenshot=False, *, response_timeout=None):
            if self.calls and clock.now >= self.ready_at:
                self.page.update(url="https://example.test/article", title="Article", text="Article")
                self.page["fingerprint"] = browser_module.fingerprint(self.page)
            return deepcopy(self.page)

    monkeypatch.setattr(loop, "Browser", DelayedBrowser)
    chooser = Mock(side_effect=choices)
    monkeypatch.setattr(loop, "choose", chooser)
    planner = Mock(side_effect=AssertionError("Settling must not replan"))
    helper = Mock(side_effect=AssertionError("Settling must not invoke text helper"))
    monkeypatch.setattr(loop, "field_texts", helper)
    return clock, DelayedBrowser, chooser, planner


def controller(planner, **limits):
    return ObjectiveAgent("https://example.test/", "Open the article",
                          checks=(Check("url", "https://example.test/article"),),
                          prepared_plan={"objective": "Open the article", "plan": []},
                          planner=planner, **limits)


def test_objective_done_waits_for_delayed_fresh_outcome_without_more_models_or_actions(monkeypatch):
    _, _, chooser, planner = boundaries(monkeypatch, [chosen("e2", "CLICK"), chosen("DONE", "DONE")])
    with controller(planner) as agent:
        assert agent.command()["status"] == "ready"
        final = agent.command()
        assert final["status"] == "done" and final["verification"] == [True]
        assert final["corrections_used"] == 0 and final["replans_used"] == 0
        assert agent.browser.calls == [("e2", None)]
        assert chooser.call_count == 2 and final["text_calls"] == []
        event = next(e for e in final["events"] if e["kind"] == "outcome_settled")
        assert event["status"] == "verified" and event["reads"] > 1
    planner.assert_not_called()


def test_generic_verifier_adds_delayed_criteria_without_replacing_caller_checks(monkeypatch):
    _, _, chooser, planner = boundaries(monkeypatch, [chosen("e2", "CLICK"), chosen("DONE", "DONE")])
    seen = []

    def extra(page):
        seen.append(page["title"])
        return page["title"] == "Article"

    with ObjectiveAgent("https://example.test/", "Open the article",
                        checks=(Check("url_contains", "example.test"),),
                        verifier=extra, prepared_plan={"objective": "Open the article", "plan": []},
                        planner=planner) as agent:
        first = agent.command()
        assert first["verification"] == [True] and first["additional_verified"] is False
        final = agent.command()
        assert final["status"] == "done" and final["additional_verified"] is True
        assert final["verification"] == [True] and len(final["check_evidence"]) == 1
        assert agent.browser.calls == [("e2", None)] and chooser.call_count == 2
        assert seen[0] == "Search" and seen[-1] == "Article"
    planner.assert_not_called()


def test_generic_verifier_cannot_weaken_an_unmet_caller_check(monkeypatch):
    _, _, _, planner = boundaries(monkeypatch, [])
    with controller(planner, verifier=lambda page: True) as agent:
        agent.observe()
        state = agent.snapshot()
        assert state["status"] == "ready" and state["verification"] == [False]
        assert state["additional_verified"] is True
    planner.assert_not_called()


@pytest.mark.parametrize("verifier", [False, 1, "callback"])
def test_generic_verifier_configuration_fails_before_browser_creation(monkeypatch, verifier):
    constructor = Mock(side_effect=AssertionError("No browser for invalid verifier"))
    monkeypatch.setattr(loop, "Browser", constructor)
    with pytest.raises(ValueError, match="verifier"):
        controller(Mock(), verifier=verifier)
    constructor.assert_not_called()


@pytest.mark.parametrize("result", [None, 1, {"passed": True}])
def test_generic_verifier_requires_strict_boolean_evidence(monkeypatch, result):
    _, _, _, planner = boundaries(monkeypatch, [])
    with controller(planner, verifier=lambda page: result) as agent:
        with pytest.raises(ValueError, match="boolean"):
            agent.observe()
        assert agent.status != "done" and agent.additional_verified is None
    planner.assert_not_called()


def test_verifier_receives_a_copy_and_cannot_change_caller_evidence(monkeypatch):
    _, _, _, planner = boundaries(monkeypatch, [])

    def extra(page):
        page["url"] = "https://example.test/article"
        page["actions"].clear()
        return True

    with controller(planner, verifier=extra) as agent:
        page = agent.observe()
        assert page["url"] == "https://example.test/" and page["actions"]
        assert agent.verification == [False] and agent.status == "ready"
    planner.assert_not_called()


def test_failed_fresh_verifier_cannot_leave_prior_completion_current(monkeypatch):
    _, _, _, planner = boundaries(monkeypatch, [])
    with controller(planner, verifier=lambda page: True) as agent:
        agent.browser.page["url"] = "https://example.test/article"
        agent.observe()
        assert agent.status == "done"
        agent.verifier = lambda page: None
        with pytest.raises(ValueError):
            agent.observe()
        assert agent.status != "done" and agent.snapshot()["additional_verified"] is None
    planner.assert_not_called()


def test_only_one_window_per_mutation_generation_even_across_more_done_signals(monkeypatch):
    clock, browser_type, _, planner = boundaries(monkeypatch, [chosen("DONE", "DONE"),
        chosen("DONE", "DONE"), chosen("e2", "CLICK"), chosen("DONE", "DONE")])
    browser_type.ready_at = 100
    with controller(planner, settle_timeout=.2, local_attempts=10) as agent:
        for _ in range(4):
            assert agent.command()["status"] == "ready"
        windows = [e for e in agent.events if e["kind"] == "outcome_settling"]
        assert [e["generation"] for e in windows] == [0, 1]
        assert clock.now == pytest.approx(.4)
        assert agent.browser.calls == [("e2", None)]
    planner.assert_not_called()


def test_wait_actions_do_not_reset_the_settling_window(monkeypatch):
    clock, browser_type, _, planner = boundaries(monkeypatch, [chosen("DONE", "DONE"),
        chosen("w", "WAIT"), chosen("DONE", "DONE")])
    browser_type.ready_at = 100
    with controller(planner, settle_timeout=.2, local_attempts=10) as agent:
        agent.browser.page["actions"].append({"id": "w", "kind": "wait", "label": "Wait", "node": 3})
        agent.browser.page["fingerprint"] = browser_module.fingerprint(agent.browser.page)
        for _ in range(3):
            assert agent.command()["status"] == "ready"
        assert len([e for e in agent.events if e["kind"] == "outcome_settling"]) == 1
        assert clock.now == pytest.approx(.2)
    planner.assert_not_called()


def test_timeout_is_capped_by_remaining_global_time_budget(monkeypatch):
    clock, _, chooser, planner = boundaries(monkeypatch, [chosen("DONE", "DONE")])
    with controller(planner, max_seconds=1) as agent:
        clock.now = .8
        final = agent.command()
        assert final["status"] == "abandoned" and final["stop_reason"] == "time_budget"
        start = next(e for e in final["events"] if e["kind"] == "outcome_settling")
        assert start["timeout"] == pytest.approx(.2) and clock.now == pytest.approx(1)
        assert chooser.call_count == 1 and agent.browser.calls == []
    planner.assert_not_called()


def test_unavailable_settling_does_not_reuse_prior_evidence_or_replan(monkeypatch):
    clock, browser_type, chooser, planner = boundaries(monkeypatch, [chosen("DONE", "DONE")])
    original = browser_type.observe

    def failure(self, *args, **kwargs):
        if clock.now > 0:
            raise RuntimeError("Sensitive failed-read details")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(browser_type, "observe", failure)
    with controller(planner) as agent:
        final = agent.command()
        assert final["status"] == "needs_attention" and final["stop_reason"] == "observation_unavailable"
        assert final["verification"] is None and final["check_evidence"] is None
        assert chooser.call_count == 1 and agent.browser.calls == []
        assert "Sensitive" not in repr(final["events"])
        browser = agent.browser
    assert browser.closed is False
    planner.assert_not_called()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_settling_retains_tab_and_safe_history(monkeypatch, interrupt):
    clock, browser_type, _, planner = boundaries(monkeypatch, [chosen("DONE", "DONE")])
    original = browser_type.observe

    def interrupted(self, *args, **kwargs):
        if clock.now > 0:
            raise interrupt("Sensitive interruption details")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(browser_type, "observe", interrupted)
    with pytest.raises(interrupt):
        with controller(planner) as agent:
            agent.command()
    assert agent.status == "needs_attention" and agent.stop_reason == "interrupted"
    assert agent.browser.closed is False and agent.verification is None
    assert agent.browser.settlements[-1]["read_errors"][-1]["error"] == interrupt.__name__
    assert "Sensitive" not in repr(agent.events)
    planner.assert_not_called()


@pytest.mark.parametrize("bad_fact", [None, "wrong_year", "wrong_passengers", "wrong_results"])
def test_flights_settling_uses_full_verifier_not_only_met_base_checks(monkeypatch, bad_fact):
    from test_objective_flights import flight_page

    from scripts.objective_flights import FlightsObjective

    clock, _, chooser, planner = boundaries(monkeypatch, [chosen("DONE", "DONE")])

    class FlightBrowser(FakeBrowser):
        settle = browser_module.Browser.settle

        def __init__(self, url):
            super().__init__(url)
            self.page = flight_page()
            self.page.update(title="Flights", text="Loading", scroll={}, ready_state="complete")
            for node, action in enumerate(self.page["actions"], 1):
                action.update(node=node, id=f"e{node}", role="button")
            if bad_fact == "wrong_year":
                import base64
                self.page["url"] = "https://www.google.com/travel/flights/search?tfs=" + (
                    base64.urlsafe_b64encode(b"2026-03-20").decode())
            elif bad_fact == "wrong_passengers":
                self.page["flight_facts"]["adults"] = "2"
            elif bad_fact == "wrong_results":
                self.page["flight_facts"]["flights"] = ["Select flight departing Saturday, April 3"]
            self.rows = self.page["flight_facts"]["flights"]
            self.page["flight_facts"]["flights"] = []
            self.page["fingerprint"] = browser_module.fingerprint(self.page)

        def observe(self, screenshot=False, *, response_timeout=None):
            if clock.now >= .3:
                self.page["flight_facts"]["flights"] = self.rows
            return deepcopy(self.page)

    monkeypatch.setattr(loop, "Browser", FlightBrowser)
    with FlightsObjective("https://example.test/", "Open the article",
                          checks=(Check("url_contains", "/travel/flights/search"),),
                          prepared_plan={"objective": "Open the article", "plan": []},
                          planner=planner, settle_timeout=.5, local_attempts=0, max_replans=0) as agent:
        final = agent.command()
        if bad_fact is None:
            assert final["status"] == "done" and all(final["verification"])
            assert clock.now >= .3
        else:
            assert final["status"] == "abandoned" and final["additional_verified"] is False
            assert final["stop_reason"] == "replanning_exhausted"
            assert agent.browser.settlements[-1]["status"] == "timed_out"
        assert agent.browser.settlements[-1]["reads"] > 1
        assert final["verification"] == [True]
        assert final["additional_verified"] is (bad_fact is None)
        assert chooser.call_count == 1 and agent.browser.calls == []
    planner.assert_not_called()


def test_expired_budget_cannot_turn_late_verifier_success_into_done(monkeypatch):
    clock, _, chooser, planner = boundaries(monkeypatch, [chosen("e2", "CLICK"), chosen("DONE", "DONE")])

    class SlowVerifier(ObjectiveAgent):
        def verify_observation(self, page):
            verified = super().verify_observation(page)
            if verified:
                clock.now += 2
            return verified

    with SlowVerifier("https://example.test/", "Open the article",
                      checks=(Check("url", "https://example.test/article"),),
                      prepared_plan={"objective": "Open the article", "plan": []},
                      planner=planner, max_seconds=1) as agent:
        agent.command()
        final = agent.command()
        assert final["status"] == "abandoned" and final["stop_reason"] == "time_budget"
        assert agent.browser.settlements[-1]["status"] == "timed_out"
        assert chooser.call_count == 2 and agent.browser.calls == [("e2", None)]
    planner.assert_not_called()


def test_advisory_milestone_done_defers_without_outcome_wait(monkeypatch):
    _, _, _, planner = boundaries(monkeypatch, [chosen("DONE", "DONE")])
    plan = {"objective": "Open the article", "plan": [{"goal": "Confirm a bad intermediate check",
            "texts": [], "checks": [{"kind": "text", "value": "Invented"}]}]}
    with ObjectiveAgent("https://example.test/", "Open the article",
                        checks=(Check("url", "https://example.test/article"),),
                        prepared_plan=plan, planner=planner) as agent:
        agent.browser.settle = Mock(side_effect=AssertionError("Milestones are advisory, not completion gates"))
        assert agent.command()["status"] == "ready"
        assert agent.snapshot()["deferred_steps"] == [0]
        agent.browser.settle.assert_not_called()
    planner.assert_not_called()


def test_legacy_observe_override_cannot_silently_bypass_full_settling_verification(monkeypatch):
    class LegacyVerifier(ObjectiveAgent):
        def observe(self):
            page = super().observe()
            self.status = "ready"
            return page

    constructor = Mock(side_effect=AssertionError("Reject unsafe hook before creating a browser"))
    monkeypatch.setattr(loop, "Browser", constructor)
    with pytest.raises(ValueError, match="verify_observation"):
        LegacyVerifier("https://example.test/", "Open the article",
                       checks=(Check("url", "https://example.test/article"),))
    constructor.assert_not_called()


@pytest.mark.parametrize("timeout", [-1, True, float("inf"), float("nan"), "5"])
def test_invalid_settling_limit_is_rejected_before_browser_creation(monkeypatch, timeout):
    constructor = Mock(side_effect=AssertionError("Invalid limits must fail before browser setup"))
    monkeypatch.setattr(loop, "Browser", constructor)
    with pytest.raises(ValueError):
        ObjectiveAgent("https://example.test/", "Open the article",
                       checks=(Check("url", "https://example.test/article"),), settle_timeout=timeout)
    constructor.assert_not_called()


def test_verified_observation_cannot_make_context_exit_close_an_uncertain_tab(monkeypatch):
    _, _, _, planner = boundaries(monkeypatch, [])
    with controller(planner) as agent:
        agent.browser._uncertain_receipt = {"status": "outcome_unknown"}
        agent.browser.page["url"] = "https://example.test/article"
        agent.observe()
        assert agent.status == "done"
    assert agent.browser.closed is False
    assert agent.browser._uncertain_receipt["status"] == "outcome_unknown"
    planner.assert_not_called()


def test_disabled_settling_preserves_bounded_recovery(monkeypatch):
    clock, _, chooser, planner = boundaries(monkeypatch, [chosen("DONE", "DONE")])
    with controller(planner, settle_timeout=0, local_attempts=0, max_replans=0) as agent:
        agent.browser.settle = Mock(side_effect=AssertionError("Settling disabled"))
        final = agent.command()
        assert final["status"] == "abandoned" and final["stop_reason"] == "replanning_exhausted"
        assert clock.now == 0 and chooser.call_count == 1
        agent.browser.settle.assert_not_called()
    planner.assert_not_called()
