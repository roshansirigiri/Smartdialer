"""Safety Controller: the only component that can mint an Approval, and the Allocator
refuses to dial without one. Pacing engines cannot reach the allocator or disable this."""
import math
import secrets
from collections import deque
from dataclasses import dataclass

from .config import SafetyConfig
from .pacing import max_dialable, wilson_upper


class SafetyViolation(Exception):
    pass


@dataclass(frozen=True)
class Approval:
    campaign_id: str
    count: int
    bound: bool              # True: each call reserves an agent up-front (progressive-safe)
    action: str              # APPROVE | REDUCE | REJECT | FALLBACK_PROGRESSIVE
    requested: int
    reasons: tuple
    nonce: str


class ProviderHealth:
    """Per-provider circuit breaker: CLOSED -> OPEN (too many failures) -> HALF_OPEN (one probe)."""

    def __init__(self, cfg: SafetyConfig):
        self.cfg, self.results, self.opened_at, self.probing = cfg, deque(maxlen=cfg.breaker_window), None, False

    def record(self, ok, now):
        if self.opened_at is not None:          # result of the half-open probe
            if ok:
                self.opened_at, self.probing = None, False
                self.results.clear()
            else:
                self.opened_at, self.probing = now, False
            return
        self.results.append(ok)
        c = self.cfg
        if len(self.results) >= c.breaker_min_calls and \
                self.results.count(False) / len(self.results) >= c.breaker_fail_ratio:
            self.opened_at = now

    def state(self, now):
        if self.opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if now - self.opened_at >= self.cfg.breaker_open_secs else "OPEN"

    def allow(self, now):
        s = self.state(now)
        if s == "CLOSED":
            return True
        if s == "HALF_OPEN" and not self.probing:
            self.probing = True                  # exactly one probe call
            return True
        return False


class SafetyController:
    def __init__(self, store, cfg: SafetyConfig, clock):
        self.store, self.cfg, self.clock = store, cfg, clock
        self.health, self._issued = {}, {}

    # ------------------------------------------------------------ provider health
    def breaker(self, name) -> ProviderHealth:
        return self.health.setdefault(name, ProviderHealth(self.cfg))

    def record_provider_result(self, name, ok):
        self.breaker(name).record(ok, self.clock.now())

    def provider_allowed(self, name) -> bool:
        return self.breaker(name).allow(self.clock.now())

    def providers_usable(self, names) -> bool:
        now = self.clock.now()
        return any(self.breaker(n).state(now) != "OPEN" for n in names)

    # ------------------------------------------------------------ abandon-rate governor
    def in_fallback(self, campaign, stats) -> bool:
        key, now, cfg = f"fallback:{campaign}", self.clock.now(), self.cfg
        since = self.store.kv_get(key)          # persisted so every worker agrees
        trusted = stats.answered_window >= cfg.min_window
        if since is None:
            if trusted and stats.abandon_rate >= cfg.abandon_hard:
                self.store.kv_set(key, now)
                return True
            return False
        if now - since >= cfg.fallback_cooldown and trusted and stats.abandon_rate <= cfg.abandon_recover:
            self.store.kv_set(key, None)
            return False
        return True

    # ------------------------------------------------------------ the gate
    def review(self, req, snap, stats, campaign_active=True, provider_names=()) -> Approval:
        cfg, reasons, bound = self.cfg, [], req.mode == "PROGRESSIVE"
        n, action = max(0, req.count), "APPROVE"

        def issue(count, act):
            ap = Approval(req.campaign_id, int(count), bound, act, req.count, tuple(reasons), secrets.token_hex(8))
            self._issued[ap.nonce] = ap
            return ap

        if not campaign_active:
            reasons.append("campaign not active")
            return issue(0, "REJECT")
        if provider_names and not self.providers_usable(provider_names):
            reasons.append("all providers OPEN (circuit breaker)")
            return issue(0, "REJECT")
        if req.mode == "PREDICTIVE" and self.in_fallback(req.campaign_id, stats):
            bound, action = True, "FALLBACK_PROGRESSIVE"
            n = max(0, snap.free - snap.unbound_preanswer)     # every in-flight call has a free agent behind it
            reasons.append(f"abandon rate {stats.abandon_rate:.1%} >= {cfg.abandon_hard:.0%}: progressive fallback")
        elif bound:
            n = min(n, snap.free)                                # hard invariant: calls <= free agents
        else:
            if stats.answered_window >= cfg.min_window and stats.abandon_rate >= cfg.abandon_soft:
                n //= 2
                reasons.append(f"abandon rate {stats.abandon_rate:.1%} >= soft limit: halve overdial")
            cap = self.max_unbound(snap) - snap.unbound_preanswer
            if n > cap:
                n = max(0, cap)
                reasons.append(f"overdial ratio cap {cfg.max_overdial_ratio}x capacity")
        if bound:
            n = min(n, snap.free)
        if n > cfg.max_calls_per_tick:
            n = cfg.max_calls_per_tick
            reasons.append("per-tick cap")
        if n > snap.ready_borrowers:
            n = snap.ready_borrowers
            reasons.append("fewer borrowers than requested")
        if provider_names and any(self.breaker(p).state(self.clock.now()) == "HALF_OPEN" for p in provider_names) \
                and not any(self.breaker(p).state(self.clock.now()) == "CLOSED" for p in provider_names):
            n, bound = min(n, 1), True
            reasons.append("half-open: single probe call")
        if action == "APPROVE" and n < req.count:
            action = "REDUCE" if n > 0 else "REJECT"
        if req.count == 0 and n == 0:
            action = "APPROVE"
        return issue(n, action)

    def max_unbound(self, snap) -> int:
        k = snap.free + snap.soon_free
        return int(math.ceil(self.cfg.max_overdial_ratio * k))

    def excess_unbound(self, snap, stats) -> int:
        """Pre-answer unbound calls to cancel *now*. Safety computes its own conservative
        answer-rate bound (it does not trust pacing's) and cancels until
        P(answers > agents able to take them) <= reconcile_epsilon again."""
        cfg, k = self.cfg, snap.free + snap.soon_free
        if self.store.kv_get(f"fallback:{snap.campaign_id}") is not None:
            allowed = k                       # fallback: every in-flight call has an agent behind it
        elif k == 0:
            allowed = 0
        elif len(stats.flags_long) < cfg.min_samples:
            allowed = k
        else:
            p = max(wilson_upper(sum(stats.flags_short), len(stats.flags_short), cfg.z),
                    wilson_upper(sum(stats.flags_long), len(stats.flags_long), cfg.z))
            allowed = max_dialable(k, p, cfg.reconcile_epsilon, self.max_unbound(snap))
        return max(0, snap.unbound_preanswer - allowed)

    def consume(self, ap: Approval) -> None:
        """One-shot: an Approval is valid only if this controller issued it and it was never used."""
        if not isinstance(ap, Approval) or self._issued.get(ap.nonce) != ap:
            raise SafetyViolation("dial attempt without a valid Safety Controller approval")
        del self._issued[ap.nonce]
