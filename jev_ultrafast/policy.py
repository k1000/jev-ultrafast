"""Closed caller-owned policy data; the existing ObjectiveAgent remains the runtime."""

from dataclasses import asdict, dataclass

from .planning import Check, Plan, parse_plan


@dataclass(frozen=True)
class Policy:
    objective: str
    checks: tuple[Check, ...]
    prepared_plan: Plan | None

    def agent_kwargs(self):
        """Fresh arguments for the existing controller; policy execution never enables replanning."""
        prepared = None
        if self.prepared_plan is not None:
            rows = []
            for step in self.prepared_plan.steps:
                row = asdict(step)
                row["texts"], row["checks"] = list(row["texts"]), list(row["checks"])
                rows.append(row)
            prepared = {"objective": self.objective, "plan": rows}
        return {"goal": self.objective, "checks": self.checks, "prepared_plan": prepared, "max_replans": 0}


def parse_policy(data):
    """Validate the v1 envelope without browser/model calls; retain only immutable records."""
    try:
        if not isinstance(data, dict) or set(data) != {"version", "objective", "checks", "prepared_plan"}:
            raise ValueError()
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError()
        objective = data["objective"]
        if (not isinstance(objective, str) or not objective or objective != objective.strip()
                or len(objective) > 2000):
            raise ValueError()
        if not isinstance(data["checks"], list) or not data["checks"]:
            raise ValueError()
        checks = tuple(Check(**record) for record in data["checks"])
        plan = parse_plan(data["prepared_plan"], objective=objective) if data["prepared_plan"] is not None else None
        return Policy(objective, checks, plan)
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid declarative policy; nothing executed") from None
