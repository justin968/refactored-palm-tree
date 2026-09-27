from __future__ import annotations
import base64
import json
import os
import urllib.parse
import urllib.request
from typing import Any

class WooCommerceClient:
    def __init__(self, site_url: str | None = None, consumer_key: str | None = None, consumer_secret: str | None = None):
        self.site_url = (site_url or os.getenv("WOO_SITE_URL") or "").rstrip("/")
        self.consumer_key = consumer_key or os.getenv("WOO_CONSUMER_KEY")
        self.consumer_secret = consumer_secret or os.getenv("WOO_CONSUMER_SECRET")
        if not self.site_url or not self.consumer_key or not self.consumer_secret:
            raise RuntimeError("WooCommerce API credentials are not configured")

    def get(self, route: str, params: dict[str, Any] | None = None, timeout: int = 120) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.site_url}/wp-json/wc/v3/{route.lstrip('/')}"
        if query:
            url += "?" + query
        token = base64.b64encode(f"{self.consumer_key}:{self.consumer_secret}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def products(self, page: int = 1, per_page: int = 100):
        return self.get("products", {"page": page, "per_page": per_page})

    def orders(self, after: str, before: str, page: int = 1, per_page: int = 100):
        return self.get("orders", {"after": after, "before": before, "page": page, "per_page": per_page})
