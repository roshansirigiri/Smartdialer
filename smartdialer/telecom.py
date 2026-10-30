"""Telecom provider interface + two deliberately different mock providers.
The dialer only ever sees `Provider`; it never learns how a provider misbehaves."""
import heapq
import random
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


class ProviderError(Exception):
    """Definite failure: the provider did NOT place the call. Safe to retry elsewhere."""


class ProviderTimeout(Exception):
    """Ambiguous: the call MAY have been placed. Never blindly retry; probe get_status()."""


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    event_id: str
    call_id: str
    type: str       # RINGING | ANSWERED | COMPLETED | NO_ANSWER | BUSY | FAILED
    ts: float


class Provider(ABC):
    name: str

    @abstractmethod
    def place_call(self, call_id: str, phone: str) -> None:
        """Idempotent on call_id (our id is the provider's client reference)."""

    @abstractmethod
    def cancel(self, call_id: str) -> None: ...

    @abstractmethod
    def get_status(self, call_id: str) -> Optional[str]:
        """None => provider has never heard of call_id."""

    @abstractmethod
    def poll_events(self) -> list: ...   # a real adapter would be a webhook handler


@dataclass
class Behavior:
    """What borrowers do. Rates/talk times may be functions of sim time -> scenario D."""
    answer_rate: Callable[[float], float]
    talk_time: Callable[[float], float]
    t0: float = 0.0

    def sample(self, now, rng):
        t = now - self.t0
        answers = rng.random() < self.answer_rate(t)
        mean = max(5.0, self.talk_time(t))
        talk = max(3.0, rng.expovariate(1.0 / mean)) if answers else 0.0
        ring = rng.uniform(4, 14) if answers else rng.uniform(12, 28)
        return answers, ring, talk, ("BUSY" if rng.random() < 0.15 else "NO_ANSWER")


class MockProvider(Provider):
    def __init__(self, name, clock, behavior: Behavior, seed=0, *, latency=(0.3, 1.0), failure_rate=0.0,
                 timeout_rate=0.0, ambiguous_ratio=0.5, dup_rate=0.0, reorder_rate=0.0, reorder_delay=4.0):
        self.name, self.clock, self.behavior = name, clock, behavior
        self.rng = random.Random(seed)
        self.latency, self.failure_rate, self.timeout_rate = latency, failure_rate, timeout_rate
        self.ambiguous_ratio, self.dup_rate = ambiguous_ratio, dup_rate
        self.reorder_rate, self.reorder_delay = reorder_rate, reorder_delay
        self.outage = False
        self.calls = {}      # call_id -> timeline
        self.heap, self.seq = [], 0
        self.lock = threading.Lock()
        self.placed = 0

    # --- fault controls used by scenarios
    def set_outage(self, on: bool):
        self.outage = on

    # --- interface
    def place_call(self, call_id, phone):
        with self.lock:
            if call_id in self.calls:
                return                                   # idempotent
            if self.outage:
                raise ProviderTimeout(f"{self.name} outage")
            r = self.rng.random()
            if r < self.failure_rate:
                raise ProviderError("rejected")
            if r < self.failure_rate + self.timeout_rate:
                if self.rng.random() < self.ambiguous_ratio:
                    self._start(call_id)                 # call really went out, caller never learns
                raise ProviderTimeout("timeout")
            self._start(call_id)

    def _start(self, call_id):
        now = self.clock.now()
        answers, ring, talk, fail = self.behavior.sample(now, self.rng)
        lat = self.rng.uniform(*self.latency)
        tl = {"t_ring": now + lat, "answers": answers, "cancelled": False}
        if answers:
            tl.update(t_answer=now + lat + ring, t_end=now + lat + ring + talk, end="COMPLETED")
        else:
            tl.update(t_answer=None, t_end=now + lat + ring, end=fail)
        self.calls[call_id] = tl
        self.placed += 1
        self._emit(call_id, "RINGING", tl["t_ring"])
        if answers:
            self._emit(call_id, "ANSWERED", tl["t_answer"])
        self._emit(call_id, tl["end"], tl["t_end"])

    def _emit(self, call_id, typ, at):
        ev_id = f"{self.name}:{call_id}:{typ}"
        t = at + (self.rng.uniform(0, self.reorder_delay) if self.rng.random() < self.reorder_rate else 0)
        self._push(t, ProviderEvent(self.name, ev_id, call_id, typ, at))
        if self.rng.random() < self.dup_rate:
            for _ in range(self.rng.choice([1, 2])):
                self._push(t + self.rng.uniform(0, 2), ProviderEvent(self.name, ev_id, call_id, typ, at))

    def _push(self, t, ev):
        self.seq += 1
        heapq.heappush(self.heap, (t, self.seq, ev))

    def cancel(self, call_id):
        with self.lock:
            tl = self.calls.get(call_id)
            if tl:
                tl["cancelled"] = True
                self.heap = [h for h in self.heap if h[2].call_id != call_id]
                heapq.heapify(self.heap)

    def get_status(self, call_id):
        with self.lock:
            if self.outage:
                raise ProviderTimeout("status timeout")
            tl = self.calls.get(call_id)
            if tl is None:
                return None
            if tl["cancelled"]:
                return "CANCELLED"
            now = self.clock.now()
            if now >= tl["t_end"]:
                return tl["end"]
            if tl["answers"] and now >= tl["t_answer"]:
                return "ANSWERED"
            return "RINGING" if now >= tl["t_ring"] else "INITIATED"

    def poll_events(self):
        out, now = [], self.clock.now()
        with self.lock:
            while self.heap and self.heap[0][0] <= now:
                out.append(heapq.heappop(self.heap)[2])
        return out


def provider_a(clock, behavior, seed=1):
    """Fast, reliable, clean event stream."""
    return MockProvider("A", clock, behavior, seed, latency=(0.2, 0.8), failure_rate=0.005)


def provider_b(clock, behavior, seed=2):
    """Slower, rejects/timeouts (half of timeouts are ambiguous), duplicate and reordered events."""
    return MockProvider("B", clock, behavior, seed, latency=(1.0, 3.5), failure_rate=0.02, timeout_rate=0.05,
                        dup_rate=0.2, reorder_rate=0.25, reorder_delay=5.0)
