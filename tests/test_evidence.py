"""Fresh verification facts are independent of the offered action space."""

from jev_ultrafast.planning import Check, evidence, verify


def test_selected_readonly_control_can_be_verified_without_offering_an_action():
    page = {"actions": [], "controls": [
        {"node": 1, "kind": "control", "role": "combobox", "label": "Category",
         "control_value": "3", "observable": True, "disabled": True},
    ]}
    assert verify(page, (Check("value", "3", label="Category"),)) == [True]


def test_hidden_cached_control_does_not_make_the_current_visible_control_ambiguous():
    check = Check("value", "Ready", label="Search")
    page = {"actions": [], "controls": [
        {"node": 1, "kind": "control", "label": "Search", "observable": False},
        {"node": 2, "kind": "control", "label": "Search", "observable": True, "value": "Ready"},
    ]}
    assert verify(page, (check,)) == [True]
    page["controls"].append({"node": 3, "kind": "control", "label": "Search", "observable": True, "value": "Ready"})
    assert evidence(page, (check,)) == [{"state": "unknown", "reason": "ambiguous_control"}]


def test_missing_or_obscured_controls_are_unknown_not_observed_contradictions():
    check = Check("value", "Ready", label="Search")
    page = {"actions": [], "controls": []}
    assert evidence(page, (check,)) == [{"state": "unknown", "reason": "missing_control"}]
    page["controls"] = [{"node": 1, "kind": "control", "label": "Search", "observable": False,
                         "value": "Ready"}]
    assert evidence(page, (check,)) == [{"state": "unknown", "reason": "obscured_control"}]
    assert verify(page, (check,)) == [False]  # Historical/stored values cannot prove final success.
    page["controls"][0].update(observable=True, value="Wrong")
    assert evidence(page, (check,)) == [{"state": "unmet", "reason": "observed"}]
    page["controls"][0]["value"] = "Ready"
    assert evidence(page, (check,)) == [{"state": "met", "reason": "observed"}]
