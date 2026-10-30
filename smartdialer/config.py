from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- lifecycle / leases (seconds) ---
    wrap_up_secs: float = 10.0
    reserve_lease: float = 10.0       # RESERVED -> must reach INITIATED by then, else recovery
    setup_lease: float = 60.0         # INITIATED/RINGING max setup time
    ambiguous_lease: float = 5.0      # provider timed out on place_call: probe soon
    max_call_secs: float = 3600.0     # CONNECTED safety net
    hold_secs: float = 2.0            # answered but no agent: max hold before abandon
    # --- borrower retry policy ---
    max_attempts: int = 3
    retry_delay: float = 300.0        # after no-answer / busy
    infra_retry_delay: float = 3.0    # after infra failure (attempt not consumed)
    abandon_callback_delay: float = 60.0
    # --- housekeeping ---
    recovery_interval: float = 2.0
    stats_window_long: int = 200
    stats_window_short: int = 40


@dataclass(frozen=True)
class SafetyConfig:
    abandon_soft: float = 0.02        # above: halve overflow dialing
    abandon_hard: float = 0.03        # at/above: fall back to progressive
    abandon_recover: float = 0.015    # below (and cooled down): leave fallback
    abandon_window: int = 100         # last N answered calls
    min_window: int = 30              # need this many answers to trust the rate
    fallback_cooldown: float = 30.0
    max_overdial_ratio: float = 4.0   # unbound in-flight <= ratio * capacity
    max_calls_per_tick: int = 200
    max_inflight: int = 100_000
    reconcile_epsilon: float = 0.10   # safety's own overflow budget (looser than pacing: hysteresis)
    z: float = 1.64
    min_samples: int = 30
    breaker_window: int = 20
    breaker_min_calls: int = 8
    breaker_fail_ratio: float = 0.5
    breaker_open_secs: float = 15.0


@dataclass(frozen=True)
class PacingConfig:
    epsilon: float = 0.05             # P(answers > free agents) budget
    min_samples: int = 30             # below: assume p=1 (== progressive)
    z: float = 1.64                   # Wilson upper-bound z (95% one-sided)
    soon_free_horizon_cap: float = 15.0
