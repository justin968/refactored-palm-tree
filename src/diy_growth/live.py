from __future__ import annotations
import os
from dataclasses import dataclass
from .integrations.supermetrics import SupermetricsClient, SupermetricsQuery
from .integrations.woocommerce import WooCommerceClient

PROFILES = {
    "google_campaigns": {
        "ds_id": "AW",
        "account_env": "SUPERMETRICS_GOOGLE_ADS_ACCOUNT",
        "fields": ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost", "Conversions", "ConversionValue"],
        "settings": {"report_type": "Campaign"},
    },
    "ga4_products": {
        "ds_id": "GAWA",
        "account_env": "SUPERMETRICS_GA4_ACCOUNT",
        "fields": ["date", "itemId", "itemName", "sessions", "itemViews", "itemsAddedToCart", "ecommercePurchases", "itemRevenue"],
        "settings": {},
    },
    "amazon_seller_asin": {
        "ds_id": "ASELL",
        "account_env": "SUPERMETRICS_AMAZON_SELLER_ACCOUNT",
        "fields": ["date", "sku", "asin", "title", "ordered_product_sales", "units_ordered", "sessions", "unit_session_percentage", "buy_box_percentage", "units_refunded"],
        "settings": {"report_type": "sales_and_traffic_by_asin"},
    },
    "amazon_orders": {
        "ds_id": "ASELL",
        "account_env": "SUPERMETRICS_AMAZON_SELLER_ACCOUNT",
        "fields": ["purchase_date", "sku", "asin", "product_name", "item_price", "shipping_price", "fulfillment_channel"],
        "settings": {"report_type": "orders"},
    },
}

@dataclass(frozen=True)
class SourceHealth:
    source: str
    status: str
    detail: str

def health() -> list[SourceHealth]:
    checks = []
    for name, profile in PROFILES.items():
        account = os.getenv(profile["account_env"])
        key = os.getenv("SUPERMETRICS_API_KEY")
        status = "READY" if key and account else "BLOCKED"
        missing = [x for x in ["SUPERMETRICS_API_KEY", profile["account_env"]] if not os.getenv(x)]
        checks.append(SourceHealth(name, status, "ok" if not missing else "missing: " + ", ".join(missing)))
    woo_missing = [x for x in ["WOO_SITE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET"] if not os.getenv(x)]
    checks.append(SourceHealth("woocommerce", "READY" if not woo_missing else "BLOCKED", "ok" if not woo_missing else "missing: " + ", ".join(woo_missing)))
    return checks

def pull_supermetrics(profile_name: str, start_date: str, end_date: str) -> list[dict]:
    profile = PROFILES[profile_name]
    account = os.getenv(profile["account_env"])
    if not account:
        raise RuntimeError(f"{profile['account_env']} is not configured")
    client = SupermetricsClient()
    return client.query(SupermetricsQuery(
        ds_id=profile["ds_id"],
        account_id=account,
        fields=profile["fields"],
        start_date=start_date,
        end_date=end_date,
        settings=profile.get("settings") or None,
    ))

def pull_woo_products() -> list[dict]:
    client = WooCommerceClient()
    rows = []
    page = 1
    while True:
        batch = client.products(page=page, per_page=100)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return rows
