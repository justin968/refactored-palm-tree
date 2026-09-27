from __future__ import annotations
import uuid
from .models import ActionClass, Channel, Opportunity, UnitEconomics

def scale_opportunity(*, entity: str, channel: Channel, current_daily_spend: float, proposed_daily_spend: float, conversions: int, current_roas: float, target_roas: float, unit_economics: UnitEconomics, confidence: float) -> Opportunity:
    pct = 0.0 if current_daily_spend == 0 else (proposed_daily_spend-current_daily_spend)/current_daily_spend
    delta_spend = proposed_daily_spend-current_daily_spend
    contribution_margin_rate = max(0.0, unit_economics.contribution_before_ads / max(unit_economics.selling_price, 1e-9))
    est_incremental_revenue = max(0.0, delta_spend * max(current_roas, 0.0) * 30)
    est_incremental_contribution = est_incremental_revenue * contribution_margin_rate - max(0.0, delta_spend*30)
    if current_roas < target_roas or est_incremental_contribution <= 0:
        klass = ActionClass.INVESTIGATE
        action = "Do not scale; diagnose efficiency and marginal-return curve."
    else:
        klass = ActionClass.APPROVAL_REQUIRED
        action = f"Increase daily spend from ${current_daily_spend:.2f} to ${proposed_daily_spend:.2f} after policy check."
    return Opportunity(id=str(uuid.uuid4()), entity=entity, channel=channel, opportunity_type="BUDGET_SCALE", observed_problem="Potentially underfunded profitable demand.", evidence={"current_daily_spend": current_daily_spend, "proposed_daily_spend": proposed_daily_spend, "pct_change": pct, "conversions": conversions, "current_roas": current_roas, "target_roas": target_roas, "break_even_roas": unit_economics.break_even_roas}, inferred_cause="Profitable demand may be constrained by current allocation.", recommended_action=action, expected_incremental_revenue=est_incremental_revenue, expected_incremental_contribution=est_incremental_contribution, confidence=confidence, action_class=klass, risk="low" if abs(pct) <= 0.15 else "medium", time_to_effect_days=7, reversibility="easy")

def rank(opportunities: list[Opportunity]) -> list[Opportunity]:
    return sorted(opportunities, key=lambda x: x.priority_score, reverse=True)
