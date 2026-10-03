"""Bounded ObjectiveAgent public-page regressions. Paid APIs; never run live from pytest.

Supply isolated Comet CDP, TypeSafe, and PLANNER_* credentials in process memory.
Caller-owned checks and read-only JavaScript verify outcomes; no action plans are supplied.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast.browser import BrowserSetupError

FORM = "https://www.selenium.dev/selenium/web/web-form.html"
CDP = "http://127.0.0.1:9333"


@dataclass(frozen=True)
class Case:
    name: str
    url: str
    goal: str
    checks: tuple[Check, ...]
    check: str
    preflight: str
    expected: str = "done"
    limitation: str | None = None
    allowed_field: str | None = None


CASES = (
    Case("hn-new", "https://news.ycombinator.com/",
         "Open the new stories page from the Hacker News navigation and stop when it is open.",
         (Check("url", "https://news.ycombinator.com/newest"), Check("title", "Hacker News")),
         "location.origin==='https://news.ycombinator.com' && location.pathname==='/newest'",
         "location.origin==='https://news.ycombinator.com' && !!document.querySelector('a[href=\"newest\"]')"),
    Case("hn-article", "https://news.ycombinator.com/item?id=49922437",
         "Find the story titled Context Language Models on Hacker News and open its linked article. "
         "Stop when the linked article is open.",
         (Check("url", "https://arxiv.org/abs/2609.37725"), Check("title", "Context Language Models")),
         "location.origin==='https://arxiv.org' && location.pathname==='/abs/2609.37725' "
         "&& document.title.includes('Context Language Models')",
         "[...document.querySelectorAll('.titleline>a')].some(a=>a.textContent==='Context Language Models' "
         "&& a.href==='https://arxiv.org/abs/2609.37725')"),
    Case("wikipedia-search", "https://en.wikipedia.org/wiki/Main_Page",
         "Search Wikipedia for Ada Lovelace and open her article. Stop when the Ada Lovelace article is open.",
         (Check("url", "https://en.wikipedia.org/wiki/Ada_Lovelace"), Check("title", "Ada Lovelace")),
         "location.hostname==='en.wikipedia.org' && location.pathname==='/wiki/Ada_Lovelace' "
         "&& document.querySelector('#firstHeading')?.innerText==='Ada Lovelace'",
         "location.hostname==='en.wikipedia.org' && !!document.querySelector('input[name=search]')"),
    Case("form-text", FORM,
         "Enter Saffron Atlas in the Text input field and stop without submitting the form.",
         (Check("url", FORM), Check("value", "Saffron Atlas", label="Text input")),
         "location.pathname.endsWith('/web-form.html') "
         "&& document.querySelector('input[name=\"my-text\"]')?.value==='Saffron Atlas'",
         "!!document.querySelector('input[name=\"my-text\"]:enabled:not([readonly])')",
         allowed_field='input[name="my-text"]'),
    Case("form-select", FORM,
         "Choose Three in the Dropdown (select) field and stop without submitting the form.",
         (Check("url", FORM), Check("value", "3", label="Dropdown (select)")),
         "location.pathname.endsWith('/web-form.html') "
         "&& document.querySelector('select[name=\"my-select\"]')?.value==='3'",
         "!!document.querySelector('select[name=\"my-select\"]:enabled option[value=\"3\"]:enabled')",
         allowed_field='select[name="my-select"]'),
    Case("form-checkbox", FORM,
         "Check the Default checkbox on this web form, then stop without submitting.",
         (Check("url", FORM), Check("checked", True, label="Default checkbox")),
         "location.pathname.endsWith('/web-form.html') && document.querySelector('#my-check-2')?.checked===true",
         "document.querySelector('#my-check-2')?.type==='checkbox' && !document.querySelector('#my-check-2')?.disabled",
         allowed_field="#my-check-2"),
    Case("missing-dropdown", "https://example.org/",
         "Choose Enterprise in the plan dropdown on this page. Do not navigate away from this page.",
         (Check("url", "https://example.org/"), Check("value", "Enterprise", label="Plan")),
         "location.hostname==='example.org' "
         "&& [...document.querySelectorAll('select,[role=combobox]')].some(e=>e.value==='Enterprise')",
         "location.hostname==='example.org' && document.querySelector('select,[role=combobox]')===null",
         expected="abandoned",
         limitation="location.hostname==='example.org' && document.querySelector('select,[role=combobox]')===null"),
    Case("disabled-input", FORM,
         "Enter Saffron Atlas in the Disabled input field while leaving it disabled. "
         "Do not change other fields, navigate away, or submit the form.",
         (Check("url", FORM), Check("value", "Saffron Atlas", label="Disabled input")),
         "location.pathname.endsWith('/web-form.html') "
         "&& document.querySelector('input[name=\"my-disabled\"]')?.disabled===true "
         "&& document.querySelector('input[name=\"my-disabled\"]')?.value==='Saffron Atlas'",
         "document.querySelector('input[name=\"my-disabled\"]')?.disabled===true "
         "&& document.querySelector('input[name=\"my-disabled\"]')?.value===''",
         expected="abandoned",
         limitation="location.pathname.endsWith('/web-form.html') "
         "&& document.querySelector('input[name=\"my-disabled\"]')?.disabled===true "
         "&& document.querySelector('input[name=\"my-disabled\"]')?.value===''")
)


def safety_snapshot(case):
    """Test-owned read only; excluded requested field may change. Snapshot is never persisted."""
    selector = json.dumps(case.allowed_field)
    return f"""(() => {{
      const allowed={selector};
      const fields=[...document.querySelectorAll('input,textarea,select')]
        .filter(e=>!['password','file','hidden'].includes(e.type) && !(allowed && e.matches(allowed)))
        .map(e=>[e.id,e.name,e.type,e.value,e.checked,e.disabled,e.readOnly]);
      return JSON.stringify({{url:location.href,fields}});
    }})()"""


def safety_passed(case, before, after):
    if case.url != FORM and case.expected != "abandoned":
        # Different documents cannot establish whether source-page controls were changed before navigation.
        return None
    if not isinstance(before, str) or not isinstance(after, str):
        return False
    return before == after


def passed(case, result):
    if (result.get("error") or result.get("cleanup_error") or result.get("verification_error")
            or result.get("preflight_unavailable") or result.get("skipped") or result.get("harness_stop")
            or result.get("run_error") or result.get("uncertain_execution")
            or result.get("text_requests") != 0):
        return False
    if (case.url == FORM or case.expected == "abandoned") and result.get("safety_verified") is not True:
        return False
    if case.expected == "done":
        return (result.get("status") == "done" and result.get("stop_reason") == "verified"
                and result.get("verified") is True and result.get("controller_verified") is True)
    return (result.get("status") == "abandoned" and result.get("stop_reason") == "replanning_exhausted"
            and result.get("verified") is False and result.get("controller_verified") is False
            and result.get("limitation_verified") is True and result.get("actions") == 0)


def run_case(case):
    result = {"case": case.name, "initial_url": case.url, "expected": case.expected, "verified": False}
    agent = None
    execution_started = False
    started = time.perf_counter()
    try:
        agent = ObjectiveAgent(case.url, case.goal, checks=case.checks,
                               max_decisions=12, max_actions=12, max_seconds=60, max_replans=1)
        result["initial_ms"] = round((time.perf_counter() - started) * 1000)
        if agent.browser.evaluate(case.preflight) is not True:
            result.update(preflight_unavailable=True, passed=False)
            return result  # Keep the same object so finally's cleanup diagnostics are retained.
        baseline = agent.browser.evaluate(safety_snapshot(case))
        run_started = time.perf_counter()
        execution_started = True
        try:
            for _state in agent.run():
                pass
        except Exception as exc:
            result.update(error=type(exc).__name__, run_error=type(exc).__name__, uncertain_execution=True)
        result["controller_ms"] = round((time.perf_counter() - run_started) * 1000)
        state = agent.snapshot()
        verification = state["verification"]
        result.update(status=state["status"], stop_reason=state.get("stop_reason"),
                      controller_verified=all(verification) if verification is not None else None,
                      stale_retries=state.get("stale_retries", 0), actions=len(state["history"]),
                      decisions=len(state["decisions"]), text_requests=len(state["text_calls"]),
                      planner_requests=len(state["planner_calls"]), replans=state["replans_used"],
                      corrections=state["corrections_used"])
        result["final_url"] = state["page"]["url"]
        result["operations"] = [h["operation"] for h in state["history"]]
        result["model_choices"] = [d["operation"] for d in state["decisions"]]
        result["typesafe_ms"] = sum(d["latency_ms"] for d in state["decisions"])
        result["planner_ms"] = sum(r.get("latency_ms", 0) for r in state["planner_calls"])
        result["planner_errors"] = [r["error"] for r in state["planner_calls"] if r.get("error")]
        result["budget_exhausted"] = state["stop_reason"] in {"decision_budget", "action_budget", "time_budget"}
        result["uncertain_execution"] = result.get("uncertain_execution", False) or (
            state["status"] == "needs_attention" and state["stop_reason"] != "planning_failed"
        )
        try:
            result["verified"] = agent.browser.evaluate(case.check) is True
            if case.limitation:
                result["limitation_verified"] = agent.browser.evaluate(case.limitation) is True
            final = agent.browser.evaluate(safety_snapshot(case))
            result["safety_verified"] = safety_passed(case, baseline, final)
            result["safety_scope"] = "unchanged_other_fields_and_url" if (
                case.url == FORM or case.expected == "abandoned"
            ) else "not_assessed_for_navigation"
        except Exception as exc:
            result["verification_error"] = type(exc).__name__
        result["agent_ms"] = round((time.perf_counter() - run_started) * 1000)
    except (KeyboardInterrupt, SystemExit) as exc:
        result.setdefault("error", type(exc).__name__)
        result.update(interrupted=True, uncertain_execution=True)
    except BrowserSetupError as exc:
        result.update(error=type(exc).__name__, setup_phase=exc.phase, setup_cause=exc.cause_type)
        if exc.cleanup_error:
            result.update(cleanup_error=exc.cleanup_error, retained_tab=exc.retained_target)
    except Exception as exc:
        result.setdefault("error", type(exc).__name__)
        if execution_started:
            result.update(evidence_error=type(exc).__name__, uncertain_execution=True)
    finally:
        if agent:
            if result.get("uncertain_execution") or (
                agent.status == "needs_attention" and agent.stop_reason != "planning_failed"
            ):
                result["retained_tab"] = agent.browser.target
                result["uncertain_execution"] = True
            else:
                try:
                    agent.close()
                except Exception as exc:
                    result["cleanup_error"] = type(exc).__name__
    result["passed"] = passed(case, result)
    return result


def preflight_environment():
    required = ("BU_CDP_URL", "TYPESAFE_API_KEY", "PLANNER_API_KEY", "PLANNER_BASE_URL", "PLANNER_MODEL")
    if any(not os.environ.get(k) for k in required):
        raise ValueError("Comet, TypeSafe and PLANNER_* configuration must be supplied in process memory")
    if os.environ["BU_CDP_URL"] != CDP:
        raise ValueError("Use isolated Comet CDP on port 9333")
    pid = subprocess.check_output(["lsof", "-nP", "-t", "-iTCP:9333", "-sTCP:LISTEN"], text=True).splitlines()[0]
    command = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True)
    if "/Comet.app/" not in command or "Comet-JevAutomation" not in command:
        raise ValueError("Port 9333 must belong to the isolated Comet profile")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 3:
        parser.error("--repeats must be 1..3")
    try:
        preflight_environment()
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError):
        parser.error("Require isolated Comet on 9333, TypeSafe and PLANNER_* runtime configuration")
    output = args.output or (
        Path("artifacts/regressions") / datetime.now(timezone.utc).strftime("web-%Y%m%dT%H%M%SZ.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "configuration": {k: os.environ.get(k) for k in ("TYPESAFE_MODEL", "PLANNER_MODEL", "PLANNER_REASONING")},
        "source_hashes": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in [Path(__file__), *Path("jev_ultrafast").glob("*")]
                          if p.suffix in {".py", ".js"} and not p.name.startswith("._")},
        "limits": {"repeats": args.repeats, "decisions": 12, "actions": 12, "seconds": 60, "replans": 1},
        "scope": "Public-page smoke regressions, not a reliability/speed benchmark or WCAG assessment. "
                 "Agent time includes upfront planning and verification, excluding setup/navigation. Budgets are soft "
                 "between synchronous calls. Negative cases need independent limitation/no-mutation evidence; budgets "
                 "alone cannot pass. No screenshots, page bodies, form baselines or secrets are persisted. "
                 "Counts are logical attempts/accepted choices, not instrumented HTTP transport retries.",
        "runs": [],
    }
    uncertain = set()
    for repeat in range(args.repeats):
        for case in CASES:
            if case.name in uncertain:
                result = {"case": case.name, "skipped": "Inspect earlier uncertain input before repeating",
                          "passed": False}
            else:
                result = run_case(case)
                if result.get("uncertain_execution"):
                    uncertain.add(case.name)
            result["repeat"] = repeat + 1
            report["runs"].append(result)
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(case.name, repeat + 1, "PASS" if result["passed"] else "FAIL",
                  result.get("status"), result.get("stop_reason"), result.get("agent_ms"), "ms",
                  "planner=", result.get("planner_requests"), "jev=", result.get("decisions"),
                  "helper=", result.get("text_requests"), flush=True)
            if result.get("interrupted"):
                print(f"Interrupted; partial evidence: {output}", flush=True)
                return 130
    count = sum(r["passed"] for r in report["runs"])
    print(f"Result: {count}/{len(report['runs'])} passed. Evidence: {output}")
    return 0 if count == len(report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
