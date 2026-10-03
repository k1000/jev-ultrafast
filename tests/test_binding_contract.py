"""Site-independent caption identity: whitespace is presentation, values are not."""

import pytest

from jev_ultrafast.planning import Check, Text, controls, evidence, parse_plan, resolve_controls


@pytest.mark.parametrize("kind,role,name,expected", [
    ("fill", "textbox", "Invoice number", "INV-42"),
    ("select", "combobox", "Preferred language", "de"),
    ("click", "checkbox", "Allow notifications", True),
    ("control", "textbox", "Project status", "Ready"),
])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("spacing", [" ", "  ", "\n\t", "\u00a0"])
def test_caption_whitespace_normalizes_for_facts_and_action_bindings(kind, role, name, expected, reverse, spacing):
    decorated = "\t " + name.replace(" ", spacing) + "\n "
    observed, requested = (name, decorated) if reverse else (decorated, name)
    action = {"node": 1, "kind": kind, "role": role, "label": observed, "group": "Billing details",
              "observable": True, "control_value": expected, "checked": expected}
    if kind == "select":
        action["label"] += " → Offered option"
        action["value"] = "not-the-selected-value"
    page = {"actions": [] if kind == "control" else [action], "controls": [action]}
    check = Check("checked" if role == "checkbox" else "value", expected, label=requested,
                  role=role, group="\n Billing\t details ")
    assert evidence(page, (check,)) == [{"state": "met", "reason": "observed"}]
    assert set(controls(page, check, facts=True)) == {1}
    assert set(controls(page, check)) == (set() if kind == "control" else {1})


def test_caption_collisions_remain_ambiguous_even_when_values_agree():
    rows = [{"node": node, "kind": "fill", "label": name, "value": "INV-42", "observable": True}
            for node, name in enumerate(("Invoice number", " Invoice\tnumber "), 1)]
    page = {"actions": rows, "controls": rows}
    assert evidence(page, (Check("value", "INV-42", label="Invoice number"),)) == [
        {"state": "unknown", "reason": "ambiguous_control"}]
    assert len(controls(page, Text("Invoice number", "INV-42"))) == 2
    rows[0]["group"], rows[1]["group"] = "Billing details", "Shipping details"
    assert set(controls(page, Text(" Invoice number ", "INV-42", group=" Billing\n details "))) == {1}


@pytest.mark.parametrize("expected,observed", [("Ready", " Ready "), ("Ready", "ready"), ("Zürich", "Zurich")])
def test_normalizing_a_caption_never_normalizes_the_expected_value(expected, observed):
    row = {"node": 1, "kind": "control", "label": " Status ", "control_value": observed}
    assert evidence({"actions": [], "controls": [row]}, (Check("value", expected, label="Status"),)) == [
        {"state": "unmet", "reason": "observed"}]


@pytest.mark.parametrize("label,role", [("Invoice Number", "textbox"), ("Invoice-number", "textbox"),
                                       ("Invoice number", "Textbox")])
def test_no_fuzzy_caption_or_role_matching(label, role):
    row = {"node": 1, "kind": "fill", "label": label, "role": role}
    assert controls({"actions": [row]}, Text("Invoice number", "INV-42", role="textbox")) == {}


def test_scoped_single_typo_can_recover_prepared_binding_but_cannot_prove_a_final_check():
    row = {"node": 1, "kind": "fill", "label": "Invoice number", "role": "textbox",
           "group": "Billing details", "value": "INV-42"}
    page = {"actions": [row]}
    binding = Text("Invoice numbr", "INV-42", role="textbox", group="Billing details")
    nodes, audit = resolve_controls(page, binding, recover=True)
    assert set(nodes) == {1}
    assert audit["method"] == "edit_distance" and audit["distance"] == 1
    assert audit["node"] == 1 and audit["score"] >= .9
    assert "INV-42" not in repr(audit)
    assert evidence(page, (Check("value", "INV-42", label="Invoice numbr"),)) == [
        {"state": "unknown", "reason": "missing_control"}]


@pytest.mark.parametrize("labels,role,group", [
    (["Invoice number", "Invoice numbers"], "textbox", "Billing details"),
    (["Invoice number", "Invoice number"], "textbox", "Billing details"),
    (["Invoice number"], "", "Billing details"),
    (["Invoice number"], "textbox", ""),
    (["Invoice number"], "combobox", "Billing details"),
    (["Invoice number"], "textbox", "Shipping details"),
    (["Invoice number 2"], "textbox", "Billing details"),
    (["Invoice number"], "textbox", "Billing details"),
])
def test_typo_recovery_rejects_ambiguous_unscoped_mismatched_or_noneditable_controls(labels, role, group):
    rows = [{"node": index, "kind": "fill", "label": label, "role": "textbox", "group": "Billing details"}
            for index, label in enumerate(labels, 1)]
    if labels == ["Invoice number"] and role == "textbox" and group == "Billing details":
        rows[0]["kind"] = "click"
    binding = Text("Invoice numbr", "INV-42", role=role, group=group)
    assert resolve_controls({"actions": rows}, binding, recover=True)[0] == {}


def test_final_facts_never_enable_typo_recovery_even_if_requested():
    row = {"node": 1, "kind": "fill", "label": "Invoice number", "role": "textbox", "group": "Billing details"}
    assert resolve_controls({"actions": [row], "controls": [row]},
                            Text("Invoice numbr", "INV-42", role="textbox", group="Billing details"),
                            recover=True, facts=True)[0] == {}


@pytest.mark.parametrize("enabled", [False, True])
def test_controller_uses_one_scoped_prepared_value_and_records_recovery_without_a_helper(monkeypatch, enabled):
    from unittest.mock import Mock

    from test_objective import FakeBrowser, chosen

    from jev_ultrafast import ObjectiveAgent
    from jev_ultrafast import agent as loop
    from jev_ultrafast.browser import fingerprint

    class InvoiceBrowser(FakeBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page["actions"][0].update(label="Invoice number", group="Billing details")
            self.page["fingerprint"] = fingerprint(self.page)

    monkeypatch.setattr(loop, "Browser", InvoiceBrowser)
    chooser = Mock(return_value=chosen("e1"))
    helper = Mock(side_effect=AssertionError("No helper allowed"))
    monkeypatch.setattr(loop, "choose", chooser)
    monkeypatch.setattr(loop, "field_texts", helper)
    plan = {"objective": "Enter invoice number", "plan": [{"goal": "Enter number", "texts": [
        {"label": "Invoice numbr", "role": "textbox", "group": "Billing details", "value": "INV-42"}],
        "checks": [{"kind": "value", "label": "Invoice number", "value": "INV-42"}]}]}
    with ObjectiveAgent("https://example.test/", "Enter invoice number", prepared_plan=plan,
                        checks=(Check("value", "INV-42", label="Invoice number"),),
                        **({"recover_bindings": True} if enabled else {})) as agent:
        state = agent.command()
        if enabled:
            assert state["status"] == "done" and state["verification"] == [True]
            assert agent.browser.calls == [("e1", "INV-42")]
            audit = next(e for e in state["events"] if e["kind"] == "binding_recovered")
            assert audit["distance"] == 1 and audit["binding_step"] == audit["binding_text"] == 0
            assert "INV-42" not in repr(audit) and "Invoice" not in repr(audit)
        else:
            assert state["status"] == "ready" and agent.browser.calls == []
            assert not any(e["kind"] == "binding_recovered" for e in state["events"])
    helper.assert_not_called()
    assert chooser.call_count == 1


@pytest.mark.parametrize("flag", ["omitted_actions", "omitted_controls"])
def test_partial_observations_do_not_authorize_typo_recovery(flag):
    row = {"node": 1, "kind": "fill", "label": "Invoice number", "role": "textbox", "group": "Billing details"}
    nodes, audit = resolve_controls({"actions": [row], flag: 1},
        Text("Invoice numbr", "INV-42", role="textbox", group="Billing details"), recover=True)
    assert nodes == {} and audit["reason"] == "incomplete_observation"


@pytest.mark.parametrize("observable", [True, False])
def test_nonactionable_facts_still_block_a_close_runner_up(observable):
    row = {"node": 1, "kind": "fill", "label": "Invoice number", "role": "textbox", "group": "Billing details"}
    other = {**row, "node": 2, "kind": "control", "label": "Invoice numbers", "observable": observable}
    nodes, audit = resolve_controls({"actions": [row], "controls": [row, other]},
        Text("Invoice numbr", "INV-42", role="textbox", group="Billing details"), recover=True)
    assert nodes == {} and audit["reason"] == "insufficient_margin"


@pytest.mark.parametrize("property", ["disabled", "readonly"])
def test_recovery_never_selects_a_disabled_or_readonly_fill(property):
    row = {"node": 1, "kind": "fill", "label": "Invoice number", "role": "textbox",
           "group": "Billing details", property: True}
    assert resolve_controls({"actions": [row]},
        Text("Invoice numbr", "INV-42", role="textbox", group="Billing details"), recover=True)[0] == {}


def test_exact_ambiguity_is_not_reduced_by_editable_filtering_or_recovery():
    rows = [{"node": node, "kind": kind, "label": "Invoice number", "role": "textbox", "group": "Billing details"}
            for node, kind in [(1, "fill"), (2, "click")]]
    nodes, audit = resolve_controls({"actions": rows},
        Text("Invoice number", "INV-42", role="textbox", group="Billing details"), recover=True)
    assert set(nodes) == {1, 2} and audit["method"] == "exact"


def test_conflicting_exact_and_recovered_prepared_values_cannot_authorize_input():
    from jev_ultrafast.objective import ObjectiveAgent, PreparedTextUnavailable

    controller = ObjectiveAgent.__new__(ObjectiveAgent)
    controller.recover_bindings = True
    controller.index = 0
    controller.plan = parse_plan({"objective": "Enter invoice number", "plan": [{"goal": "Enter number", "texts": [
        {"label": label, "role": "textbox", "group": "Billing details", "value": value}
        for label, value in [("Invoice number", "INV-42"), ("Invoice numbr", "OTHER")]],
        "checks": [{"kind": "value", "label": "Invoice number", "value": "INV-42"}]}]})
    row = {"node": 1, "kind": "fill", "label": "Invoice number", "role": "textbox", "group": "Billing details"}
    with pytest.raises(PreparedTextUnavailable):
        controller.prepared_value(row, {"actions": [row]})


@pytest.mark.parametrize("requested,observed", [("Project statuz", "Project status"),
    ("Preferred languag", "Preferred language"), ("Contact email adress", "Contact email address"),
    ("x" * 398 + "yz", "x" * 398 + "xz")])
def test_single_typo_policy_is_independent_of_application_names_and_label_length(requested, observed):
    row = {"node": 1, "kind": "fill", "label": observed, "role": "textbox", "group": "Settings"}
    nodes, audit = resolve_controls({"actions": [row]}, Text(requested, "Ready", role="textbox", group="Settings"),
                                    recover=True)
    assert set(nodes) == {1} and audit["distance"] == 1


def test_normalized_duplicate_prepared_bindings_fail_before_accepting_the_plan():
    with pytest.raises(ValueError):
        parse_plan({"objective": "Enter invoice number", "plan": [{"goal": "Enter number", "texts": [
            {"label": "Invoice number", "group": " Billing details ", "value": "INV-42"},
            {"label": " Invoice\tnumber ", "group": "Billing details", "value": "OTHER"},
        ], "checks": [{"kind": "value", "label": "Invoice number", "value": "INV-42"}]}]})
