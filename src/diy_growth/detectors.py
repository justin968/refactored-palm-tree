from __future__ import annotations
import json
import uuid
from collections import defaultdict
from pathlib import Path
from .models import ActionClass, Channel, Opportunity

def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0

def load_policy(path: str | Path = "config/policies.json") -> dict:
    return json.loads(Path(path).read_text())

def detect_search_term_waste(rows: list[dict], channel: Channel, policy: dict) -> list[Opportunity]:
    out = []
    threshold = float(policy["waste_term_spend_threshold_usd"])
    min_clicks = int(policy["minimum_clicks_for_negative"])
    for r in rows:
        if channel == Channel.GOOGLE:
            spend, conv, sales = _num(r.get("Cost")), _num(r.get("Conversions")), _num(r.get("ConversionValue"))
            query = str(r.get("Searchterm") or "").strip()
            clicks = _num(r.get("Clicks"))
            entity = f"{r.get('CampaignID','')}:{r.get('AdgroupID','')}:{query}"
        else:
            spend, conv, sales = _num(r.get("cost")), _num(r.get("orders")), _num(r.get("sales"))
            query = str(r.get("query") or "").strip()
            clicks = _num(r.get("clicks"))
            entity = f"{r.get('campaignId','')}:{r.get('adGroupId','')}:{query}"
        if not query or spend < threshold or clicks < min_clicks or conv > 0 or sales > 0:
            continue
        out.append(Opportunity(
            id=str(uuid.uuid4()),
            entity=entity,
            channel=channel,
            opportunity_type="SEARCH_TERM_WASTE",
            observed_problem=f"Search term spent ${spend:.2f} with no attributed orders/conversions.",
            evidence={"query": query, "spend": spend, "clicks": clicks, "conversions": conv, "sales": sales},
            inferred_cause="Query has accumulated material spend without observed purchase value in the measurement window.",
            recommended_action=f"Review and, if semantically irrelevant or repeatedly unproductive, add '{query}' as a negative target.",
            expected_incremental_revenue=0.0,
            expected_incremental_contribution=spend,
            confidence=min(0.98, 0.65 + min(spend / max(threshold, 1), 2) * 0.12),
            risk="medium",
            reversibility="easy",
            action_class=ActionClass.APPROVAL_REQUIRED,
        ))
    return out

def detect_campaign_scale(rows: list[dict], channel: Channel, policy: dict) -> list[Opportunity]:
    grouped: dict[str, dict] = defaultdict(lambda: {"spend":0.0,"sales":0.0,"conversions":0.0,"name":"","budget":0.0})
    for r in rows:
        if channel == Channel.GOOGLE:
            cid = str(r.get("CampaignID") or "")
            g = grouped[cid]
            g["name"] = r.get("Campaignname") or cid
            g["spend"] += _num(r.get("Cost"))
            g["sales"] += _num(r.get("ConversionValue"))
            g["conversions"] += _num(r.get("Conversions"))
            g["budget"] = max(g["budget"], _num(r.get("dailybudget")))
        else:
            cid = str(r.get("campaignId") or "")
            g = grouped[cid]
            g["name"] = r.get("campaignName") or cid
            g["spend"] += _num(r.get("cost"))
            g["sales"] += _num(r.get("sales"))
            g["conversions"] += _num(r.get("orders"))
            g["budget"] = max(g["budget"], _num(r.get("campaignBudget")))
    target = float(policy["target_google_roas"] if channel == Channel.GOOGLE else policy["target_amazon_roas"])
    min_conv = int(policy["minimum_conversions_for_scaling"])
    headroom = float(policy["scale_roas_headroom"])
    out = []
    for cid, g in grouped.items():
        if not cid or g["spend"] <= 0:
            continue
        roas = g["sales"] / g["spend"]
        if g["conversions"] < min_conv or roas < target * headroom:
            continue
        proposed = round(g["budget"] * 1.10, 2) if g["budget"] > 0 else None
        action = f"Test a 10% budget increase for {g['name']}" if proposed else f"Review {g['name']} for controlled budget expansion"
        out.append(Opportunity(
            id=str(uuid.uuid4()),
            entity=cid,
            channel=channel,
            opportunity_type="BUDGET_SCALE",
            observed_problem="Campaign is materially above configured ROAS target with adequate conversion volume.",
            evidence={"campaign":g["name"],"spend":g["spend"],"sales":g["sales"],"conversions":g["conversions"],"roas":roas,"target_roas":target,"current_budget":g["budget"],"proposed_budget":proposed},
            inferred_cause="The campaign may have profitable marginal demand available, but marginal ROAS is not yet proven.",
            recommended_action=action,
            expected_incremental_revenue=None,
            expected_incremental_contribution=None,
            confidence=0.72,
            risk="low",
            reversibility="easy",
            action_class=ActionClass.APPROVAL_REQUIRED,
        ))
    return out
