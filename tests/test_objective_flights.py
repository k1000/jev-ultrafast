"""Complex flight outcome checks are test-owned; no live sites or paid APIs."""

import base64
import json
import subprocess
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast.browser import PolicyRejected
from scripts.objective_flights import FACTS, FlightsObjective, SearchOnlyBrowser, verify_flights


def flight_page():
    token = base64.urlsafe_b64encode(b"query includes 2027-03-20").decode().rstrip("=")
    return {"url": "https://www.google.com/travel/flights/search?tfs=" + token,
            "actions": [{"kind": "click", "label": k, "value": v} for k, v in {
                "Where from?": "Zürich", "Where to? ": "London", "Departure": "Sat, Mar 20",
                "Change ticket type. One way": "One way", "Change seating class. Economy": "Economy",
            }.items()],
            "flight_facts": {"adults": "1", "children": "0", "infants_seated": "0", "infants_lap": "0",
                             "flights": ["Select flight departing Saturday, March 20 from ZRH to LHR"]}}


def test_flights_passes_canonical_caller_checks_to_planner_without_running_browser(monkeypatch, tmp_path):
    from jev_ultrafast.planning import verify
    from scripts import objective_flights

    constructor = Mock(side_effect=RuntimeError("Offline constructor guard"))
    monkeypatch.setattr(objective_flights, "FlightsObjective", constructor)
    report = objective_flights.run(tmp_path / "offline.json")
    checks = constructor.call_args.kwargs["checks"]
    page = flight_page()
    for node, action in enumerate(page["actions"], 1):
        action["node"] = node
    assert all(verify(page, checks))
    assert any(c.label == "Where from?" and c.value == "Zürich" for c in checks)
    assert any(c.label == "Departure" and c.value == "Sat, Mar 20" for c in checks)
    assert report["passed"] is False and report["error"] == "RuntimeError"


def test_bootstrap_failure_preserves_safe_phase_and_retained_target_before_agent_exists(monkeypatch, tmp_path):
    from jev_ultrafast.browser import BrowserSetupError
    from scripts import objective_flights

    error = BrowserSetupError("attach", TimeoutError("Raw server detail must not persist"))
    error.cleanup_error, error.retained_target = "RuntimeError", "owned-tab"
    monkeypatch.setattr(objective_flights, "FlightsObjective", Mock(side_effect=error))
    report = objective_flights.run(tmp_path / "bootstrap.json")
    assert report["setup_error"] == {"phase": "attach", "cause_type": "TimeoutError",
                                     "cleanup_error": "RuntimeError", "retained_target": "owned-tab"}
    assert report["retained_tab"] == "owned-tab" and report["passed"] is False
    assert "Raw server detail" not in str(report)


def test_atomic_facts_accept_observed_price_prefixed_selection_labels_without_hidden_buttons():
    prefix = "Select flight departing Saturday, March 20 from ZRH to LHR"
    suffix = ("From 60 euros.This price does not include overhead bin access. Nonstop flight with easyJet. "
              "Leaves Zurich Airport at 1:20 PM on Saturday, March 20 and arrives at London Stansted Airport "
              "at 2:15 PM on Saturday, March 20. Total duration 1 hr 55 min. Select flight")
    nodes = [{"label": prefix}, {"label": suffix}, {"label": "Select flight", "visible": False},
             {"label": suffix, "top": 900}, {"label": "Flight details. Saturday, March 20"}]
    script = """const nodes=JSON.parse(require('fs').readFileSync(0,'utf8')).map(n=>({
      getAttribute:name=>name==='aria-label'?n.label:null,
      checkVisibility:()=>n.visible!==false,
      getBoundingClientRect:()=>({top:n.top??100,bottom:(n.top??100)+50,width:900})
    }));global.document={querySelectorAll:()=>nodes};global.innerHeight=780;
    process.stdout.write(JSON.stringify(""" + FACTS + "));"
    result = subprocess.run(["node", "-e", script], input=json.dumps(nodes), text=True,
                            capture_output=True, check=True, timeout=5)
    facts = json.loads(result.stdout)
    assert facts["flights"] == [prefix, suffix]
    page = flight_page()
    page["flight_facts"]["flights"] = facts["flights"]
    assert verify_flights(page)["passed"]


def test_full_flight_criteria_pass_only_when_all_independent_facts_match():
    result = verify_flights(flight_page())
    assert result["passed"] and all(result["checks"].values())


@pytest.mark.parametrize("field,value", [("adults", "2"), ("children", "1"),
                                         ("infants_seated", "1"), ("infants_lap", "1"), ("adults", None)])
def test_wrong_or_unavailable_passenger_counts_cannot_pass(field, value):
    page = flight_page()
    page["flight_facts"][field] = value
    assert not verify_flights(page)["passed"]


@pytest.mark.parametrize("mutation", ["wrong_year", "no_flights", "wrong_date", "wrong_route", "wrong_cabin"])
def test_search_url_or_done_signal_cannot_replace_full_flight_outcome(mutation):
    page = deepcopy(flight_page())
    if mutation == "wrong_year":
        page["url"] = "https://www.google.com/travel/flights/search?tfs=" + (
            base64.urlsafe_b64encode(b"2026-03-20").decode()
        )
    elif mutation == "no_flights":
        page["flight_facts"]["flights"] = []
    elif mutation == "wrong_date":
        page["flight_facts"]["flights"] = ["Select flight departing Saturday, April 3"]
    else:
        page["actions"][0 if mutation == "wrong_route" else -1]["value"] = "Wrong"
    assert not verify_flights(page)["passed"]


@pytest.mark.parametrize("full_match", [True, False])
def test_example_controller_completion_is_owned_by_full_verifier(monkeypatch, full_match):
    from test_objective import FakeBrowser

    from jev_ultrafast import Check
    from jev_ultrafast import agent as loop

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    with FlightsObjective("https://example.test/", "Inspect results",
                          checks=(Check("url_contains", "/travel/flights/search"),),
                          prepared_plan={"objective": "Inspect results", "plan": []}) as controller:
        page = flight_page()
        if not full_match:
            page["flight_facts"]["adults"] = "2"
        controller.verify_observation(page)
        assert controller.status == ("done" if full_match else "ready")
        assert controller.verification == [True]
        assert controller.additional_verified is full_match


@pytest.mark.parametrize("label", ["Select flight departing Saturday, March 20", "Sign in", "Track prices",
    "Track prices from Zürich to London departing 2027-03-20", "Track prices from Zürich to London - Any dates",
    "Reveal Track prices from Zürich to London departing 2027-03-20", "Sign in to continue"])
def test_receipt_interface_cannot_bypass_search_only_policy(label):
    browser = SearchOnlyBrowser.__new__(SearchOnlyBrowser)
    browser.fresh = Mock(side_effect=AssertionError("No input can be authorized"))
    browser.call = Mock(side_effect=AssertionError("No browser transport authorized"))
    action = {"id": "e1", "kind": "click", "node": 1, "label": label}
    receipt = browser.execute(action, {"actions": [action]})
    assert receipt["status"] == "rejected_by_policy" and receipt["phase"] == "validation"
    assert receipt["input_started"] is False and receipt["calls"] == []
    browser.fresh.assert_not_called()
    browser.call.assert_not_called()


def test_search_only_guard_rejects_flight_selection_before_any_input():
    browser = SearchOnlyBrowser.__new__(SearchOnlyBrowser)
    browser.fresh = Mock(side_effect=AssertionError("No input can be authorized"))
    browser.call = Mock(side_effect=AssertionError("No browser transport authorized"))
    action = {"id": "e1", "kind": "click", "node": 1, "label": "Select flight departing Saturday, March 20"}
    with pytest.raises(PolicyRejected) as caught:
        browser.act(action, {"actions": [action]})
    assert caught.value.receipt["status"] == "rejected_by_policy"
    assert caught.value.receipt["phase"] == "validation"
    assert caught.value.receipt["input_started"] is False and caught.value.receipt["calls"] == []
    browser.fresh.assert_not_called()
    browser.call.assert_not_called()
