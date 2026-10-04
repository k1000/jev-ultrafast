"""Owned local Chrome overlay fixture; no provider calls or existing-tab mutation."""

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
from jev_ultrafast.browser import READ_STATE, Browser, BrowserSetupError

HTML = """<!doctype html><title>Overlay fixture</title>
<label>Background search<input id="background" aria-label="Background search"></label>
<div id="overlay" style="position:fixed;inset:0;background:white;z-index:200"
 aria-label="Local overlay fixture"><h2>Overlay heading</h2><button>Dismiss</button></div>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inspect-target", help="Read only a target recorded by a prior failed owned fixture")
    parser.add_argument("--prior", type=Path, help="Unmodified failed evidence containing the owned target ID")
    args = parser.parse_args()
    if bool(args.inspect_target) != bool(args.prior):
        parser.error("--inspect-target and --prior must be provided together")
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    if Path(browser_module.__file__).resolve() != Path(__file__).resolve().parents[1] / "jev_ultrafast/browser.py":
        parser.error("Require local worktree package; run with PYTHONPATH set to the worktree root")
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
    if args.inspect_target:
        prior = json.loads(args.prior.read_text())
        if (prior.get("passed") is not False or prior.get("retained_target") != args.inspect_target or
                args.inspect_target not in direct):
            parser.error("Read-only inspection requires the failed fixture's retained owned target")

    blocked = Mock(side_effect=AssertionError("Model/helper requests prohibited"))
    report = {"scope": "Read-only follow-up on failed fixture's retained owned target" if args.inspect_target
              else "Observation of a new owned local data:text/html target; no public-site completion.",
              "source_hashes": {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                for path in ("jev_ultrafast/snapshot.js", __file__)}}
    browser = None
    page = None
    phase = "owned_target_observation"
    try:
        with patch("jev_ultrafast.model.post_json", blocked), patch("jev_ultrafast.planning.post_json", blocked), \
                patch("jev_ultrafast.agent.choose", blocked), patch("jev_ultrafast.agent.field_texts", blocked):
            if args.inspect_target:
                session = cdp("Target.attachToTarget", targetId=args.inspect_target, flatten=True)["sessionId"]
                try:
                    reply = cdp("Runtime.evaluate", session_id=session, expression=READ_STATE, returnByValue=True)
                    if reply.get("exceptionDetails"):
                        raise RuntimeError("Owned fixture read failed")
                    page = reply["result"]["value"]
                finally:
                    cdp("Target.detachFromTarget", sessionId=session)
            else:
                browser = Browser("data:text/html," + quote(HTML))
                page = browser.observe(screenshot=False)
            phase = "assert_snapshot"
            report["observed_shape"] = {key: isinstance(page, dict) and key in page
                                        for key in ("controls", "actions", "modal_open", "modal_label")}
            background = next(c for c in page["controls"] if c["label"] == "Background search")
            checks = {"modal_open": page["modal_open"] is True,
                      "modal_label": page["modal_label"] == "Local overlay fixture",
                      "background_obscured": background["observable"] is False,
                      "background_not_offered": not any(a["label"] == "Background search" for a in page["actions"]),
                      "dismiss_offered": any(a["label"] == "Dismiss" for a in page["actions"]),
                      "no_model_calls": not blocked.called}
            report["checks"] = checks
            assert all(checks.values())
            report.update(passed=True, modal_open=page["modal_open"], modal_label=page["modal_label"],
                          background_observable=background["observable"],
                          exposed_actions=len(page["actions"]))
            if browser is not None:
                phase = "owned_target_cleanup"
                browser.close()
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report.update(passed=False, error=type(exc).__name__, failure_phase=phase)
        if isinstance(exc, KeyError):
            report["missing_key"] = (exc.args[0] if exc.args and exc.args[0] in
                                     {"controls", "actions", "modal_open", "modal_label", "value", "result"}
                                     else "other")
        if isinstance(exc, BrowserSetupError):
            report.update(retained_target=exc.retained_target,
                          setup_error={"phase": exc.phase, "cause_type": exc.cause_type,
                                       "cleanup_error": exc.cleanup_error})
    finally:
        report.update(model_or_helper_calls=blocked.call_count,
                      retained_target=(args.inspect_target if args.inspect_target else
                                       browser.target if browser else report.get("retained_target")))
        if args.inspect_target:
            report.update(prior_evidence=args.prior.name, prior_passed=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: report.get(k) for k in ("passed", "error", "model_or_helper_calls",
                                                    "retained_target")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
