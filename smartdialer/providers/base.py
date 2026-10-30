"""The only thing the dialer knows about a telecom provider.

Contract (deliberately pessimistic):
  * place_call is idempotent on idem_key (we pass our call_id). A retry never double-dials.
  * place_call / lookup / hangup / poll_events may raise ProviderTimeout; after a timeout the
    outcome is UNKNOWN (the call may or may not exist) -> resolve with lookup(idem_key).
  * Events are at-least-once, unordered, and may repeat. They stay in the provider's queue until ack().
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import ProviderEvent


class ProviderError(Exception):
    pass


class ProviderTimeout(ProviderError):
    """Outcome unknown."""


class ProviderRejected(ProviderError):
    """Definitely not placed."""


class TelecomProvider(ABC):
    name: str

    @abstractmethod
    def place_call(self, idem_key: str, phone: str, now: float) -> str: ...

    @abstractmethod
    def lookup(self, idem_key: str) -> Optional[str]: ...

    @abstractmethod
    def hangup(self, provider_call_id: str, now: float) -> None: ...

    @abstractmethod
    def poll_events(self, now: float) -> list[ProviderEvent]: ...

    @abstractmethod
    def ack(self, event_ids: list[str]) -> None: ...
