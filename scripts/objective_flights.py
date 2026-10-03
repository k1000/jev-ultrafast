"""One paid Google Flights ObjectiveAgent search; never select or book a flight.

Test-owned read-only facts extend each atomic snapshot. The example verifier owns
completion; Jev still chooses every operation/observed target and prepared text.
"""

import base64
import hashlib
import json
import time
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.planning import evidence

URL = "https://www.google.com/travel/flights?hl=en"
DATE = "2027-03-20"
GOAL = (
    "Find one-way flights from Zurich to London on March 20, 2027, for one adult in economy. "
    "Stop when matching flight options are visible. Do not select or book a flight, sign in, or enable price tracking."
)

# Fixed, test-owned inspection only. This expression never becomes model input or a browser action.
FACTS = """(() => {
  const count=label=>{
    const nodes=[...document.querySelectorAll('[aria-label]')].filter(e=>e.getAttribute('aria-label')===label);
    return nodes.length===1 ? nodes[0].getAttribute('aria-valuenow') : null;
  };
  const rows=[...document.querySelectorAll('[aria-label]')].filter(e=>
    e.getAttribute('aria-label').includes('Select flight') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
  return {adults:count('Number of adult passengers'),children:count('Number of children aged 2 to 11'),
    infants_seated:count('Number of infants in their own seat'),infants_lap:count('Number of infants on lap'),
    flights:rows.filter(e=>{const r=e.getBoundingClientRect();return r.bottom>0 && r.top<innerHeight && r.width>0})
      .map(e=>e.getAttribute('aria-label'))};
})()"""


def verify_flights(page, departure=DATE):
    parsed = urlparse(page["url"])
    encoded = parse_qs(parsed.query).get("tfs", [""])[0]
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except ValueError:
        decoded = b""
    day = date.fromisoformat(departure)
    display = f"{day:%a}, {day:%b} {day.day}"
    spoken = f"{day:%A}, {day:%B} {day.day}"
    values = {a["label"].strip(): a.get("value") for a in page["actions"] if a["kind"] != "scroll_to"}
    facts = page.get("flight_facts", {})
    flights = facts.get("flights", [])
    checks = {
        "search_page": parsed.hostname == "www.google.com" and parsed.path == "/travel/flights/search",
        "one_way": values.get("Change ticket type. One way") == "One way",
        "origin": values.get("Where from?") == "Zürich",
        "destination": values.get("Where to?") == "London",
        "date": values.get("Departure") == display,
        "year": departure.encode() in decoded,
        "one_adult_no_children_or_infants": tuple(facts.get(k) for k in
            ("adults", "children", "infants_seated", "infants_lap")) == ("1", "0", "0", "0"),
        "economy": values.get("Change seating class. Economy") == "Economy",
        "results": bool(flights) and all(spoken in label for label in flights),
    }
    return {"passed": all(checks.values()), "checks": checks, "visible_flights": flights}


class SearchOnlyBrowser(browser_module.Browser):
    def validate_action(self, action, page, text=None):
        super().validate_action(action, page, text)
        if any(marker in action["label"] for marker in ("Select flight", "Sign in", "Track prices")):
            raise ValueError("Search-only test prohibits this action; no input issued")


class FlightsObjective(ObjectiveAgent):
    """Example-only full-outcome verifier, never a site-specific execution plan."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, verifier=lambda page: verify_flights(page)["passed"], **kwargs)

    def planning_context(self, reason):
        context = super().planning_context(reason)
        context["independent_flight_checks"] = verify_flights(self.agent.state["page"])["checks"]
        return context


def run(output):
    output = Path(output)
    original_reader, original_browser = browser_module.READ_STATE, loop.Browser
    # Facts and DOM action table come from the same JS evaluation, not separately sampled state.
    browser_module.READ_STATE = "(() => {const page=" + original_reader + "; if (!page) return null; " + (
        "page.flight_facts=" + FACTS + "; return page;})()"
    )
    loop.Browser = SearchOnlyBrowser
    agent = None
    report = {"goal": GOAL, "date": DATE, "scope": "One live complex search, not a reliability benchmark. "
              "Consent rejection is separate setup. Timing includes planning and final verification, excludes "
              "initial navigation. Test-owned facts/verifier are read-only; no executor action plan or text values.",
              "source_hashes": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [Path(__file__), *Path("jev_ultrafast").glob("*")]
                                if p.suffix in {".py", ".js"} and not p.name.startswith("._")}}
    started = time.perf_counter()
    departure = date.fromisoformat(DATE)
    display_date = f"{departure:%a}, {departure:%b} {departure.day}"
    try:
        agent = FlightsObjective(URL, GOAL, checks=(
            Check("url_contains", "/travel/flights/search"),
            Check("value", "Zürich", label="Where from?"),
            Check("value", "London", label="Where to? "),
            Check("value", "One way", label="Change ticket type. One way"),
            Check("value", display_date, label="Departure"),
            Check("value", "Economy", label="Change seating class. Economy"),
        ),
                                 max_decisions=36, max_actions=36, max_seconds=120, max_replans=1)
        report["initial_ms"] = round((time.perf_counter() - started) * 1000)
        report["limits"] = {"decisions": 36, "actions": 36, "seconds": 120, "replans": 1,
                            "local_corrections_per_blocker": 2}
        run_started = time.perf_counter()
        for state in agent.run():
            print(json.dumps({"status": state["status"], "stop_reason": state["stop_reason"],
                              "elapsed_ms": state["elapsed_ms"], "checks": state["verification"],
                              "additional_verified": state["additional_verified"],
                              "planner": len(state["planner_calls"]), "jev": len(state["decisions"]),
                              "prediction_attempts": len(state["prediction_calls"]),
                              "helper": len(state["text_calls"]), "actions": len(state["history"]),
                              "last_action": state["history"][-1]["action"] if state["history"] else None}), flush=True)
        # A NEW final observation, never a DONE signal, independently validates the full outcome.
        final = agent.browser.observe(screenshot=False)
        report["verification"] = verify_flights(final)
        report["caller_check_evidence"] = evidence(final, agent.checks)
        report["elapsed_ms"] = round((time.perf_counter() - run_started) * 1000)
        state = agent.snapshot()
        report.update(status=state["status"], stop_reason=state["stop_reason"], planner_calls=state["planner_calls"],
                      jev_calls=len(state["decisions"]), helper_calls=len(state["text_calls"]),
                      actions=len(state["history"]), replans_used=state["replans_used"], final_url=final["url"],
                      check_evidence=state["check_evidence"], additional_verified=state["additional_verified"],
                      verified_steps=state["verified_steps"],
                      deferred_steps=state["deferred_steps"])
        report["decisions"] = [{k: d.get(k) for k in ("operation", "choice", "latency_ms", "confidence")}
                               for d in state["decisions"]]
        report["prepared_plan"] = {"objective": agent.plan.objective, "plan": [
            {"goal": s.goal, "texts": [vars(t) for t in s.texts], "checks": [vars(c) for c in s.checks]}
            for s in agent.plan.steps
        ]} if agent.plan else None
        report["milestones"] = state["plan"]
        report["events"] = state["events"]
        report["history"] = [{k: h.get(k) for k in ("operation", "action", "text", "text_source", "from_url", "url")}
                             for h in state["history"]]
        report["passed"] = (state["status"] == "done" and report["verification"]["passed"]
                            and all(r["state"] == "met" for r in report["caller_check_evidence"])
                            and not state["text_calls"])
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report.update(error=type(exc).__name__, passed=False)
        if isinstance(exc, browser_module.BrowserSetupError):
            report["setup_error"] = {"phase": exc.phase, "cause_type": exc.cause_type,
                                     "cleanup_error": exc.cleanup_error, "retained_target": exc.retained_target}
            if exc.retained_target:
                report["retained_tab"] = exc.retained_target
        if agent:
            report["retained_tab"] = agent.browser.target
    finally:
        if agent:
            if agent.status == "needs_attention" or report.get("error"):
                report["retained_tab"] = agent.browser.target
            else:
                try:
                    agent.close()
                except Exception as exc:
                    report.update(cleanup_error=type(exc).__name__, passed=False, retained_tab=agent.browser.target)
        if agent:
            report["browser_calls"] = agent.browser.cdp_calls
            report["execution_attempts"] = agent.agent.state.get("attempts", [])
            report["prediction_calls"] = deepcopy(agent.agent.state["prediction_calls"])
            report["jev_prediction_attempts"] = len(report["prediction_calls"])
            report.setdefault("events", deepcopy(agent.events))
            report.setdefault("status", agent.status)
            report.setdefault("stop_reason", agent.stop_reason)
        browser_module.READ_STATE, loop.Browser = original_reader, original_browser
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
