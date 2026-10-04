"""Conservative label-intent helpers for caller-owned safety policies, not action authorization."""

import re


def is_dialog_dismissal(action, page):
    """Recognize a unique observed modal-close button, not a link or coordinated transaction.

    This is a caption heuristic, not proof of a website callback's effects. Callers must still
    validate the action, destination, freshness, and outcome. Unknown captions are not exempted.
    """
    if (page.get("modal_open") is not True or action.get("kind") != "click" or
            action.get("role") != "button" or action.get("href") or
            sum(row == action for row in page.get("actions", ())) != 1):
        return False
    label = " ".join(action.get("label", "").casefold().split())
    if re.search(r"\b(?:and|then|to|accept|agree|continue|submit|confirm|buy|purchase|book|reserve|register|create)"
                 r"\b|[&+]", label):
        return False
    return re.fullmatch(
        r"(?:close|dismiss)(?: (?:this|the))?(?: (?:[\w-]+ )*"
        r"(?:dialog|window|popup|pop-up|information|notice|message|banner))?[.!]?", label) is not None
