from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable
from .models import Opportunity

@dataclass
class BacktestSummary:
    evaluated: int
    proposed: int
    estimated_incremental_contribution: float
    false_positive_ids: list[str]


def summarize_historical_opportunities(opportunities: Iterable[Opportunity], realized_contribution_by_id: dict[str, float]) -> BacktestSummary:
    ops = list(opportunities)
    proposed = [o for o in ops if (o.expected_incremental_contribution or 0) > 0 and o.confidence > 0]
    false_positives = [o.id for o in proposed if realized_contribution_by_id.get(o.id, 0.0) <= 0]
    return BacktestSummary(
        evaluated=len(ops),
        proposed=len(proposed),
        estimated_incremental_contribution=sum(o.expected_incremental_contribution or 0.0 for o in proposed),
        false_positive_ids=false_positives,
    )
