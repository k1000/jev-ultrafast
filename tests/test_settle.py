"""Read-only outcome settling is independent of action acknowledgments and models."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import Check
from jev_ultrafast import browser as module

ORIGINAL_OPERATION = module.browser_operation


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def setup(monkeypatch, pages):
    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    browser = module.Browser.__new__(module.Browser)
    browser.session = "owned-session"
    browser.target = "owned-target"
    browser.execute = Mock(side_effect=AssertionError("Settling must not execute"))
    replies = iter(pages)
    observer = Mock(side_effect=lambda *_args, **_kwargs: deepcopy(next(replies)))
    monkeypatch.setattr(module, "browser_operation", observer)
    return browser, clock, observer


def page(text):
    return {"url": "https://example.test/", "title": "Fixture", "text": text,
            "actions": [], "scroll": {}, "ready_state": "complete"}


def test_delayed_outcome_is_verified_with_reads_only_even_when_document_is_complete(monkeypatch):
    browser, _, observer = setup(monkeypatch, [page("Loading"), page("Loading"), page("Ready")])
    result = browser.settle((Check("text", "Ready"),), timeout=1, interval=.1)
    assert result["status"] == "verified"
    assert result["reads"] == 3 and result["page"]["text"] == "Ready"
    assert result["evidence"][0]["state"] == "met"
    assert observer.call_count == 3
    browser.execute.assert_not_called()


def test_timeout_keeps_unknown_missing_control_evidence_without_claiming_success(monkeypatch):
    browser, clock, _ = setup(monkeypatch, [page("Ready")] * 3)
    result = browser.settle((Check("value", "Ready", label="Missing"),), timeout=.25, interval=.1)
    assert result["status"] == "timed_out" and result["reads"] == 3
    assert result["evidence"][0]["state"] == "unknown"
    assert clock.now == .25
    browser.execute.assert_not_called()


def test_additional_verifier_remains_authoritative_and_returned_evidence_is_defensive(monkeypatch):
    browser, _, _ = setup(monkeypatch, [page("Ready")] * 3)
    verifier = Mock(side_effect=[False, False, True])
    result = browser.settle((Check("text", "Ready"),), verifier=verifier, timeout=1)
    assert result["status"] == "verified" and result["reads"] == 3
    assert result["additional_verified"] is True
    result["page"]["text"] = "Tampered"
    result["evidence"][0]["state"] = "unknown"
    assert browser.settlements[-1]["page"]["text"] == "Ready"
    assert browser.settlements[-1]["evidence"][0]["state"] == "met"


def test_transient_read_timeout_recovers_and_response_timeout_uses_remaining_budget(monkeypatch):
    browser, clock, _ = setup(monkeypatch, [])
    monkeypatch.setattr(module, "browser_operation", ORIGINAL_OPERATION)
    timeouts = []

    def cdp(method, **params):
        assert method == "Runtime.evaluate"
        timeouts.append(params["_response_timeout"])
        if len(timeouts) == 1:
            clock.now += .2
            raise TimeoutError("Sensitive transport detail must not appear in result")
        return {"result": {"value": page("Ready")}}

    monkeypatch.setattr(module, "cdp", cdp)
    result = browser.settle((Check("text", "Ready"),), timeout=1, interval=.1)
    assert result["status"] == "verified" and result["reads"] == 2
    assert timeouts == [1, .7]
    assert result["read_errors"] == [{"read": 1, "error": "TimeoutError", "phase": "observation"}]
    browser.execute.assert_not_called()


def test_final_failed_read_cannot_reuse_prior_met_evidence(monkeypatch):
    browser, clock, observer = setup(monkeypatch, [])

    def read(*_args, **_kwargs):
        if clock.now == 0:
            return page("Ready")
        clock.now += .15
        raise TimeoutError("Sensitive transport details")

    observer.side_effect = read
    result = browser.settle((Check("text", "Ready"),), verifier=lambda _: False,
                            timeout=.25, interval=.1)
    assert result["status"] == "unavailable"
    assert result["page"] is None and result["evidence"] is None
    assert result["additional_verified"] is None
    assert result["last_observed_page"]["text"] == "Ready"
    assert result["read_errors"] == [{"read": 2, "error": "TimeoutError", "phase": "observation"}]
    assert "Sensitive" not in repr(result)


def test_observed_success_does_not_unlock_unknown_execution(monkeypatch):
    browser, _, _ = setup(monkeypatch, [page("Ready")])
    receipt = {"request_id": "original", "status": "outcome_unknown", "input_started": True}
    browser._uncertain_receipt = deepcopy(receipt)
    assert browser.settle((Check("text", "Ready"),))["status"] == "verified"
    later = module.Browser.execute(browser, {}, {})
    assert later == receipt and browser._uncertain_receipt == receipt
    later["status"] = "executed"
    assert browser._uncertain_receipt["status"] == "outcome_unknown"


@pytest.mark.parametrize("field,value", [("timeout", 0), ("timeout", -1), ("timeout", True),
    ("timeout", float("inf")), ("timeout", float("nan")), ("interval", 0), ("interval", "1")])
def test_invalid_limits_are_rejected_before_reading(monkeypatch, field, value):
    browser, _, observer = setup(monkeypatch, [])
    with pytest.raises(ValueError):
        browser.settle((Check("text", "Ready"),), **{field: value})
    observer.assert_not_called()


def test_empty_criteria_or_noncallable_verifier_are_rejected_before_reading(monkeypatch):
    browser, _, observer = setup(monkeypatch, [])
    for checks, verifier in (((), None), (("Ready",), None), ((Check("text", "Ready"),), {})):
        with pytest.raises(ValueError):
            browser.settle(checks, verifier=verifier)
    observer.assert_not_called()


def test_truthy_nonboolean_verifier_is_not_success(monkeypatch):
    browser, _, _ = setup(monkeypatch, [page("Ready")])
    result = browser.settle((Check("text", "Ready"),), verifier=lambda _: {"passed": False})
    assert result["status"] == "unavailable" and result["additional_verified"] is None
    assert result["read_errors"] == [{"read": 1, "error": "ValueError", "phase": "extra_verification"}]


def test_late_observation_cannot_claim_success_within_expired_budget(monkeypatch):
    browser, clock, observer = setup(monkeypatch, [])

    def late(*_args, **_kwargs):
        clock.now += 2
        return page("Ready")

    observer.side_effect = late
    result = browser.settle((Check("text", "Ready"),), timeout=1)
    assert result["status"] == "timed_out" and result["evidence"][0]["state"] == "met"


def test_observation_caps_post_input_wait_and_snapshot_with_one_shared_budget(monkeypatch):
    browser, clock, _ = setup(monkeypatch, [])
    monkeypatch.setattr(module, "browser_operation", ORIGINAL_OPERATION)
    browser.after_input = {"node": 1, "kind": "click"}
    limits = []

    def cdp(method, **params):
        assert method == "Runtime.evaluate"
        limits.append(params["_response_timeout"])
        clock.now += .1
        return {} if len(limits) == 1 else {"result": {"value": page("Ready")}}

    monkeypatch.setattr(module, "cdp", cdp)
    assert browser.observe(screenshot=False, response_timeout=.3)["text"] == "Ready"
    assert limits == pytest.approx([.3, .2])


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_settling_preserves_safe_evidence_and_propagates(monkeypatch, interrupt):
    browser, _, observer = setup(monkeypatch, [])
    observer.side_effect = interrupt("Sensitive interruption details")
    with pytest.raises(interrupt):
        browser.settle((Check("text", "Ready"),))
    record = browser.settlements[-1]
    assert record["status"] == "unavailable" and record["page"] is None
    assert record["read_errors"] == [{"read": 1, "error": interrupt.__name__, "phase": "observation"}]
    assert "Sensitive" not in repr(record)
    browser.execute.assert_not_called()
