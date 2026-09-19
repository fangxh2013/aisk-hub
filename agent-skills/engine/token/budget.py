"""Protected-first token budget allocation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_BUDGETS = {"lite": 1200, "standard": 4000, "deep": 8000, "emergency": 12000}


class BudgetError(ValueError):
    pass


@dataclass(frozen=True)
class ContextSource:
    source_id: str
    tokens: int
    protected: bool = False
    required: bool = False
    priority: int = 0

    def as_dict(self, *, included: bool = True) -> dict[str, Any]:
        return {"source": self.source_id, "tokens": self.tokens,
                "protected": self.protected, "required": self.required,
                "priority": self.priority, "included": included}


def estimate_tokens(value: Any, chars_per_token: int = 4) -> int:
    if chars_per_token < 1:
        raise BudgetError("chars_per_token must be positive")
    if isinstance(value, int):
        if value < 0:
            raise BudgetError("token count must not be negative")
        return value
    return max(1, (len(str(value)) + chars_per_token - 1) // chars_per_token)


def normalize_sources(sources: Iterable[Any] | None) -> list[ContextSource]:
    result = []
    for index, item in enumerate(sources or ()):
        if isinstance(item, ContextSource):
            source = item
        elif isinstance(item, dict):
            source_id = item.get("source", item.get("id", item.get("name", f"context-{index}")))
            tokens = item.get("tokens", estimate_tokens(item.get("content", "")))
            source = ContextSource(str(source_id), estimate_tokens(int(tokens)),
                                   bool(item.get("protected") or item.get("hard") or item.get("kind") == "protected"),
                                   bool(item.get("required")), int(item.get("priority", 0)))
        else:
            source = ContextSource(f"context-{index}", estimate_tokens(item))
        if not source.source_id or source.tokens < 0:
            raise BudgetError("context source must have an id and non-negative tokens")
        result.append(source)
    return result


def allocate(sources: Iterable[Any] | None, budget: int,
             protected_sources: Iterable[str] = ()) -> dict[str, Any]:
    if int(budget) < 0:
        raise BudgetError("budget must not be negative")
    protected_ids = {str(x) for x in protected_sources}
    items = []
    for item in normalize_sources(sources):
        if item.source_id in protected_ids and not item.protected:
            item = ContextSource(item.source_id, item.tokens, True, item.required, item.priority)
        items.append(item)
    protected = [x for x in items if x.protected or x.required]
    optional = [x for x in items if x not in protected]
    protected_tokens = sum(x.tokens for x in protected)
    limit = int(budget)
    effective = max(limit, protected_tokens)
    remaining = effective - protected_tokens
    included = list(protected)
    dropped = []
    for item in sorted(optional, key=lambda x: (-x.priority, x.source_id)):
        if item.tokens <= remaining:
            included.append(item)
            remaining -= item.tokens
        else:
            dropped.append(item)
    included_ids = {x.source_id for x in included}
    return {"limit": limit, "effective": effective,
            "allocated": sum(x.tokens for x in included),
            "protected_tokens": protected_tokens,
            "hard_constraints_preserved": all(x.source_id in included_ids for x in protected),
            "included": [x.as_dict() for x in included],
            "dropped": [x.as_dict(included=False) for x in dropped]}


def allocate_budget(sources: Iterable[Any] | None, budget: int,
                   protected_sources: Iterable[str] = ()) -> dict[str, Any]:
    return allocate(sources, budget, protected_sources)
