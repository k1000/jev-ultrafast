"""Local invoice fixture: scoped typo recovery, exact checks, and delayed extra verification."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote
from urllib.request import urlopen

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast.browser import BrowserSetupError
from jev_ultrafast.planning import verify

HTML = """<!doctype html><title>Binding contract fixture</title>
<form aria-label="Billing details"><label> Invoice   number <input id="invoice"></label></form>
<label>Project status<input id="status" readonly value="Pending"></label>
<button id="save">Save</button><script>window.actionCount=0;
invoice.oninput=()=>window.actionCount++;save.onclick=()=>{window.actionCount++;
setTimeout(()=>{document.getElementById('status').value='Ready'},600)};</script>"""
GOAL = "Save invoice INV-42 and wait for project status Ready."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    if (os.environ.get("BU_NAME") != "jev-chrome-diagnostic" or
            os.environ.get("BU_CDP_URL") != "http://127.0.0.1:9334"):
        parser.error("Require isolated Chrome diagnostic Harness on port 9334")
    pid = subprocess.check_output(["lsof", "-nP", "-t", "-iTCP:9334", "-sTCP:LISTEN"], text=True).splitlines()[0]
    process = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True)
    if "/Google Chrome.app/" not in process or "Chrome-JevDiagnostics" not in process:
        parser.error("Require isolated Chrome profile")
    ensure_daemon(wait=15)
    with urlopen("http://127.0.0.1:9334/json/list", timeout=3) as response:
        direct = {t["id"] for t in json.load(response) if t.get("type") == "page"}
    if direct != {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}:
        parser.error("Harness target set must match isolated Chrome")
    blocked = Mock(side_effect=AssertionError("Model/helper requests prohibited"))
    choices = []

    def choose(page, _goal, _history, *, execution):
        operations = ("TYPE_TEXT", "CLICK", "DONE")
        operation = operations[len(choices)]
        kind = "fill" if operation == "TYPE_TEXT" else "click"
        label = "Invoice number" if operation == "TYPE_TEXT" else "Save"
        matches = [a for a in page["actions"] if a["kind"] == kind and " ".join(a["label"].split()) == label]
        selected = "DONE" if operation == "DONE" else matches[0]["id"]
        assert operation == "DONE" or len(matches) == 1
        choices.append(operation)
        return {"operation": operation, "choice": selected, "target": "1", "confidence": 1.,
                "probabilities": {selected: 1.}, "latency_ms": 0, "usage": {}}

    plan = {"objective": GOAL, "plan": [{"goal": "Save invoice", "texts": [
        {"label": "Invoice numbr", "role": "textbox", "group": "Billing details", "value": "INV-42"}],
        "checks": [{"kind": "value", "label": "Invoice number", "value": "INV-42"}]}]}
    sources = ("jev_ultrafast/planning.py", "jev_ultrafast/objective.py", "jev_ultrafast/browser.py",
               "jev_ultrafast/snapshot.js", __file__)
    report = {"scope": "Owned local non-flight Chrome fixture. Stubbed Jev, prepared plan, API/helper calls blocked. "
                       "Tests mechanics, not live inference or universal fuzzy-match reliability.",
              "source_hashes": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources}}
    agent = None
    try:
        with patch("jev_ultrafast.agent.choose", choose), patch("jev_ultrafast.agent.field_texts", blocked), \
                patch("jev_ultrafast.model.post_json", blocked), patch("jev_ultrafast.planning.post_json", blocked):
            agent = ObjectiveAgent("data:text/html," + quote(HTML), GOAL, prepared_plan=plan,
                checks=(Check("value", "INV-42", label="\n Invoice number \t",
                              role="textbox", group="Billing details"),),
                recover_bindings=True, settle_timeout=3,
                verifier=lambda page: all(verify(page, (Check("value", "Ready", label="Project status"),))))
            states = list(agent.run())
            state = agent.snapshot()
            count = agent.browser.evaluate("window.actionCount")
            assert state["status"] == "done" and state["verification"] == [True]
            assert state["additional_verified"] is True and count == 2 and not blocked.called
            assert state["replans_used"] == 0 and choices == ["TYPE_TEXT", "CLICK", "DONE"]
            assert any(e["kind"] == "binding_recovered" for e in state["events"])
            assert agent.browser.settlements[-1]["reads"] > 1
            report.update(passed=True, action_count=count, states=len(states), final=state,
                          settlements=[{k: v for k, v in s.items() if k not in ("page", "last_observed_page")}
                                       for s in agent.browser.settlements])
            agent.close()
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report.update(passed=False, error=type(exc).__name__)
        if isinstance(exc, BrowserSetupError):
            report.update(retained_target=exc.retained_target,
                          setup_error={"phase": exc.phase, "cause_type": exc.cause_type,
                                       "cleanup_error": exc.cleanup_error})
    finally:
        report.update(model_or_helper_calls=blocked.call_count, stub_decisions=choices,
                      retained_target=agent.browser.target if agent else report.get("retained_target"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: report.get(k) for k in ("passed", "error", "action_count", "model_or_helper_calls",
                                                    "stub_decisions", "retained_target")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
