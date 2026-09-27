from .base import ChannelAdapter

class GoogleAdsAdapter(ChannelAdapter):
    name = "google"
    def health(self) -> dict:
        return {"channel": self.name, "status": "BLOCKED", "reason": "Credentials/API wiring not configured."}
    def fetch_performance(self, start_date: str, end_date: str):
        return []
    def fetch_catalog(self):
        return []
