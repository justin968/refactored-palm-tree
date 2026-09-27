import json
from pathlib import Path
from diy_growth.models import ActionClass, Channel
from diy_growth.policies import PolicyEngine

def test_writes_disabled_blocks_auto(tmp_path: Path):
    cfg = {"writes_enabled": False, "auto_eligible_enabled": False, "max_google_budget_change_pct_per_day": 15, "max_amazon_budget_change_pct_per_day": 15, "max_individual_bid_change_pct": 12, "max_aggregate_incremental_daily_spend_usd": 250, "max_auto_actions_per_day": 25, "minimum_conversions_for_scaling": 8, "minimum_observation_days": 7, "price_change_cooldown_days": 7, "creative_change_cooldown_days": 7, "listing_change_cooldown_days": 7}
    p = tmp_path / "policies.json"; p.write_text(json.dumps(cfg))
    d = PolicyEngine(p).classify_budget_change(Channel.GOOGLE, 10, 100, 20)
    assert not d.allowed
    assert d.action_class == ActionClass.APPROVAL_REQUIRED
