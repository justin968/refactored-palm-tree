from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Iterable

class ChannelAdapter(ABC):
    name: str

    @abstractmethod
    def health(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def fetch_performance(self, start_date: str, end_date: str) -> Iterable[dict]:
        raise NotImplementedError

    @abstractmethod
    def fetch_catalog(self) -> Iterable[dict]:
        raise NotImplementedError

    def apply_action(self, action: dict) -> dict:
        raise RuntimeError("Production writes are disabled unless explicitly implemented and owner-authorized.")
