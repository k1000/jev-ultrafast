"""Bounded live consent probe: Jev chooses, caller permits only an observed Reject.

Run with process-memory PLANNER_* and TYPESAFE_API_KEY on isolated Comet. Paid APIs.
No selectors, action plan, optional-cookie acceptance, mutation retry, or raw trace output.
"""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.browser import PolicyRejected
from scripts.live_safety import CookieSafeBrowser, consent_dialog, safe_reject

GOAL = ("Decline optional cookies using an explicit Reject, Decline, or essential-only/necessary-only choice. "
        "Never choose Accept all or accept optional cookies, sign in, navigate, or change any other setting. "
        "Stop when the consent panel is gone after rejection.")


class UnindexedConsentFrame(RuntimeError):
    """Visible modal content is in a frame this browser cannot index or act on."""


def consent_verified(page, receipts, permitted_ids, initial_url):
    issued = [r for r in receipts if r.get("input_started") is True]
    return (page.get("modal_open") is False and page.get("url") == initial_url and
            len(issued) == 1 and issued[0].get("status") == "executed" and
            issued[0].get("action_id") in permitted_ids)


class ConsentOnlyBrowser(CookieSafeBrowser):
    """Caller-owned veto; Jev still selects operation and observed target."""

    frame_clicks_enabled = True

    def validate_action(self, action, page, text=None):
        super().validate_action(action, page, text)
        if action["kind"] == "wait":  # No browser input.
            return
        if any(r.get("input_started") for r in getattr(self, "receipts", ())):
            raise PolicyRejected("Consent probe permits at most one browser input")
        if (not consent_dialog(page) or action["kind"] != "click" or
                action.get("role") != "button" or not safe_reject(action.get("label"))):
            raise PolicyRejected("Only an observed optional-cookie rejection is permitted")
        if not hasattr(self, "permitted_action_ids"):
            self.permitted_action_ids = set()
        self.permitted_action_ids.add(action["id"])


def preflight(url, title_fragment):
    """New owned tab; read only. Do not pay for a missing or ambiguous Reject control."""
    browser = browser_module.Browser(url)
    browser.frame_clicks_enabled = True  # Read-only frame discovery; preflight never executes input.
    try:
        unindexed = False
        for attempt in range(4):
            page = browser.observe(screenshot=False, response_timeout=5)
            observed_url = urlsplit(page.get("url") or "")
            if observed_url.query or observed_url.fragment:
                return None  # Never forward redirect handoff tokens into a planner prompt.
            choices = [a for a in page.get("actions", ()) if a.get("kind") == "click" and
                       a.get("role") == "button" and safe_reject(a.get("label"))]
            if (consent_dialog(page) and title_fragment in (page.get("title") or "") and
                    len(choices) == 1):
                return page["url"]
            unindexed = (consent_dialog(page) and title_fragment in (page.get("title") or "") and
                         page.get("unindexed_modal_frames", 0) > 0 and not choices)
            if attempt < 3:
                time.sleep(1)
        if unindexed:
            raise UnindexedConsentFrame("No actionable observed rejection in modal iframe")
        return None
    finally:
        browser.close()


def run(url, title_fragment):
    report = {"site": urlsplit(url).netloc, "scope": "single observed consent rejection; not backend proof",
              "passed": False, "planner_calls": 0, "jev_decisions": 0, "browser_inputs": 0}
    agent = None
    initial_url = None
    try:
        initial_url = preflight(url, title_fragment)
        if initial_url is None:
            report["reason"] = "no_unique_observed_reject_or_unavailable"
            return report  # No model or browser input.
        original_browser = loop.Browser
        loop.Browser = ConsentOnlyBrowser
        ref = [None]
        try:
            def extra(page):
                current = ref[0]
                return current is not None and consent_verified(
                    page, getattr(current.browser, "receipts", ()),
                    getattr(current.browser, "permitted_action_ids", set()), initial_url)

            agent = ObjectiveAgent(initial_url, GOAL,
                                   checks=(Check("url", initial_url), Check("title", title_fragment)),
                                   verifier=extra, max_replans=0, max_decisions=8,
                                   max_actions=4, max_seconds=45)
            ref[0] = agent
            for _ in agent.run():
                pass
            state = agent.snapshot()
            receipts = getattr(agent.browser, "receipts", ())
            report.update(status=state["status"], stop_reason=state["stop_reason"],
                          planner_calls=len(state["planner_calls"]), jev_decisions=len(state["decisions"]),
                          browser_inputs=sum(r.get("input_started") is True for r in receipts),
                          uncertain_inputs=sum(r.get("status") == "outcome_unknown" for r in receipts),
                          verification=state["verification"], additional_verified=state["additional_verified"])
            report["passed"] = (state["status"] == "done" and state["stop_reason"] == "verified" and
                                state["verification"] == [True, True] and
                                state["additional_verified"] is True and
                                report["browser_inputs"] == 1 and report["uncertain_inputs"] == 0)
            return report
        finally:
            loop.Browser = original_browser
    except UnindexedConsentFrame:
        report["reason"] = "unindexed_consent_frame"
        return report
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report["error"] = type(exc).__name__
        return report
    finally:
        if agent is not None:
            if agent.status == "needs_attention" or getattr(agent.browser, "_uncertain_receipt", None):
                report["retained_target"] = agent.browser.target  # Private internal-SSD report only.
            else:
                try:
                    agent.close()
                except Exception as exc:
                    report["cleanup_error"] = type(exc).__name__
                    report["passed"] = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--title-fragment", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path.home() / "Library/Application Support/JevDiagnostics"
    output = args.output.expanduser().resolve()
    if (not output.is_relative_to(root) or output.exists() or
            urlsplit(args.url).scheme != "https" or not args.title_fragment.strip()):
        parser.error("Use a new internal-SSD JevDiagnostics output and an HTTPS URL/title check")
    if Path(browser_module.__file__).resolve().parents[1] != Path.cwd().resolve():
        parser.error("Invoke with python -m scripts.consent_probe from this repository")
    if not all(os.environ.get(k) for k in
               ("BU_CDP_URL", "PLANNER_API_KEY", "PLANNER_BASE_URL", "PLANNER_MODEL", "TYPESAFE_API_KEY")):
        parser.error("Process-memory browser, planner and TypeSafe configuration required")
    if os.environ["BU_CDP_URL"] != "http://127.0.0.1:9333":
        parser.error("Use isolated Comet on 9333")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.parent.chmod(0o700)
    result = run(args.url, args.title_fragment)
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                   "w", encoding="utf-8") as evidence:
        evidence.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result.get(k) for k in
                      ("site", "passed", "reason", "status", "stop_reason", "planner_calls",
                       "jev_decisions", "browser_inputs", "uncertain_inputs", "error")}), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
