from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

class Channel(str, Enum):
    DIY = "diy"
    GOOGLE = "google"
    AMAZON = "amazon"

class DataStatus(str, Enum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    ESTIMATED = "ESTIMATED"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"

class ActionClass(str, Enum):
    AUTO_ELIGIBLE = "AUTO_ELIGIBLE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    OWNER_DECISION = "OWNER_DECISION"
    INVESTIGATE = "INVESTIGATE"

@dataclass(frozen=True)
class SourcedValue:
    value: Optional[float]
    source: str
    observed_at: str
    confidence: float = 1.0
    status: DataStatus = DataStatus.CURRENT
    estimated: bool = False

@dataclass(frozen=True)
class CanonicalProduct:
    sku: str
    woo_variation_id: Optional[str] = None
    merchant_offer_id: Optional[str] = None
    google_product_id: Optional[str] = None
    amazon_seller_sku: Optional[str] = None
    asin: Optional[str] = None
    mapping_status: DataStatus = DataStatus.CURRENT

@dataclass
class UnitEconomics:
    sku: str
    channel: Channel
    selling_price: float
    cogs: float
    packaging: float = 0.0
    fulfillment: float = 0.0
    outbound_shipping: float = 0.0
    shipping_revenue: float = 0.0
    marketplace_fee: float = 0.0
    payment_fee: float = 0.0
    refund_allowance: float = 0.0
    ad_spend: float = 0.0
    inventory_days: Optional[float] = None
    capacity_constraint: bool = False

    @property
    def contribution_before_ads(self) -> float:
        return self.selling_price + self.shipping_revenue - (self.cogs + self.packaging + self.fulfillment + self.outbound_shipping + self.marketplace_fee + self.payment_fee + self.refund_allowance)

    @property
    def contribution_after_ads(self) -> float:
        return self.contribution_before_ads - self.ad_spend

    @property
    def break_even_cac(self) -> float:
        return max(0.0, self.contribution_before_ads)

    @property
    def break_even_roas(self) -> Optional[float]:
        if self.break_even_cac <= 0:
            return None
        return self.selling_price / self.break_even_cac

@dataclass
class Opportunity:
    id: str
    entity: str
    channel: Channel
    opportunity_type: str
    observed_problem: str
    evidence: dict[str, Any]
    inferred_cause: str
    recommended_action: str
    expected_incremental_revenue: Optional[float]
    expected_incremental_contribution: Optional[float]
    confidence: float
    implementation_cost: float = 0.0
    risk: str = "low"
    time_to_effect_days: Optional[int] = None
    reversibility: str = "easy"
    action_class: ActionClass = ActionClass.INVESTIGATE
    dependencies: list[str] = field(default_factory=list)

    @property
    def priority_score(self) -> float:
        impact = self.expected_incremental_contribution or 0.0
        risk_multiplier = {"low": 1.0, "medium": 0.75, "high": 0.4}.get(self.risk, 0.5)
        return impact * self.confidence * risk_multiplier - self.implementation_cost
