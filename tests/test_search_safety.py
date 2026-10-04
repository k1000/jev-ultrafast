"""Dismissal intent is not sign-in intent; incidental words must not bypass safeguards."""

import pytest

from jev_ultrafast.safety import is_dialog_dismissal


def observed(label, *, kind="click", role="button", href="", modal=True):
    action = {"id": "close", "node": 1, "kind": kind, "role": role, "label": label}
    if href:
        action["href"] = href
    return action, {"modal_open": modal, "actions": [dict(action)]}


def test_dismiss_sign_in_information_is_a_passive_observed_modal_button():
    action, page = observed("Dismiss sign in information.")
    assert is_dialog_dismissal(action, page)


@pytest.mark.parametrize("label", ["Close", "Dismiss", "Close this dialog", "Close registration window",
                                   "Dismiss account notice", "Close checkout popup."])
def test_generic_close_captions_are_not_site_aliases(label):
    assert is_dialog_dismissal(*observed(label))


@pytest.mark.parametrize("label", ["Sign in", "Sign in or register", "Buy now", "Accept all",
    "Dismiss and sign in", "Close then buy now", "Dismiss sign in information and continue",
    "Close to register", "Dismiss sign in information & buy now", "Close and accept cookies banner",
    "Dismiss submit notice", "Dismiss continue message", "Close agree notice", "Close accept cookies banner"])
def test_transactional_or_coordinated_labels_are_not_exempted(label):
    assert not is_dialog_dismissal(*observed(label))


@pytest.mark.parametrize("changes", [dict(kind="fill"), dict(kind="scroll_to"), dict(role="link"),
    dict(href="https://example.test/login"), dict(modal=False)])
def test_only_observed_modal_button_clicks_can_be_exempted(changes):
    assert not is_dialog_dismissal(*observed("Dismiss sign in information.", **changes))


def test_unobserved_and_duplicate_buttons_are_not_exempted():
    action, page = observed("Dismiss sign in information.")
    assert not is_dialog_dismissal(action, {"modal_open": True, "actions": []})
    page["actions"].append(dict(action))
    assert not is_dialog_dismissal(action, page)
