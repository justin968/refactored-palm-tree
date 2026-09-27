from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from .models import ActionClass, Channel

@dataclass
class PolicyDecision:
    allowed: bool
    action_class: ActionClass
    reason: str

class PolicyEngine:
    def __init__(self, config_path: str | Path = "config/policies.json"):
        self.config = json.loads(Path(config_path).read_text())

    def classify_budget_change(self, channel: Channel, pct_change: float, incremental_daily_spend: float, conversions: int) -> PolicyDecision:
        if channel not in {Channel.GOOGLE, Channel.AMAZON}:
            return PolicyDecision(False, ActionClass.INVESTIGATE, "Unsupported ad channel.")
        if conversions < self.config["minimum_conversions_for_scaling"]:
            return PolicyDecision(False, ActionClass.INVESTIGATE, "Insufficient conversion evidence.")
        limit = self.config["max_google_budget_change_pct_per_day"] if channel == Channel.GOOGLE else self.config["max_amazon_budget_change_pct_per_day"]
        if abs(pct_change) > limit:
            return PolicyDecision(False, ActionClass.APPROVAL_REQUIRED, f"Budget change exceeds {limit}% policy limit.")
        if incremental_daily_spend > self.config["max_aggregate_incremental_daily_spend_usd"]:
            return PolicyDecision(False, ActionClass.APPROVAL_REQUIRED, "Incremental daily spend exceeds portfolio limit.")
        if not self.config.get("auto_eligible_enabled", False):
            return PolicyDecision(False, ActionClass.APPROVAL_REQUIRED, "Autonomous actions are not owner-authorized.")
        if not self.config.get("writes_enabled", False):
            return PolicyDecision(False, ActionClass.APPROVAL_REQUIRED, "Production writes are globally disabled.")
        return PolicyDecision(True, ActionClass.AUTO_ELIGIBLE, "Within owner-authorized autonomous guardrails.")
