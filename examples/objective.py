"""Run one objective with an upfront plan and Jev-only runtime decisions by default.

Jev calls need TYPESAFE_API_KEY; only upfront or opt-in replan calls need PLANNER_API_KEY/BASE_URL/MODEL.
Final checks are caller-owned data; plans cannot redefine them. Do not put secrets in command arguments.
"""

import argparse
import json
from pathlib import Path

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast.policy import parse_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--goal")
    parser.add_argument("--checks", type=Path, help="JSON array of caller-owned checks")
    parser.add_argument("--prepared-plan", type=Path, help="Optional planner-produced JSON plan")
    parser.add_argument("--policy", type=Path,
                        help="Reviewed non-secret v1 JSON policy; replaces goal/check/plan flags")
    parser.add_argument("--max-replans", type=int,
                        help="Legacy flags only: opt in to bounded runtime planning (default: 0)")
    args = parser.parse_args()
    if args.policy is not None:
        if any(value is not None for value in (args.goal, args.checks, args.prepared_plan, args.max_replans)):
            parser.error("--policy cannot be combined with --goal, --checks, --prepared-plan, or --max-replans")
        try:
            options = parse_policy(json.loads(args.policy.read_text())).agent_kwargs()
        except (ValueError, OSError):
            parser.error("Invalid policy file; nothing executed")
    else:
        if args.goal is None or args.checks is None:
            parser.error("Supply --policy or both --goal and --checks")
        checks = tuple(Check(**row) for row in json.loads(args.checks.read_text()))
        prepared = json.loads(args.prepared_plan.read_text()) if args.prepared_plan else None
        options = {"goal": args.goal, "checks": checks, "prepared_plan": prepared,
                   "max_replans": args.max_replans if args.max_replans is not None else 0}
    with ObjectiveAgent(args.url, **options) as agent:
        for state in agent.run():
            print(state["elapsed_ms"], "ms", state["status"], state["stop_reason"],
                  "planner calls:", len(state["planner_calls"]), "verification:", state["verification"], flush=True)
        return 0 if state["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
