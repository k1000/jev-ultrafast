"""Local delayed-outcome check on isolated Chrome; model requests are blocked."""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote
from urllib.request import urlopen

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from jev_ultrafast import Check, model, planning
from jev_ultrafast.browser import Browser, BrowserSetupError

HTML = """<!doctype html><title>Local settling fixture</title>
<label>Status<input id="status" readonly value="Pending"></label>
<button id="search">Search</button>
<script>window.actionCount=0;document.getElementById('search').onclick=()=>{
window.actionCount++;setTimeout(()=>{document.getElementById('status').value='Ready'},600)};</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    if (os.environ.get("BU_NAME") != "jev-chrome-diagnostic" or
            os.environ.get("BU_CDP_URL") != "http://127.0.0.1:9334"):
        parser.error("Require separate Chrome diagnostic Harness on port 9334")
    pid = subprocess.check_output(["lsof", "-nP", "-t", "-iTCP:9334", "-sTCP:LISTEN"], text=True).splitlines()[0]
    command = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True)
    if "/Google Chrome.app/" not in command or "Chrome-JevDiagnostics" not in command:
        parser.error("Require isolated Chrome profile")
    ensure_daemon(wait=15)
    with urlopen("http://127.0.0.1:9334/json/list", timeout=3) as response:
        direct = {t["id"] for t in json.load(response) if t.get("type") == "page"}
    if direct != {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}:
        parser.error("Harness must target exactly isolated Chrome")
    original_model, original_planner = model.post_json, planning.post_json
    blocked = Mock(side_effect=AssertionError("No model API requests permitted"))
    model.post_json = planning.post_json = blocked
    browser = None
    sources = ("jev_ultrafast/browser.py", "jev_ultrafast/snapshot.js", "tests/test_settle.py", __file__)
    report = {"scope": "One owned local Chrome fixture, manually selected observed action. "
                      "No live Jev/Flights; model APIs blocked.",
              "fixture_sha256": hashlib.sha256(HTML.encode()).hexdigest(),
              "source_hashes": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources}}
    started = time.perf_counter()
    try:
        browser = Browser("data:text/html," + quote(HTML))
        page = browser.observe(screenshot=False)
        actions = [a for a in page["actions"] if a["kind"] == "click" and a["label"] == "Search"]
        assert len(actions) == 1
        receipt = browser.execute(actions[0], page)
        assert receipt["status"] == "executed"
        before = browser.observe(screenshot=False)
        assert before["ready_state"] == "complete"
        outcome = browser.settle((Check("value", "Ready", label="Status"),), timeout=3, interval=.1)
        count = browser.evaluate("window.actionCount")
        assert outcome["status"] == "verified" and outcome["reads"] > 1 and count == 1
        assert len(browser.receipts) == 1 and not blocked.called
        report.update(passed=True, document_ready_before_settling=before["ready_state"], action_count=count,
                      outcome={k: v for k, v in outcome.items() if k not in ("page", "last_observed_page")})
        browser.close()
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report.update(passed=False, error=type(exc).__name__)
        if isinstance(exc, BrowserSetupError):
            report.update(retained_target=exc.retained_target,
                          setup_error={"phase": exc.phase, "cause_type": exc.cause_type,
                                       "cleanup_error": exc.cleanup_error})
    finally:
        model.post_json, planning.post_json = original_model, original_planner
        report.update(elapsed_ms=round((time.perf_counter() - started) * 1000), model_calls=blocked.call_count,
                      retained_target=browser.target if browser else report.get("retained_target"),
                      execution_receipts=getattr(browser, "receipts", []),
                      browser_calls=getattr(browser, "cdp_calls", []))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        summary = ("passed", "error", "action_count", "model_calls", "outcome", "retained_target")
        print(json.dumps({k: report.get(k) for k in summary}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
