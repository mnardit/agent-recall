"""Token budget system — hard caps with priority-based allocation.

Allocation algorithm:
1. Get scope's budget_tokens
2. Sort entities by priority
3. Fill: entity_name + slots(compact) → still room? → top-N observations
4. Truncate overflow with "...[truncated: N tokens remaining]"
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Priority tiers
PRIORITY_MUST = 1       # People, current project
PRIORITY_IMPORTANT = 2  # Topics, clients, decisions
PRIORITY_USEFUL = 3     # Relations, log entries
PRIORITY_NICE = 4       # Archived, cold tier


@dataclass
class BudgetConfig:
    """Token budget configuration.

    Args:
        default_budget: Default tokens per scope (4000).
        per_operation_caps: Operation-specific hard limits.
        truncation_marker: Suffix appended when output is truncated.
    """
    default_budget: int = 4000
    per_operation_caps: dict[str, int] = field(default_factory=lambda: {
        "search_nodes": 2000,
        "vector_search": 2000,
        "read_graph": 8000,
        "open_nodes": 4000,
        "get_context_compact": 500,
        "get_context_timeline": 3000,
        "get_context_full": 8000,
        "timeline": 3000,
        "session_start_inject": 4000,
    })
    truncation_marker: str = "\n...[truncated: {n} tokens remaining]"


class TokenBudget:
    """Manages token allocation for a scope.

    Usage::

        budget = TokenBudget(store, "my-project")
        ctx = budget.allocate(items, priority_map)
        # or
        result = budget.enforce("search_nodes", raw_result)
    """

    def __init__(
        self,
        store,  # MemoryStore (lazy import to avoid circular)
        scope: str,
        config: BudgetConfig | None = None,
    ) -> None:
        self._store = store
        self.scope = scope
        self.config = config or BudgetConfig()

    # ------------------------------------------------------------------
    # Estimation
    # ------------------------------------------------------------------

    @staticmethod
    def estimate(text: str) -> int:
        """Rough token count: chars/4. Works for Chinese + English."""
        if not text:
            return 0
        return max(1, len(text) // 4 + 1)

    # ------------------------------------------------------------------
    # Priority allocation
    # ------------------------------------------------------------------

    def allocate(
        self,
        items: list[dict],
        priority_map: dict[int, int],
    ) -> str:
        """Allocate token budget across items by priority.

        Args:
            items: List of {"text": ..., "priority": ...} dicts.
            priority_map: Maps item index to priority tier (1-4).

        Returns:
            Formatted context string, possibly truncated.
        """
        budget = self.get_budget()
        if budget <= 0:
            return ""

        # Sort by priority (lower number = higher priority)
        indexed = [(i, item) for i, item in enumerate(items)]
        indexed.sort(key=lambda x: priority_map.get(x[0], PRIORITY_NICE))

        parts: list[str] = []
        used = 0

        for i, item in indexed:
            text = item.get("text", "")
            cost = self.estimate(text)
            if used + cost > budget:
                remaining = budget - used
                if remaining > 20:  # Only add partial if meaningful
                    parts.append(text[: remaining * 4])
                marker = self.config.truncation_marker.format(
                    n=budget - used
                )
                parts.append(marker)
                break
            parts.append(text)
            used += cost

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Operation-level enforcement
    # ------------------------------------------------------------------

    def enforce(self, operation: str, result: str) -> str:
        """Apply per-operation hard cap to a result string.

        Args:
            operation: Operation name (must be a key in per_operation_caps
                       or falls back to default_budget).
            result: Raw result string.

        Returns:
            Truncated string if over cap, otherwise original.
        """
        cap = self.config.per_operation_caps.get(
            operation, self.config.default_budget
        )
        estimated = self.estimate(result)
        if estimated <= cap:
            return result
        # Truncate to ~cap tokens
        cutoff = cap * 4
        truncated = result[:cutoff]
        return truncated + self.config.truncation_marker.format(
            n=estimated - cap
        )

    # ------------------------------------------------------------------
    # Budget CRUD
    # ------------------------------------------------------------------

    def set_budget(self, scope: str, tokens: int) -> None:
        """Set token budget for a scope."""
        self._store.set_token_budget(scope, tokens)

    def get_budget(self) -> int:
        """Get token budget for current scope."""
        val = self._store.get_token_budget(self.scope)
        if val is None or val <= 0:
            return self.config.default_budget
        return val

    def reset_usage(self, scope: str) -> None:
        """Reset used_tokens counter for a scope."""
        conn = self._store._conn
        conn.execute(
            "UPDATE token_budgets SET used_tokens = 0, last_reset = datetime('now') "
            "WHERE scope = ?",
            (scope,),
        )
        conn.commit()
