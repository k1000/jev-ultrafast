"""Deterministic OFFLINE fixtures: simulated browser and stub policies, never live Jev."""

import argparse
import json
from copy import deepcopy
from unittest.mock import patch

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import ExecutionUncertain, StalePage, fingerprint
from jev_ultrafast.evaluation import evaluate_trajectories, record_trajectory

_BASIC_SCENARIOS = ("memo_submit", "select_submit", "missing_text")
_ADVERSARIAL_SCENARIOS = ("stale_target", "uncertain_input")


class FixtureBrowser:
    """Test-only environment. Fixture values are not executor defaults for real sites."""

    def __init__(self, url, *, native_select=False):
        self.page = {"url": url, "title": "Offline fixture", "text": "Pending", "scroll": {"y": 0},
                     "actions": [
                         {"id": "field", "kind": "fill", "node": 1, "label": "Memo", "role": "textbox",
                          "value": ""},
                         {"id": "submit", "kind": "click", "node": 2, "label": "Submit", "role": "button"},
                     ], "controls": [{"node": 3, "kind": "checkbox", "role": "checkbox", "label": "Unrelated",
                                   "checked": False}]}
        if native_select:
            self.page["actions"][0] = {"id": "field", "kind": "select", "node": 1,
                                       "label": "Status → Approved", "role": "select", "value": "approved",
                                       "control_value": "pending", "current_value": "Pending"}
        self.calls = []
        self.closed = False
        self.completed = False
        self.refresh()

    def refresh(self):
        self.page["controls"] = [self.page["controls"][0], *deepcopy(self.page["actions"])]
        self.page["fingerprint"] = fingerprint(self.page)

    def observe(self, screenshot=False):
        return deepcopy(self.page)

    def fresh(self, page, action=None, *, terminal=False):
        return page["fingerprint"] == self.page["fingerprint"]

    def act(self, action, page, text=None):
        if not self.fresh(page) or action not in self.page["actions"]:
            raise StalePage("Fixture target rejected before input")
        self.calls.append(action["kind"])
        if action["kind"] == "fill":
            self.page["actions"][0]["value"] = text
        elif action["kind"] == "select":
            self.page["actions"][0].update(control_value=action["value"], current_value="Approved")
        elif action["kind"] == "click":
            field = self.page["actions"][0]
            self.completed = (field["control_value"] == "approved" if field["kind"] == "select"
                              else field["value"] == "Reviewed")
            self.page["text"] = "Complete" if self.completed else "Pending"
        self.refresh()

    def close(self):
        self.closed = True


class StaleFixtureBrowser(FixtureBrowser):
    """Replace a target before the first input; an old choice cannot execute afterward."""

    stale_rejections = 0

    def act(self, action, page, text=None):
        if self.stale_rejections == 0:
            self.stale_rejections += 1
            self.page["actions"][0].update(id="replacement_field", node=4)
            self.refresh()
            raise StalePage("Target replaced before input")
        return super().act(action, page, text)


class UncertainFixtureBrowser(FixtureBrowser):
    """One real simulated effect, then a lost acknowledgment; retain it for inspection."""

    def act(self, action, page, text=None):
        if getattr(self, "_uncertain_receipt", None) is None:
            super().act(action, page, text)
            self._uncertain_receipt = {"status": "outcome_unknown", "input_started": True,
                                       "phase": "typing", "error": "RuntimeError"}
        raise ExecutionUncertain(deepcopy(self._uncertain_receipt))


def progress_policy(page, goal, history, *, execution):
    """A fixture-only semantic baseline, not a replacement for Jev or a site action script."""
    action = next((a for a in page["actions"] if (a["kind"] == "fill" and not a["value"])
                   or (a["kind"] == "select" and a["control_value"] != a["value"])),
                  next(a for a in page["actions"] if a["kind"] == "click"))
    operation = {"fill": "TYPE_TEXT", "select": "SELECT", "click": "CLICK"}[action["kind"]]
    return {"choice": action["id"], "operation": operation, "target": "1", "confidence": 1.,
            "probabilities": {action["id"]: 1.}, "latency_ms": 0, "usage": {}}


def forbidden(*_args, **_kwargs):
    raise AssertionError("Offline fixtures must not call a browser transport or model API")


def run_fixture(name, variant):
    if name not in (*_BASIC_SCENARIOS, *_ADVERSARIAL_SCENARIOS) or variant not in {
            "progress", "premature_done", "blocked"}:
        raise ValueError("Unknown offline fixture or stub policy")
    goal = "Set Memo to Reviewed and submit"
    plan = {"objective": goal, "plan": [{"goal": goal,
            "texts": [{"label": "Memo", "value": "Reviewed"}],
            "checks": [{"kind": "text", "value": "Complete"}]}]}
    checks = (Check("value", "Reviewed", label="Memo"), Check("text", "Complete"))
    if name == "select_submit":
        goal = "Set Status to Approved and submit"
        plan["objective"] = plan["plan"][0]["goal"] = goal
        plan["plan"][0]["texts"] = []
        checks = (Check("value", "approved", label="Status"), Check("text", "Complete"))
    elif name == "missing_text":
        plan["plan"][0]["texts"] = []

    def policy(page, goal, history, *, execution):
        if variant == "progress":
            return progress_policy(page, goal, history, execution=execution)
        operation = "DONE" if variant == "premature_done" else "BLOCKED"
        return {"choice": operation, "operation": operation, "target": None, "confidence": 1.,
                "probabilities": {operation: 1.}, "latency_ms": 0, "usage": {}}

    def browser(url):
        if name == "stale_target":
            return StaleFixtureBrowser(url)
        if name == "uncertain_input":
            return UncertainFixtureBrowser(url)
        return FixtureBrowser(url, native_select=name == "select_submit")

    with (patch.object(loop, "Browser", browser), patch.object(loop, "choose", policy),
          patch.object(loop, "field_texts", forbidden), patch("httpx.Client.send", forbidden),
          patch("jev_ultrafast.browser.cdp", forbidden)):
        with ObjectiveAgent("https://offline.invalid/", goal, checks=checks, prepared_plan=plan,
                            planner=forbidden, settle_timeout=0, max_decisions=8, max_actions=8) as agent:
            report = record_trajectory(agent.run())
            expected = ("abandoned" if name == "missing_text" else "inconclusive" if name == "uncertain_input"
                        else "verified")
            if name == "missing_text":
                grounded = not agent.browser.completed and not agent.browser.calls
            elif name == "uncertain_input":
                grounded = (report["uncertain_attempts"] == report["mutation_attempts"] == 1
                            and len(agent.browser.calls) == report["decisions"] == 1 and report["actions"] == 0)
            else:
                grounded = agent.browser.completed
            report.update(scenario=name, expected_outcome=expected,
                          fixture_pass=report["outcome"] == expected and grounded
                          and agent.browser.page["controls"][0]["checked"] is False)
            if name == "stale_target":
                report.update(stale_rejections=agent.browser.stale_rejections,
                              simulated_inputs=len(agent.browser.calls))
                report["fixture_pass"] = (report["fixture_pass"] and agent.browser.stale_rejections == 1
                                          and report["decisions"] == 3 and len(agent.browser.calls) == 2)
            elif name == "uncertain_input":
                report.update(simulated_inputs=len(agent.browser.calls),
                              simulated_effect_observed=agent.browser.page["actions"][0]["value"] == "Reviewed")
                report["fixture_pass"] = report["fixture_pass"] and report["simulated_effect_observed"]
        if name == "uncertain_input":
            report["environment_retained"] = not agent.browser.closed
            report["fixture_pass"] = report["fixture_pass"] and report["environment_retained"]
    return report


def compare_fixture_policies(*, repeats=2, adversarial=False):
    if type(repeats) is not int or repeats < 1:
        raise ValueError("Supply a positive integer repeat count")
    variants = {}
    scenarios = _ADVERSARIAL_SCENARIOS if adversarial else _BASIC_SCENARIOS
    for variant in ("progress", "premature_done", "blocked"):
        runs = [run_fixture(name, variant) for _ in range(repeats) for name in scenarios]
        variants[variant] = {"runs": runs, "summary": evaluate_trajectories(runs),
                             "fixture_passes": sum(r["fixture_pass"] for r in runs)}
    return {"schema_version": 1, "evidence_scope": "simulated_browser_and_stub_policies_not_live_jev",
            "repeats": repeats, "variants": variants}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--adversarial", action="store_true",
                        help="Run stale-target and uncertain-input fixtures instead of the basic suite")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be a positive integer")
    print(json.dumps(compare_fixture_policies(repeats=args.repeats, adversarial=args.adversarial),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
