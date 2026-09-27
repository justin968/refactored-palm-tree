from .base import ChannelAdapter

class AmazonAdapter(ChannelAdapter):
    name = "amazon"
    def health(self) -> dict:
        return {"channel": self.name, "status": "BLOCKED", "reason": "Amazon Ads and Seller/SP-API credentials/API wiring not configured."}
    def fetch_performance(self, start_date: str, end_date: str):
        return []
    def fetch_catalog(self):
        return []
