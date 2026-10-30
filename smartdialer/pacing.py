"""Pacing engines. They only *propose* a number; they hold no reference to the allocator,
the provider or the store's write API. Only the SafetyController can turn a proposal into
an Approval, and only an Approval can make the Allocator dial."""
import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PacingRequest:
    campaign_id: str
    mode: str                 # "PROGRESSIVE" | "PREDICTIVE"
    count: int
    explain: dict = field(default_factory=dict)


def wilson_upper(successes: int, n: int, z: float) -> float:
    if n == 0:
        return 1.0
    p = successes / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / d)


def tail_prob(n: int, k: int, p: float) -> float:
    """P(Binomial(n,p) > k): chance more people answer than we have agents."""
    if n <= k:
        return 0.0
    if p >= 1.0:
        return 1.0
    if p <= 0.0:
        return 0.0
    if k > 200:  # normal approx with continuity correction
        z = (k + 0.5 - n * p) / math.sqrt(n * p * (1 - p))
        return 0.5 * math.erfc(z / math.sqrt(2))
    lp, lq, cdf = math.log(p), math.log1p(-p), 0.0
    for i in range(k + 1):
        cdf += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    return max(0.0, 1.0 - cdf)


def max_dialable(k: int, p: float, eps: float, hard_cap: int) -> int:
    """Largest N in flight with P(answers > k) <= eps. Tail is monotone in N -> binary search."""
    lo, hi = k, max(k, hard_cap)
    if tail_prob(hi, k, p) <= eps:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tail_prob(mid, k, p) <= eps:
            lo = mid
        else:
            hi = mid - 1
    return lo


class ProgressivePacing:
    """One free agent -> one call (agent reserved at dial time)."""

    def request(self, snap, stats) -> PacingRequest:
        return PacingRequest(snap.campaign_id, "PROGRESSIVE", snap.free,
                             {"rule": "1 call per AVAILABLE agent", "free_agents": snap.free})


class PredictivePacing:
    def __init__(self, cfg, max_ratio=4.0):
        self.cfg, self.max_ratio = cfg, max_ratio

    def estimate_p(self, stats):
        """Conservative answer-rate estimate: the larger of the Wilson upper bounds from a
        short and a long window. Rate *rises* are caught fast by the short window; a rate
        *drop* is trusted slowly (we keep assuming the higher value) -> errs on the safe side."""
        cfg = self.cfg
        if len(stats.flags_long) < cfg.min_samples:
            return 1.0, 1.0, 1.0, "cold-start (<%d samples): assume everyone answers" % cfg.min_samples
        hs = wilson_upper(sum(stats.flags_short), len(stats.flags_short), cfg.z)
        hl = wilson_upper(sum(stats.flags_long), len(stats.flags_long), cfg.z)
        return max(hs, hl), hs, hl, "max(short,long) Wilson upper bound"

    def request(self, snap, stats) -> PacingRequest:
        p, hs, hl, why = self.estimate_p(stats)
        k = snap.free + snap.soon_free                     # agents able to take an answer
        cap = int(math.ceil(self.max_ratio * max(k, 1)))
        n_target = max_dialable(k, p, self.cfg.epsilon, cap)
        new = max(0, n_target - snap.unbound_preanswer)
        explain = {"p_used": round(p, 3), "p_short_hi": round(hs, 3), "p_long_hi": round(hl, 3),
                   "p_basis": why, "free": snap.free, "soon_free": snap.soon_free, "capacity_k": k,
                   "in_flight_unbound": snap.unbound_preanswer, "target_in_flight": n_target,
                   "avg_time_to_answer": round(stats.avg_time_to_answer, 1),
                   "tail_prob": round(tail_prob(n_target, k, p), 4),
                   "sentence": (f"{k} agents can take a call ({snap.free} free + {snap.soon_free} leaving wrap-up "
                                f"soon); assuming p<={p:.2f}, keeping {n_target} calls in flight holds "
                                f"P(answers>agents)<={self.cfg.epsilon:.0%}; {snap.unbound_preanswer} already "
                                f"in flight -> start {new} more")}
        return PacingRequest(snap.campaign_id, "PREDICTIVE", new, explain)
