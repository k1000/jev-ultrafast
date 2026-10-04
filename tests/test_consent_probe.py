"""The live consent probe must not convert a consent goal into arbitrary input."""

import sys
from unittest.mock import Mock

import pytest

from jev_ultrafast.browser import PolicyRejected
from scripts import consent_probe
from scripts.consent_probe import (
    ConsentOnlyBrowser,
    UnindexedConsentFrame,
    consent_verified,
    preflight,
    run,
    safe_reject,
)
from scripts.live_safety import CookieSafeBrowser


def observed(label="Reject", *, role="button", kind="click", modal="Cookie preferences"):
    action = {"id": "e1", "node": 1, "kind": kind, "role": role, "label": label}
    return action, {"url": "https://example.test/", "modal_open": True,
                    "modal_label": modal, "actions": [action]}


@pytest.mark.parametrize("label", ["Reject", "Reject all", "Reject Non-Essential", "Decline optional cookies",
                                   "Accetta i cookie essenziali",
                                   "Only necessary", "Essential only", "Do not consent"])
def test_only_explicit_privacy_preserving_choices_are_recognized(label):
    assert safe_reject(label)


@pytest.mark.parametrize("label", ["Accept", "Allow all", "Accetta tutto", "Accetta i cookie",
                                   "Yes, I agree", "Cookie settings",
                                   "Reject application", "Reject and continue", "Dismiss sign in"])
def test_accept_or_coordinated_choices_are_not_reject(label):
    assert not safe_reject(label)


def test_plural_cookies_dialog_still_allows_the_observed_reject():
    browser = ConsentOnlyBrowser.__new__(ConsentOnlyBrowser)
    browser.receipts = []
    action, page = observed(modal="Hej! You are in control of your cookies.")
    browser.validate_action(action, page)
    assert browser.permitted_action_ids == {"e1"}


def test_policy_accepts_one_observed_reject_on_cookie_dialog_and_never_repeats():
    browser = ConsentOnlyBrowser.__new__(ConsentOnlyBrowser)
    browser.receipts = []
    action, page = observed()
    browser.validate_action(action, page)
    assert browser.permitted_action_ids == {"e1"}
    browser.receipts.append({"status": "executed", "action_id": "e1", "input_started": True})
    with pytest.raises(PolicyRejected):
        browser.validate_action(action, page)


@pytest.mark.parametrize("change", [dict(label="Accept"), dict(label="Cookie settings"),
                                    dict(role="link"), dict(kind="press_key"),
                                    dict(modal="Sign in"), dict(modal=None)])
def test_policy_rejects_unwanted_or_nonconsent_browser_input_before_freshness(change):
    browser = ConsentOnlyBrowser.__new__(ConsentOnlyBrowser)
    browser.receipts = []
    action, page = observed()
    change = dict(change)
    if "modal" in change:
        page["modal_label"] = change.pop("modal")
        page["modal_open"] = page["modal_label"] is not None
    action.update(change)
    if action["kind"] == "press_key":
        action["key"] = "Escape"
    browser.fresh = Mock(side_effect=AssertionError("Do not reach mutation freshness"))
    with pytest.raises(PolicyRejected):
        browser.validate_action(action, page)
    browser.fresh.assert_not_called()


@pytest.mark.parametrize("label", ["Accept", "Accept all", "Yes, I agree", "Sign in",
                                   "Log in", "Book now", "Buy now", "Checkout", "Pay"])
def test_public_page_policy_vetoes_unsafe_clicks_even_without_modal(label):
    browser = CookieSafeBrowser.__new__(CookieSafeBrowser)
    action, page = observed(label=label)
    page["modal_open"] = False
    page["modal_label"] = ""
    with pytest.raises(PolicyRejected):
        browser.validate_action(action, page)


def test_public_page_policy_only_permits_explicit_reject_in_consent_modal():
    browser = CookieSafeBrowser.__new__(CookieSafeBrowser)
    action, page = observed(label="Cookie settings", modal="Privacy Notice")
    with pytest.raises(PolicyRejected):
        browser.validate_action(action, page)
    action["label"] = "Reject Non-Essential"
    browser.validate_action(action, page)


def test_preflight_never_forwards_redirect_query_tokens_to_the_planner(monkeypatch):
    action, page = observed(label="Accetta i cookie essenziali", modal="La tua privacy è importante")
    page["title"] = "Skyscanner"
    page["url"] = "https://example.test/?session_handoff=private-token"
    browser = Mock()
    browser.observe.return_value = page
    monkeypatch.setattr("scripts.consent_probe.browser_module.Browser", Mock(return_value=browser))
    assert preflight("https://example.test/", "Skyscanner") is None
    browser.close.assert_called_once()
    browser.observe.assert_called_once()


def test_unindexed_consent_frame_stops_before_planning_or_input(monkeypatch):
    _, page = observed(modal="SP Consent Message")
    page.update(title="The Guardian", actions=[{"id": "wait", "kind": "wait", "label": "Wait"}],
                unindexed_modal_frames=1)
    browser = Mock()
    browser.observe.return_value = page
    monkeypatch.setattr("scripts.consent_probe.browser_module.Browser", Mock(return_value=browser))
    monkeypatch.setattr("scripts.consent_probe.time.sleep", Mock())
    planner = Mock(side_effect=AssertionError("No paid planner or browser input"))
    monkeypatch.setattr(consent_probe, "ObjectiveAgent", planner)
    with pytest.raises(UnindexedConsentFrame):
        preflight("https://example.test/", "The Guardian")
    assert browser.observe.call_count == 4  # A late top-level Reject is still allowed to appear.
    result = run("https://example.test/", "The Guardian")
    assert result["reason"] == "unindexed_consent_frame"
    assert result["planner_calls"] == result["browser_inputs"] == 0
    assert not result["passed"]
    planner.assert_not_called()
    assert browser.close.call_count == 2


def test_observed_reject_remains_eligible_when_a_modal_also_contains_an_unindexed_frame(monkeypatch):
    _, page = observed(modal="Consent message")
    page.update(title="Example", unindexed_modal_frames=1)
    browser = Mock()
    browser.observe.return_value = page
    monkeypatch.setattr("scripts.consent_probe.browser_module.Browser", Mock(return_value=browser))
    assert preflight("https://example.test/", "Example") == page["url"]
    browser.close.assert_called_once()


def test_unique_observed_frame_reject_passes_read_only_preflight(monkeypatch):
    action, page = observed(modal="Local cookie consent")
    action.update(id="frame-1:e1", node=-1, frame_id="frame-1", frame_node=1)
    page.update(title="Local consent fixture", unindexed_modal_frames=0)
    browser = Mock()
    browser.observe.return_value = page
    monkeypatch.setattr("scripts.consent_probe.browser_module.Browser", Mock(return_value=browser))
    assert preflight("https://example.test/", "Local consent fixture") == page["url"]
    assert browser.frame_clicks_enabled is True
    browser.close.assert_called_once()
    browser.execute.assert_not_called()


def test_consent_policy_keeps_frame_accept_all_blocked():
    browser = ConsentOnlyBrowser.__new__(ConsentOnlyBrowser)
    browser.receipts = []
    action, page = observed(modal="Local cookie consent")
    action.update(id="frame-1:e1", node=-1, frame_id="frame-1", frame_node=1)
    browser.validate_action(action, page)
    assert browser.permitted_action_ids == {action["id"]}
    action["label"] = "Accept all"
    with pytest.raises(PolicyRejected):
        browser.validate_action(action, page)


def test_preflight_waits_for_a_late_observed_reject_outside_the_frame(monkeypatch):
    _, framed = observed(modal="Consent message")
    framed.update(title="Example", actions=[], unindexed_modal_frames=1)
    _, ready = observed(modal="Consent message")
    ready.update(title="Example", unindexed_modal_frames=1)
    browser = Mock()
    browser.observe.side_effect = [framed, ready]
    monkeypatch.setattr("scripts.consent_probe.browser_module.Browser", Mock(return_value=browser))
    monkeypatch.setattr("scripts.consent_probe.time.sleep", Mock())
    assert preflight("https://example.test/", "Example") == ready["url"]
    assert browser.observe.call_count == 2
    browser.close.assert_called_once()


def test_runner_private_evidence_permissions_without_paid_call(monkeypatch, tmp_path):
    output = tmp_path / "Library/Application Support/JevDiagnostics" / "offline.json"
    monkeypatch.setattr(consent_probe.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["consent_probe", "--url", "https://example.test/",
                                      "--title-fragment", "Example", "--output", str(output)])
    for key in ("PLANNER_API_KEY", "PLANNER_BASE_URL", "PLANNER_MODEL", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(key, "offline-only")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    run = Mock(return_value={"site": "example.test", "passed": False, "reason": "unsupported"})
    monkeypatch.setattr(consent_probe, "run", run)
    assert consent_probe.main() == 1
    run.assert_called_once()
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert output.stat().st_mode & 0o777 == 0o600


def test_consent_outcome_needs_acknowledged_reject_and_freshly_absent_dialog():
    action, page = observed()
    assert not consent_verified(page, [], {action["id"]}, page["url"])
    page["modal_open"] = False
    assert not consent_verified(page, [], {action["id"]}, page["url"])
    receipt = {"action_id": action["id"], "status": "executed", "input_started": True}
    assert consent_verified(page, [receipt], {action["id"]}, page["url"])
    assert not consent_verified({**page, "url": "https://example.test/other"},
                                [receipt], {action["id"]}, page["url"])
    assert not consent_verified(page, [receipt, receipt], {action["id"]}, page["url"])
    assert not consent_verified(page, [{**receipt, "status": "outcome_unknown"}],
                                {action["id"]}, page["url"])
