"""Closed caller-owned policy data and existing runtime composition; offline only."""

import json
import sys
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest
from test_objective import FakeBrowser, chosen, search_plan

from examples import objective as cli
from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast import objective as controller
from jev_ultrafast.planning import parse_plan
from jev_ultrafast.policy import parse_policy

GOAL = "Enter Ada Lovelace in Search"


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch):
    monkeypatch.setattr("httpx.Client.send", Mock(side_effect=AssertionError("No model transport in tests")))
    monkeypatch.setattr("jev_ultrafast.browser.cdp", Mock(side_effect=AssertionError("No browser transport in tests")))


def policy_data():
    return {"version": 1, "objective": GOAL,
            "checks": [{"kind": "value", "value": "Ada Lovelace", "label": "Search"}],
            "prepared_plan": search_plan()}


def test_runtime_cli_policy_reuses_controller_with_prepared_text_and_no_planner(monkeypatch, tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_data()))
    browser = FakeBrowser("https://example.test/")
    monkeypatch.setattr(loop, "Browser", lambda url: browser)
    chooser = Mock(return_value=chosen("e1"))
    helper = Mock(side_effect=AssertionError("No text helper"))
    planner = Mock(side_effect=AssertionError("No planner for a prepared policy"))
    monkeypatch.setattr(loop, "choose", chooser)
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(controller, "plan_objective", planner)
    factory = Mock(wraps=ObjectiveAgent)
    monkeypatch.setattr(cli, "ObjectiveAgent", factory)
    monkeypatch.setattr(sys, "argv", ["objective", "--url", "https://example.test/", "--policy", str(path)])
    assert cli.main() == 0
    assert browser.calls == [("e1", "Ada Lovelace")]
    assert factory.call_args.kwargs["max_replans"] == 0
    assert factory.call_args.kwargs["checks"] == (Check("value", "Ada Lovelace", label="Search"),)
    chooser.assert_called_once()
    helper.assert_not_called()
    planner.assert_not_called()


@pytest.mark.parametrize("contents", [
    "{", "null", "[]", json.dumps(policy_data() | {"version": True}),
    json.dumps(policy_data() | {"checks": []}),
    json.dumps(policy_data() | {"prepared_plan": {"objective": "changed", "plan": []}}),
    json.dumps(policy_data() | {"code": "DO_NOT_ECHO_THIS_VALUE"}),
])
def test_runtime_cli_invalid_policy_fails_before_controller_or_browser(monkeypatch, tmp_path, capsys, contents):
    path = tmp_path / "policy.json"
    path.write_text(contents)
    factory, browser, chooser, planner = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(cli, "ObjectiveAgent", factory)
    monkeypatch.setattr(loop, "Browser", browser)
    monkeypatch.setattr(loop, "choose", chooser)
    monkeypatch.setattr(controller, "plan_objective", planner)
    monkeypatch.setattr(sys, "argv", ["objective", "--url", "https://example.test/", "--policy", str(path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "Invalid policy file" in output and "DO_NOT_ECHO_THIS_VALUE" not in output
    for boundary in (factory, browser, chooser, planner):
        boundary.assert_not_called()


@pytest.mark.parametrize("flag, value", [
    ("--goal", ""), ("--checks", "missing.json"), ("--prepared-plan", "missing.json"),
    ("--max-replans", "0"), ("--max-replans", "1"),
])
def test_runtime_cli_rejects_mixed_policy_and_legacy_flags_before_reading(monkeypatch, capsys, flag, value):
    factory = Mock()
    monkeypatch.setattr(cli, "ObjectiveAgent", factory)
    monkeypatch.setattr(sys, "argv", ["objective", "--url", "https://example.test/", "--policy", "missing.json",
                                     flag, value])
    with pytest.raises(SystemExit):
        cli.main()
    assert "cannot be combined" in capsys.readouterr().err
    factory.assert_not_called()


@pytest.mark.parametrize("unreadable", ["missing", "invalid_utf8"])
def test_runtime_cli_unreadable_policy_fails_before_setup(monkeypatch, tmp_path, capsys, unreadable):
    path = tmp_path / "policy.json"
    if unreadable == "invalid_utf8":
        path.write_bytes(b"\xff")
    factory = Mock()
    monkeypatch.setattr(cli, "ObjectiveAgent", factory)
    monkeypatch.setattr(sys, "argv", ["objective", "--url", "https://example.test/", "--policy", str(path)])
    with pytest.raises(SystemExit):
        cli.main()
    assert "Invalid policy file" in capsys.readouterr().err
    factory.assert_not_called()


def test_runtime_null_plan_allows_one_initial_plan_but_no_runtime_replanning(monkeypatch):
    data = policy_data()
    data["prepared_plan"] = None
    policy = parse_policy(data)
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("BLOCKED", "BLOCKED")))
    planner = Mock(return_value=(search_plan(), {}))
    with ObjectiveAgent("https://example.test/", **policy.agent_kwargs(), planner=planner) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "abandoned" and final["verification"] == [False]
        assert final["replans_used"] == 0 and len(final["planner_calls"]) == 1
        assert final["planner_calls"][0]["phase"] == "initial" and agent.browser.calls == []
        assert agent.checks == policy.checks
    planner.assert_called_once()


@pytest.mark.parametrize("bad_plan", [
    {"objective": "changed", "plan": []},
    {"objective": GOAL, "plan": [], "final_checks": [{"kind": "text", "value": "Search"}]},
    policy_data(),
])
def test_runtime_planner_cannot_replace_the_caller_policy_contract(monkeypatch, bad_plan):
    data = policy_data()
    data["prepared_plan"] = None
    policy = parse_policy(data)
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    chooser = Mock()
    monkeypatch.setattr(loop, "choose", chooser)
    planner = Mock(return_value=(bad_plan, {}))
    with ObjectiveAgent("https://example.test/", **policy.agent_kwargs(), planner=planner) as agent:
        result = agent.command()
        assert result["status"] == "needs_attention" and result["stop_reason"] == "planning_failed"
        assert result["verification"] == [False] and result["goal"] == GOAL
        assert agent.checks == policy.checks and agent.browser.calls == []
    planner.assert_called_once()
    chooser.assert_not_called()


def test_runtime_missing_prepared_text_never_generates_or_replans(monkeypatch):
    data = policy_data()
    data["prepared_plan"]["plan"][0]["texts"] = []
    policy = parse_policy(data)
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    helper, planner = Mock(), Mock()
    monkeypatch.setattr(loop, "field_texts", helper)
    with ObjectiveAgent("https://example.test/", **policy.agent_kwargs(), planner=planner) as agent:
        final = list(agent.run())[-1]
        assert final["status"] == "abandoned" and final["verification"] == [False]
        assert final["replans_used"] == 0 and final["planner_calls"] == final["text_calls"] == []
        assert agent.browser.calls == []
    helper.assert_not_called()
    planner.assert_not_called()


def test_runtime_done_and_met_milestone_cannot_override_final_caller_checks(monkeypatch):
    data = policy_data()
    data["prepared_plan"]["plan"][0]["checks"] = [{"kind": "title", "value": "Search"}]
    policy = parse_policy(data)
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("DONE", "DONE")))
    with ObjectiveAgent("https://example.test/", **policy.agent_kwargs(), settle_timeout=0) as agent:
        final = list(agent.run())[-1]
        assert final["verified_steps"] == [0]
        assert final["status"] == "abandoned" and final["verification"] == [False]
        assert agent.browser.calls == []


@pytest.mark.parametrize("replans", [None, "1"])
def test_runtime_cli_legacy_goal_check_plan_flags_remain_supported(monkeypatch, tmp_path, replans):
    checks, plan = tmp_path / "checks.json", tmp_path / "plan.json"
    checks.write_text(json.dumps(policy_data()["checks"]))
    plan.write_text(json.dumps(search_plan()))
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    factory = Mock(wraps=ObjectiveAgent)
    monkeypatch.setattr(cli, "ObjectiveAgent", factory)
    args = ["objective", "--url", "https://example.test/", "--goal", GOAL,
            "--checks", str(checks), "--prepared-plan", str(plan)]
    if replans is not None:
        args.extend(["--max-replans", replans])
    monkeypatch.setattr(sys, "argv", args)
    assert cli.main() == 0
    assert factory.call_args.kwargs["goal"] == GOAL
    assert factory.call_args.kwargs["max_replans"] == (int(replans) if replans is not None else 0)


@pytest.mark.parametrize("flags", [[], ["--goal", GOAL], ["--checks", "missing.json"]])
def test_runtime_cli_requires_policy_or_complete_legacy_arguments(monkeypatch, capsys, flags):
    factory = Mock()
    monkeypatch.setattr(cli, "ObjectiveAgent", factory)
    monkeypatch.setattr(sys, "argv", ["objective", "--url", "https://example.test/", *flags])
    with pytest.raises(SystemExit):
        cli.main()
    assert "Supply --policy or both" in capsys.readouterr().err
    factory.assert_not_called()


def test_parser_returns_immutable_caller_checks_and_plan_without_aliasing_input():
    data = policy_data()
    policy = parse_policy(data)
    assert policy.objective == GOAL and policy.checks == (Check("value", "Ada Lovelace", label="Search"),)
    assert policy.prepared_plan.steps[0].texts[0].value == "Ada Lovelace"
    data["checks"][0]["value"] = "changed"
    data["prepared_plan"]["plan"][0]["texts"][0]["value"] = "changed"
    assert policy.checks[0].value == policy.prepared_plan.steps[0].texts[0].value == "Ada Lovelace"
    with pytest.raises(FrozenInstanceError):
        policy.objective = "different goal"


def test_parser_null_plan_and_runtime_arguments_are_defensive_and_pin_no_replanning():
    data = policy_data()
    policy = parse_policy(data)
    options = policy.agent_kwargs()
    assert options["goal"] == GOAL and options["checks"] == policy.checks and options["max_replans"] == 0
    assert parse_plan(options["prepared_plan"], objective=GOAL) == policy.prepared_plan
    options["prepared_plan"]["plan"][0]["texts"][0]["value"] = "changed"
    options["max_replans"] = 1
    assert policy.agent_kwargs()["prepared_plan"]["plan"][0]["texts"][0]["value"] == "Ada Lovelace"
    assert policy.agent_kwargs()["max_replans"] == 0
    data["prepared_plan"] = None
    assert parse_policy(data).agent_kwargs()["prepared_plan"] is None


@pytest.mark.parametrize("data", [None, [], "policy", 1])
def test_parser_rejects_non_object_envelopes(data):
    with pytest.raises(ValueError, match="Invalid declarative policy"):
        parse_policy(data)


@pytest.mark.parametrize("key", ["version", "objective", "checks", "prepared_plan"])
def test_parser_requires_every_envelope_key(key):
    data = policy_data()
    del data[key]
    with pytest.raises(ValueError):
        parse_policy(data)


@pytest.mark.parametrize("field, value", [
    ("version", True), ("version", 1.0), ("version", "1"), ("version", 2),
    ("objective", ""), ("objective", "   "), ("objective", " shifted "), ("objective", 1),
    ("objective", "x" * 2001), ("checks", []), ("checks", {}), ("checks", None),
    ("checks", [None]), ("checks", [{"kind": "text", "value": "Complete", "selector": "#unsafe"}]),
    ("checks", [{"kind": "checked", "value": "true", "label": "Option"}]),
    ("checks", [{"kind": "value", "value": "Ready"}]),
    ("prepared_plan", []), ("prepared_plan", {"objective": "changed", "plan": []}),
])
def test_parser_rejects_malformed_fields_with_safe_errors(field, value):
    data = policy_data()
    data[field] = value
    with pytest.raises(ValueError, match="Invalid declarative policy") as error:
        parse_policy(data)
    assert "#unsafe" not in str(error.value) and "Ada Lovelace" not in str(error.value)


@pytest.mark.parametrize("field", ["url", "limits", "max_replans", "capability", "selector", "code", "final_checks"])
def test_parser_rejects_unknown_envelope_fields(field):
    data = policy_data()
    data[field] = "untrusted"
    with pytest.raises(ValueError):
        parse_policy(data)


@pytest.mark.parametrize("field", ["selector", "code", "capability", "final_checks"])
def test_parser_rejects_unknown_nested_plan_fields(field):
    data = policy_data()
    data["prepared_plan"]["plan"][0][field] = "untrusted"
    with pytest.raises(ValueError):
        parse_policy(data)
