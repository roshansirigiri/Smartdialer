import time


class Clock:
    def now(self) -> float:
        return time.time()


class VirtualClock(Clock):
    """Deterministic clock for simulation and tests."""

    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt
