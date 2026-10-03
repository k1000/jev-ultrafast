"""Local Comet controller smoke: real DOM/input, stubbed planning/Jev; zero paid calls.

This proves controller mechanics, NOT model reasoning or public-site reliability.
"""

import argparse
import hashlib
import json
import sys
import time
import unicodedata
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from jev_ultrafast import Check, ObjectiveAgent

CASES = (("Montréal", "2027-03-20"), ("Málaga", "2028-02-10"), ("Zürich", "2027-04-08"))


def fixture(city, date):
    config = json.dumps({"city": city, "date": date})
    return """<!doctype html><title>Objective controller fixture</title>
<style>body{margin:30px}dialog{width:500px}button{padding:12px}</style>
<label>Destination<input id=city role=combobox></label><div id=suggestions role=listbox></div>
<label>Departure<input id=departure readonly></label>
<label>Category<select id=category><option value=all>All</option>
<option value=research>Research</option></select></label>
<label><input id=notifications type=checkbox>Notifications</label><button id=search>Search</button>
<p id=result>Not submitted</p><dialog id=calendar aria-label=Calendar></dialog>
<script>
const cfg=""" + config + """;
const city=document.querySelector('#city'), departure=document.querySelector('#departure');
const suggestions=document.querySelector('#suggestions'), calendar=document.querySelector('#calendar');
city.oninput=()=>setTimeout(()=>{
  suggestions.innerHTML='<button type=button role=option></button>';
  const option=suggestions.firstChild; option.textContent=cfg.city;
  option.onclick=()=>{city.value=cfg.city; suggestions.innerHTML=''};
},40);
let picked='';
const redraw=()=>{
  calendar.innerHTML='<h2>Target month</h2><label>Picked date<input readonly id=picked></label>'+
    '<button type=button id=day>Requested date</button><button type=button id=apply>Apply date</button>';
  document.querySelector('#day').onclick=()=>{
    picked=cfg.date; document.querySelector('#picked').value=picked;
    document.querySelector('#day').setAttribute('aria-pressed','true');
  };
  document.querySelector('#apply').onclick=()=>{departure.value=picked; calendar.close()};
};
departure.onclick=()=>{
  picked=''; calendar.innerHTML='<h2>Previous month</h2><button type=button id=next>Next month</button>';
  document.querySelector('#next').onclick=redraw; calendar.showModal();
};
document.querySelector('#search').onclick=()=>{
  if(city.value===cfg.city && departure.value===cfg.date && document.querySelector('#category').value==='research')
    document.querySelector('#result').textContent='Confirmed '+cfg.city+' '+cfg.date+' Research';
};
</script>"""


def run_case(city, date):
    query = "".join(c for c in unicodedata.normalize("NFD", city) if not unicodedata.combining(c))
    goal = f"Search {city} on {date} in Research; leave Notifications unchanged and confirm the result."
    result = f"Confirmed {city} {date} Research"
    checks = (Check("value", city, label="Destination"), Check("value", date, label="Departure"),
              Check("value", "research", label="Category"), Check("checked", False, label="Notifications"),
              Check("text", result))
    plan = {"objective": goal, "plan": [
        {"goal": "Select destination", "texts": [{"label": "Destination", "value": query}],
         "checks": [{"kind": "value", "label": "Destination", "value": query}]},
        {"goal": "Choose and apply the requested date", "texts": [],
         "checks": [{"kind": "value", "label": "Departure", "value": date}]},
        {"goal": "Set Research category", "texts": [],
         "checks": [{"kind": "value", "label": "Category", "value": "research"}]},
        {"goal": "Submit and confirm", "texts": [], "checks": [{"kind": "text", "value": result}]},
    ]}
    planner_count = 0
    decision_count = 0
    premature_done = False
    unknown_seen = False

    def planner(_context):
        nonlocal planner_count
        planner_count += 1
        assert planner_count == 1, "Unexpected semantic replan"
        return plan, {"model": "offline-planner-stub"}

    def choose(page, _goal, _history, *, execution):
        nonlocal decision_count, premature_done, unknown_seen
        decision_count += 1
        actions = page["actions"]
        facts = {c["label"]: c for c in page["controls"]}
        selected = None
        if page["modal_open"]:
            assert all(a.get("label") not in {"Destination", "Departure", "Category → Research", "Search"}
                       for a in actions), "Modal leaked background action targets"
            assert execution["check_evidence"][0]["state"] == "unknown"
            unknown_seen = True
            if "Picked date" in facts and facts["Picked date"].get("value") == date and not premature_done:
                premature_done = True
                selected = "DONE"  # Must defer, never promote hidden date/background facts to final success.
            else:
                label = "Next month" if any(a["label"] == "Next month" for a in actions) else (
                    "Requested date" if facts["Picked date"].get("value") != date else "Apply date")
                selected = next(a["id"] for a in actions if a["label"] == label)
        elif any(a.get("role") == "option" for a in actions):
            selected = next(a["id"] for a in actions if a.get("role") == "option")
        elif facts["Destination"].get("value") != city:
            selected = next(a["id"] for a in actions if a["kind"] == "fill" and a["label"] == "Destination")
        elif facts["Departure"].get("value") != date:
            selected = next(a["id"] for a in actions if a["kind"] == "click" and a["label"] == "Departure")
        elif facts["Category"].get("control_value") != "research":
            selected = next(a["id"] for a in actions if a["kind"] == "select" and a["value"] == "research")
        else:
            selected = next(a["id"] for a in actions if a["label"] == "Search")
        action = next((a for a in actions if a["id"] == selected), {})
        operation = {"fill": "TYPE_TEXT", "select": "SELECT", "click": "CLICK"}.get(action.get("kind"), "DONE")
        return {"choice": selected, "operation": operation, "target": "1", "confidence": 1.,
                "probabilities": {selected: 1.}, "latency_ms": 0, "usage": {}}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Paid HTTP/text helper forbidden")

    started = time.perf_counter()
    # Patch both boundaries and their shared HTTP helper: accidental paid calls fail the smoke.
    with patch("jev_ultrafast.agent.choose", choose), patch("jev_ultrafast.agent.field_texts", forbidden), \
            patch("jev_ultrafast.model.post_json", forbidden), patch("jev_ultrafast.planning.post_json", forbidden):
        with ExitStack() as cleanup:
            controller = ObjectiveAgent("data:text/html," + quote(fixture(city, date)), goal, checks=checks,
                                        planner=planner, max_decisions=16, max_actions=12, max_seconds=15)
            cleanup.callback(lambda: controller.close() if controller.status != "needs_attention" and
                             sys.exc_info()[0] is None else print(
                                 f"Fixture failed; retained tab {controller.browser.target}", file=sys.stderr))
            states = list(controller.run())
            state = states[-1]
            assert state["status"] == "done" and all(state["verification"]), state["stop_reason"]
            assert unknown_seen and premature_done
            assert state["deferred_steps"] == [1] and 1 not in state["verified_steps"]
            assert state["text_calls"] == [] and state["replans_used"] == 0 and planner_count == 1
            assert len(state["history"]) == 8, "Unexpected extra or repeated browser mutations"
            assert all(a["status"] == "executed" for a in state["attempts"])
            assert len({a["receipt"]["request_id"] for a in state["attempts"]}) == 8
            raw = controller.browser.evaluate("({city:document.querySelector('#city').value,"
                                              "date:document.querySelector('#departure').value,"
                                              "category:document.querySelector('#category').value,"
                                              "notifications:document.querySelector('#notifications').checked,"
                                              "result:document.querySelector('#result').textContent})")
            assert raw == {"city": city, "date": date, "category": "research", "notifications": False, "result": result}
            return {"city": city, "date": date, "verified": True, "verification": state["verification"],
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "planner_stub_calls": planner_count, "jev_stub_calls": decision_count,
                    "real_actions": len(state["history"]), "helpers": 0, "replans": 0,
                    "modal_unknown_seen": unknown_seen, "deferred_steps": state["deferred_steps"],
                    "events": state["events"], "attempts": state["attempts"], "raw_final": raw}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("Refusing to overwrite evidence")
    # This smoke only opens local data URLs, never public pages or existing tabs.
    root = Path(__file__).resolve().parents[1]
    sources = ("jev_ultrafast/objective.py", "jev_ultrafast/planning.py", "jev_ultrafast/snapshot.js",
               "jev_ultrafast/questions.py", "scripts/check_objective_flow.py")
    report = {"scope": "Real local Comet DOM and browser inputs; stubbed planner/Jev; NOT live model reliability",
              "paid_model_calls": 0, "cases": [],
              "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources}}
    try:
        for city, date in CASES:
            report["cases"].append(run_case(city, date))
    except BaseException as exc:
        report["error"] = type(exc).__name__
        raise
    finally:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("PASS: 3 local controller flows; autocomplete, modal, redraw, exact final checks; zero paid calls")


if __name__ == "__main__":
    main()
