"""Call Allocator: reserves agent+borrower atomically, then talks to a provider.
It dials ONLY with a one-shot Approval from the Safety Controller."""
import random

from .telecom import ProviderError, ProviderTimeout
from .states import CallState as C


class WorkerCrash(Exception):
    """Raised by fault-injection hooks to simulate a hard worker crash."""


class CallAllocator:
    def __init__(self, store, providers: dict, safety, cfg, worker_id="w1", seed=0, weights=None):
        self.store, self.providers, self.safety, self.cfg = store, providers, safety, cfg
        self.worker_id, self.rng = worker_id, random.Random(seed)
        self.weights = weights or {n: 1.0 for n in providers}
        self.crash_at = None     # None | "after_reserve" | "after_place"  (failure demos/tests)

    def execute(self, approval) -> list:
        self.safety.consume(approval)
        refs = self.store.reserve_calls(approval.campaign_id, approval.count, approval.bound, self.worker_id)
        for ref in refs:
            if self.crash_at == "after_reserve":
                raise WorkerCrash("crashed after reserving agent+borrower, before dialing")
            self._place(ref)
        return [r.call_id for r in refs]

    def _pick(self, exclude):
        names = [n for n in self.providers if n not in exclude and self.safety.provider_allowed(n)]
        if not names:
            return None
        return self.providers[self.rng.choices(names, [self.weights[n] for n in names])[0]]

    def _place(self, ref):
        tried = set()
        while True:
            prov = self._pick(tried)
            if prov is None:
                self.store.apply_transition(ref.call_id, C.FAILED, cause="allocator", reason="provider_unavailable")
                return
            try:
                prov.place_call(ref.call_id, ref.phone)
            except ProviderError:
                self.safety.record_provider_result(prov.name, False)
                tried.add(prov.name)           # definite failure => safe to fail over
                continue
            except ProviderTimeout:
                # Ambiguous: the call may be live. Do NOT retry elsewhere (double-dial risk).
                self.safety.record_provider_result(prov.name, False)
                self.store.apply_transition(ref.call_id, C.INITIATED, cause="timeout_ambiguous", provider=prov.name,
                                            lease=self.cfg.ambiguous_lease)
                return
            self.safety.record_provider_result(prov.name, True)
            if self.crash_at == "after_place":
                raise WorkerCrash("crashed after provider accepted the call, before recording INITIATED")
            self.store.apply_transition(ref.call_id, C.INITIATED, cause="placed", provider=prov.name)
            return
