"""Wires a complete dialer for tests / simulation / load tests."""
import os
import tempfile
from dataclasses import dataclass

from .allocator import CallAllocator
from .clock import VirtualClock
from .config import Config, PacingConfig, SafetyConfig
from .dialer import Dialer
from .pacing import PredictivePacing, ProgressivePacing
from .safety import SafetyController
from .store import Store
from .telecom import Behavior, provider_a, provider_b


@dataclass
class Rig:
    clock: VirtualClock
    store: Store
    providers: dict
    safety: SafetyController
    allocator: CallAllocator
    dialer: Dialer
    cfg: Config
    campaign: str = "c1"

    def step(self, n=1, dt=1.0):
        for _ in range(n):
            self.dialer.tick()
            self.clock.advance(dt)

    def calls(self, state=None):
        if state:
            return self.store.q("SELECT * FROM calls WHERE state=?", (state,))
        return self.store.q("SELECT * FROM calls")


def build(agents=20, borrowers=2000, mode="predictive", answer_rate=0.3, talk=60.0, cfg=None,
          scfg=None, pcfg=None, providers=("A", "B"), seed=1, behavior=None, fast=True, path=None,
          weights=None, login=True) -> Rig:
    clock, cfg = VirtualClock(), cfg or Config()
    scfg, pcfg = scfg or SafetyConfig(), pcfg or PacingConfig()
    path = path or os.path.join(tempfile.mkdtemp(prefix="sd_"), "dialer.db")
    store = Store(path, clock, cfg, fast=fast)
    store.add_agents("c1", agents, login=login)
    store.add_borrowers("c1", borrowers)
    beh = behavior or Behavior(lambda t: answer_rate, lambda t: talk)
    makers = {"A": provider_a, "B": provider_b}
    provs = {n: makers[n](clock, beh, seed=seed + i) for i, n in enumerate(providers)}
    safety = SafetyController(store, scfg, clock)
    alloc = CallAllocator(store, provs, safety, cfg, seed=seed, weights=weights)
    engine = ProgressivePacing() if mode == "progressive" else PredictivePacing(pcfg, scfg.max_overdial_ratio)
    dialer = Dialer("c1", store, engine, safety, alloc, provs, cfg, clock)
    return Rig(clock, store, provs, safety, alloc, dialer, cfg)
