"""Owned local Chrome P5 keyboard/range fixture; never acts on an existing target."""

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

from jev_ultrafast import browser as browser_module
from jev_ultrafast.browser import Browser, BrowserSetupError
from jev_ultrafast.planning import Check, parse_plan, verify

HTML = """<!doctype html><title>Key/range fixture</title>
<dialog id="modal" open aria-label="Local notice" tabindex="0"><button>Notice</button></dialog>
<form id="form"><label>Search<input id="search" type="search" value="prepared query"></label>
<label>Budget<input id="budget" type="range" min="0.1" max="0.9" step="0.2" value="0.1"></label></form>
<p id="outcome"></p><script>
window.keyDowns=0;window.keyUps=0;window.submitCount=0;
search.onkeydown=()=>window.keyDowns++;search.onkeyup=()=>window.keyUps++;
modal.onkeydown=e=>{if(e.key==='Escape'){modal.remove();search.focus()}};
form.onsubmit=e=>{e.preventDefault();window.submitCount++;outcome.textContent='Search submitted'};
modal.focus();</script>"""
GOAL = "Dismiss the notice, submit the prepared search and set Budget to 0.3."
PLAN = {"objective": GOAL, "plan": [{"goal": "Set the observed slider", "texts": [
    {"label": "Budget", "role": "slider", "value": "0.3"}],
    "checks": [{"kind": "value", "label": "Budget", "role": "slider", "value": "0.3"}]}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    if Path(browser_module.__file__).resolve() != Path(__file__).resolve().parents[1] / "jev_ultrafast/browser.py":
        parser.error("Require worktree source; run from worktree root with python -m scripts.check_key_range")
    if (os.environ.get("BU_NAME") != "jev-chrome-diagnostic" or
            os.environ.get("BU_CDP_URL") != "http://127.0.0.1:9334"):
        parser.error("Require isolated diagnostic Harness on port 9334")
    pid = subprocess.check_output(["lsof", "-nP", "-t", "-iTCP:9334", "-sTCP:LISTEN"], text=True).splitlines()[0]
    process = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True)
    if "/Google Chrome.app/" not in process or "Chrome-JevDiagnostics" not in process:
        parser.error("Require isolated Chrome profile")
    ensure_daemon(wait=15)
    with urlopen("http://127.0.0.1:9334/json/list", timeout=3) as response:
        direct = {t["id"] for t in json.load(response) if t.get("type") == "page"}
    if direct != {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}:
        parser.error("Harness target set must match isolated Chrome")

    blocked = Mock(side_effect=AssertionError("Model/helper request prohibited"))
    report = {"scope": "New owned data:text/html target, three distinct observed actions, no paid requests; "
                       "DOM mechanics only, not live Jev reasoning.",
              "source_hashes": {path: hashlib.sha256((Path(__file__).resolve().parents[1] / path)
                                                        .read_bytes()).hexdigest()
                                for path in ("jev_ultrafast/snapshot.js", "jev_ultrafast/browser.py",
                                             "scripts/check_key_range.py")}}
    browser = None
    phase = "bootstrap"
    try:
        with patch("jev_ultrafast.model.post_json", blocked), patch("jev_ultrafast.planning.post_json", blocked), \
                patch("jev_ultrafast.agent.field_texts", blocked), patch("jev_ultrafast.agent.choose", blocked):
            prepared = parse_plan(PLAN, objective=GOAL).steps[0].texts[0].value
            browser = Browser("data:text/html," + quote(HTML))
            phase = "escape"
            page = browser.observe(screenshot=False)
            assert page["modal_open"] is True
            escape = next(a for a in page["actions"] if a["kind"] == "press_key" and a["key"] == "Escape")
            escape_receipt = browser.act(escape, page)
            report["escape_receipt"] = {"status": escape_receipt["status"],
                                        "input_started": escape_receipt["input_started"],
                                        "phases": [(c["phase"], c["status"]) for c in escape_receipt["calls"]]}
            assert escape_receipt["status"] == "executed"
            page = browser.observe(screenshot=False)
            assert page["modal_open"] is False
            phase = "enter"
            enter = next(a for a in page["actions"] if a["kind"] == "press_key" and a["key"] == "Enter")
            enter_receipt = browser.act(enter, page)
            report["enter_receipt"] = {"status": enter_receipt["status"],
                                       "input_started": enter_receipt["input_started"],
                                       "phases": [(c["phase"], c["status"]) for c in enter_receipt["calls"]]}
            assert enter_receipt["status"] == "executed"
            page = browser.observe(screenshot=False)
            report["enter_dom"] = browser.evaluate(
                "({downs:window.keyDowns,ups:window.keyUps,submits:window.submitCount})")
            report["enter_check"] = verify(page, (Check("text", "Search submitted"),)) == [True]
            assert report["enter_check"]
            phase = "range"
            slider = next(a for a in page["actions"] if a["kind"] == "set_range" and a["label"] == "Budget")
            range_receipt = browser.act(slider, page, text=prepared)
            report["range_receipt"] = {"status": range_receipt["status"],
                                       "input_started": range_receipt["input_started"],
                                       "phases": [(c["phase"], c["status"]) for c in range_receipt["calls"]]}
            assert range_receipt["status"] == "executed"
            page = browser.observe(screenshot=False)
            assert verify(page, (Check("value", prepared, label="Budget", role="slider"),)) == [True]
            assert blocked.call_count == 0
            report.update(passed=True, acknowledged_actions=3, model_or_helper_calls=0,
                          final_checks=[True, True])
            phase = "cleanup"
            browser.close()
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report.update(passed=False, failure_phase=phase, error=type(exc).__name__)
        if isinstance(exc, BrowserSetupError):
            report.update(retained_target=exc.retained_target,
                          setup_error={"phase": exc.phase, "cause_type": exc.cause_type,
                                       "cleanup_error": exc.cleanup_error})
    finally:
        report.update(model_or_helper_calls=blocked.call_count,
                      retained_target=browser.target if browser else report.get("retained_target"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as evidence:
            evidence.write(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: report.get(k) for k in ("passed", "failure_phase", "error",
                                                    "model_or_helper_calls", "retained_target")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
