"""Owned no-model Chrome fixture for native fill and fail-closed focus loss."""

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

from jev_ultrafast.browser import Browser, BrowserSetupError

VALUE = "fixture-value"
CASES = {
    "normal": "",
    "click_redirect": "target.addEventListener('click',()=>other.focus());",
    "same_caption_replacement": "target.addEventListener('click',()=>{const copy=target.cloneNode(true);"
                                "target.replaceWith(copy);copy.focus()});",
    "keyboard_redirect": "target.addEventListener('keyup',e=>{if(e.key==='a')other.focus()});",
}
HTML = """<!doctype html><title>Owned focus-binding fixture</title>
<label>Observed field<input id="field"></label><label>Other field<input id="other"></label>
<script>const target=document.getElementById('field'),other=document.getElementById('other');
window.inputCount=0;document.addEventListener('input',()=>window.inputCount++);HANDLER</script>"""


def run_case(name, handler):
    browser = None
    record = {"case": name, "passed": False}
    try:
        browser = Browser("data:text/html," + quote(HTML.replace("HANDLER", handler)))
        page = browser.observe(screenshot=False)
        matches = [a for a in page["actions"] if a["kind"] == "fill" and a["label"] == "Observed field"]
        assert len(matches) == 1
        receipt = browser.execute(matches[0], page, text=VALUE)
        observed = browser.evaluate("""({field:document.getElementById('field').value,
          other:document.getElementById('other').value,input_count:window.inputCount,
          original_connected:window.__jevFast.nodes.get(""" + str(matches[0]["node"]) + """).isConnected,
          original_focused:document.activeElement===window.__jevFast.nodes.get(""" +
          str(matches[0]["node"]) + ")})")
        record.update(receipt=receipt, observed=observed)
        assert observed["other"] == ""
        if name == "normal":
            assert receipt["status"] == "executed" and observed["field"] == VALUE and observed["input_count"] == 1
            phases = [c["phase"] for c in receipt["calls"]]
            assert "focus_after_activation" in phases and "focus_before_insert" in phases
            assert receipt["phase"] == "insertText"
            record.update(goal_met=True, lock_verified=None)
            browser.close()
        else:
            phase = "focus_before_insert" if name == "keyboard_redirect" else "focus_after_activation"
            assert receipt["status"] == "outcome_unknown" and receipt["input_started"] is True
            assert receipt["phase"] == phase
            assert observed["field"] == "" and observed["input_count"] == 0
            assert not any(c["method"] == "Input.insertText" for c in receipt["calls"])
            if name != "keyboard_redirect":
                assert not any(c["method"] == "Input.dispatchKeyEvent" for c in receipt["calls"])
            if name == "same_caption_replacement":
                assert observed["original_connected"] is False
            else:
                assert observed["original_focused"] is False
            fresh = browser.observe(screenshot=False)  # Inspection remains available while locked.
            other = next(a for a in fresh["actions"] if a["kind"] == "fill" and a["label"] == "Other field")
            before = len(browser.cdp_calls)
            # Exercise only the local lock path with a DIFFERENT observed target: no CDP command may be emitted.
            assert browser.execute(other, fresh, text=VALUE) == receipt
            assert len(browser.cdp_calls) == before
            record.update(goal_met=False, lock_verified=True)
        record["passed"] = True
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        record["error"] = type(exc).__name__
        if isinstance(exc, BrowserSetupError):
            record.update(setup_error={"phase": exc.phase, "cause_type": exc.cause_type,
                                       "cleanup_error": exc.cleanup_error}, retained_target=exc.retained_target)
    finally:
        if browser:
            record.update(retained_target=browser.target, browser_calls=browser.cdp_calls)
    return record


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
    with urlopen("http://127.0.0.1:9334/json/version", timeout=3) as response:
        product = json.load(response)["Browser"]
    sources = ("jev_ultrafast/browser.py", "jev_ultrafast/snapshot.js", __file__)
    report = {"scope": "Owned local data-URL Chrome fixtures, no model/helper calls. Positive typing plus three "
                       "negative focus-binding cases. Negative PASS proves prevented text input/lock, not goal "
                       "completion or live Flights reliability. Uncertain owned tabs remain open for inspection.",
              "browser": product, "isolated_profile": "Chrome-JevDiagnostics", "cases": [],
              "source_hashes": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump({"passed": False, "scope": "Reserved for one approved local fixture run"}, stream)
    blocked = Mock(side_effect=AssertionError("Model/helper requests prohibited"))
    with patch("jev_ultrafast.agent.choose", blocked), patch("jev_ultrafast.agent.field_texts", blocked), \
            patch("jev_ultrafast.model.post_json", blocked), patch("jev_ultrafast.planning.post_json", blocked), \
            patch("httpx.Client.post", blocked):
        for name, handler in CASES.items():
            result = run_case(name, handler)
            report["cases"].append(result)
            print(json.dumps({k: result.get(k) for k in ("case", "passed", "error", "goal_met", "retained_target")}),
                  flush=True)
            if not result["passed"]:
                break  # No automatic retry or continuation after unexpected failure.
    report.update(model_or_helper_calls=blocked.call_count,
                  passed=len(report["cases"]) == len(CASES) and all(c["passed"] for c in report["cases"])
                         and not blocked.called)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]),
                      "model_or_helper_calls": blocked.call_count}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
