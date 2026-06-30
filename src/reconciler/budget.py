"""Global ACU budget ceiling. ACU is real money, so dispatch stops before worst-case
spend could blow the ceiling, never after.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..store import Store


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str
    spent: float
    reserved: float
    ceiling: float


class BudgetGuard:
    def __init__(self, *, global_budget: float, max_acu_limit: int) -> None:
        self.global_budget = global_budget
        self.max_acu_limit = max_acu_limit

    def evaluate(self, store: Store) -> BudgetDecision:
        spent = store.total_acus()
        active = len(store.list_active_sessions())
        reserved = active * self.max_acu_limit
        # Worst case if every active session AND one new one each hit their cap.
        worst_case = spent + reserved + self.max_acu_limit
        allowed = worst_case <= self.global_budget
        reason = (
            "ok"
            if allowed
            else f"budget ceiling: worst-case {worst_case:.1f} ACU > {self.global_budget:.1f}"
        )
        return BudgetDecision(allowed, reason, spent, reserved, self.global_budget)
