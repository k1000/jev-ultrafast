"""Paired live comparison of speculative and selected-only text on a public demo form.

Requires Comet CDP, TYPESAFE_API_KEY and TEXT_MODEL_API_KEY in the process environment.
This script makes paid API calls; it never records credentials or raw model/page responses.
"""

import argparse
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast import agent as loop

URL = "https://www.selenium.dev/selenium/web/web-form.html"
CASES = {
    "text": (
        "Enter Saffron Atlas in the Text input field and stop without submitting the form.",
        "document.querySelector('input[name=\"my-text\"]')?.value === 'Saffron Atlas'",
    ),
    "select": (
        "Choose Three in the Dropdown (select) field and stop without submitting the form.",
        "document.querySelector('select[name=\"my-select\"]')?.value === '3'",
    ),
}


def trial(case, mode):
    goal, verifier = CASES[case]
    events, futures = [], []
    original_prepare, original_field = Agent.prepare_text, loop.field_texts

    def counted(context):
        started = time.perf_counter()
        event = {"fields": len(context["fields"])}
        try:
            value, meta = original_field(context)
            event["usage"] = meta.get("usage", {})
            return value, meta
        except Exception as exc:
            event["error"] = type(exc).__name__
            raise
        finally:
            event["ms"] = round((time.perf_counter() - started) * 1000)
            events.append(event)

    def speculative(self, context):
        original_prepare(self, context)
        if self.pending_text and self.pending_text[1] is not None:
            future = self.pending_text[1]
            if future not in futures:
                futures.append(future)

    Agent.prepare_text = speculative if mode == "speculative" else original_prepare
    loop.field_texts = counted
    agent = None
    start = time.perf_counter()
    result = {"case": case, "mode": mode, "verified": False}
    try:
        agent = Agent(URL, goal, speculative_text=(mode == "speculative"))
        result["initial_ms"] = round((time.perf_counter() - start) * 1000)
        try:
            for state in agent.run():
                if len(state["decisions"]) >= 10:
                    result["error"] = "decision budget"
                    break
        except Exception as exc:
            result["error"] = type(exc).__name__
        result["agent_ms"] = round((time.perf_counter() - start) * 1000 - result["initial_ms"])
        state = agent.snapshot()
        result["status"] = state["status"]
        result["decisions"] = len(state["decisions"])
        result["actions"] = len(state["history"])
        result["typesafe_ms"] = sum(item["latency_ms"] for item in state["decisions"])
        result["text_used"] = sum(bool(item.get("used")) for item in state["text_calls"])
        try:
            result["verified"] = agent.browser.evaluate(verifier) is True
        except Exception as exc:
            result["verification_error"] = type(exc).__name__
    except Exception as exc:
        result["error"] = type(exc).__name__
    finally:
        if agent:
            agent.close()
        for future in futures:
            try:
                future.result(timeout=40)  # Count unused calls after the task clock stops.
            except Exception:
                pass
        Agent.prepare_text = original_prepare
        loop.field_texts = original_field
    result["text_requests"] = len(events)
    result["text_ms"] = [event["ms"] for event in events]
    result["text_failures"] = sum("error" in event for event in events)
    result["text_usage"] = [event.get("usage", {}) for event in events]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.pairs < 1 or args.pairs > 10:
        parser.error("--pairs must be between 1 and 10")
    if any(not os.environ.get(key) for key in ("BU_CDP_URL", "TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY")):
        parser.error("Comet CDP and both API credentials must be supplied in the process environment")
    runs = []
    for case in CASES:
        for pair in range(args.pairs):
            order = ("speculative", "selected") if pair % 2 == 0 else ("selected", "speculative")
            for mode in order:
                result = trial(case, mode)
                result["pair"] = pair + 1
                runs.append(result)
                print(case, pair + 1, mode, "verified=", result["verified"],
                      "ms=", result.get("agent_ms"), "text_calls=", result["text_requests"], flush=True)
    summary = {
        f"{case}:{mode}": {
            "verified": sum(row["verified"] for row in rows),
            "attempts": len(rows),
            "median_agent_ms": statistics.median(row["agent_ms"] for row in rows if "agent_ms" in row),
            "text_requests": sum(row["text_requests"] for row in rows),
        }
        for case in CASES for mode in ("speculative", "selected")
        if (rows := [r for r in runs if r["case"] == case and r["mode"] == mode])
    }
    output = args.output or Path("artifacts/benchmarks") / (
        datetime.now(timezone.utc).strftime("text-%Y%m%dT%H%M%SZ.json")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"runs": runs, "summary": summary}, indent=2))
    print(json.dumps(summary, indent=2))
    print("Metrics:", output)


if __name__ == "__main__":
    main()
