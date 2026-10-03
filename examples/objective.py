"""Run one objective with a prepared plan or an upfront planner and last-resort replanning.

Paid calls require TYPESAFE_API_KEY plus PLANNER_API_KEY/BASE_URL/MODEL in the process environment.
Final checks are caller-owned data; plans cannot redefine them. Do not put secrets in command arguments.
"""

import argparse
import json
from pathlib import Path

from jev_ultrafast import Check, ObjectiveAgent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--checks", required=True, type=Path, help="JSON array of caller-owned checks")
    parser.add_argument("--prepared-plan", type=Path, help="Optional planner-produced JSON plan")
    parser.add_argument("--max-replans", type=int, default=1)
    args = parser.parse_args()
    checks = tuple(Check(**row) for row in json.loads(args.checks.read_text()))
    prepared = json.loads(args.prepared_plan.read_text()) if args.prepared_plan else None
    with ObjectiveAgent(args.url, args.goal, checks=checks, prepared_plan=prepared,
                        max_replans=args.max_replans) as agent:
        for state in agent.run():
            print(state["elapsed_ms"], "ms", state["status"], state["stop_reason"],
                  "planner calls:", len(state["planner_calls"]), "verification:", state["verification"], flush=True)
        return 0 if state["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
