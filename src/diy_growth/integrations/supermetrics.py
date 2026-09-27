from __future__ import annotations
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

BASE_URL = "https://api.supermetrics.com/enterprise/v2/query/data/keyjson"

@dataclass(frozen=True)
class SupermetricsQuery:
    ds_id: str
    account_id: str
    fields: list[str]
    start_date: str
    end_date: str
    settings: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "ds_id": self.ds_id,
            "ds_accounts": self.account_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "fields": ",".join(self.fields),
        }
        if self.settings:
            p["settings"] = self.settings
        return p

class SupermetricsClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("SUPERMETRICS_API_KEY")
        if not self.api_key:
            raise RuntimeError("SUPERMETRICS_API_KEY is not configured")

    def query(self, q: SupermetricsQuery, timeout: int = 120) -> list[dict[str, Any]]:
        body = json.dumps(q.payload()).encode("utf-8")
        req = urllib.request.Request(
            BASE_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        raise RuntimeError(f"Unexpected Supermetrics response shape: {type(data).__name__}")
