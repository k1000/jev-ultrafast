"""Caller-owned veto for bounded public-page probes; not a site-specific action script."""

import re

from jev_ultrafast.browser import Browser, PolicyRejected

_REJECT = re.compile(r"(?:reject(?: all(?: optional)?(?: cookies)?)?|"
                     r"reject non-essential|decline(?: all)?(?: optional cookies)?|"
                     r"only necessary(?: cookies)?|essential only(?: cookies)?|do not consent|"
                     r"accetta i cookie essenziali)", re.I)
_CONSENT = re.compile(r"\b(?:cookies?|consent|privacy)\b", re.I)
_FORBIDDEN = re.compile(r"\b(?:accept|agree|allow|sign[ -]?in|log[ -]?in|book|buy|"
                        r"purchase|checkout|pay|payment|subscribe|register|reserve)\b", re.I)


def safe_reject(label):
    return isinstance(label, str) and _REJECT.fullmatch(" ".join(label.split())) is not None


def consent_dialog(page):
    return page.get("modal_open") is True and bool(_CONSENT.search(page.get("modal_label") or ""))


class CookieSafeBrowser(Browser):
    """No optional-cookie acceptance, sign-in, booking or purchase during live probes."""

    def validate_action(self, action, page, text=None):
        super().validate_action(action, page, text)
        if action["kind"] == "wait":
            return
        if consent_dialog(page):
            if (action["kind"] != "click" or action.get("role") != "button" or
                    not safe_reject(action.get("label"))):
                raise PolicyRejected("Only explicit rejection is allowed on a consent dialog")
        elif action["kind"] == "click" and _FORBIDDEN.search(action.get("label") or ""):
            raise PolicyRejected("No acceptance, account, booking or purchase clicks")
