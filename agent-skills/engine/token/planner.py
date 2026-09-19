"""Auditable context router with fail-to-full fallback."""
from __future__ import annotations

from typing import Any, Iterable

from .budget import DEFAULT_BUDGETS, ContextSource, allocate, normalize_sources
from .policy import evaluate_policy


class Planner:
    def __init__(self, budgets: dict[str, int] | None = None):
        self.budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
        if set(self.budgets) != set(DEFAULT_BUDGETS) or any(int(x) < 0 for x in self.budgets.values()):
            raise ValueError("budgets must define lite, standard, deep and emergency")

    def plan(self, task: Any, *, context: Iterable[Any] | None = None,
             budget: int | None = None, complexity: Any = None,
             required_sources: Iterable[str] = (),
             available_sources: Iterable[str] | None = None,
             conflicts: Iterable[str] = (),
             hard_constraints: Iterable[Any] = ()) -> dict[str, Any]:
        sources = normalize_sources(context)
        for index, constraint in enumerate(hard_constraints or ()):
            if isinstance(constraint, dict):
                item = {**constraint, "id": constraint.get("id", f"constraint-{index}"),
                        "protected": True, "required": True}
            else:
                item = ContextSource(f"constraint-{index}", estimate_constraint(constraint), True, True)
            sources.append(normalize_sources([item])[0])
        available = available_sources if available_sources is not None else [x.source_id for x in sources]
        policy = evaluate_policy(task, complexity=complexity, required_sources=required_sources,
                                 available_sources=available, conflicts=conflicts)
        mode = policy["mode"]
        requested = self.budgets[mode] if budget is None else int(budget)
        if policy["fail_to_full"]:
            mode = "emergency"
            requested = max(requested, self.budgets[mode])
        allocation = allocate(sources, requested)
        return {"mode": mode, "triggers": policy["triggers"],
                "protected_sources": [x.source_id for x in sources if x.protected or x.required],
                "budget": {"requested": requested, "limit": allocation["limit"],
                           "effective": allocation["effective"], "allocated": allocation["allocated"],
                           "protected_tokens": allocation["protected_tokens"],
                           "hard_constraints_preserved": allocation["hard_constraints_preserved"],
                           "included_sources": allocation["included"],
                           "dropped_sources": allocation["dropped"]},
                "fallback_reason": policy["fallback_reason"]}


def estimate_constraint(value: Any) -> int:
    return max(1, (len(str(value)) + 3) // 4)


def plan(task: Any, **kwargs: Any) -> dict[str, Any]:
    return Planner().plan(task, **kwargs)


build_plan = plan
