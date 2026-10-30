"""Lease-based recovery. Any worker may run this; it is idempotent. It resolves calls whose lease
expired (crashed worker, lost webhook, ambiguous timeout) by asking the provider what really
happened, never by guessing."""
from .events import EVENT_TO_STATE
from .telecom import ProviderTimeout
from .states import CallState as C

ORDER = [C.INITIATED, C.RINGING, C.ANSWERED]


def sweep(store, providers: dict, cfg, now=None):
    now = now if now is not None else store.clock.now()
    stats = {"adopted": 0, "failed": 0, "deferred": 0, "repaired": 0}
    for call in store.expired_calls(now):
        cid, state = call["id"], C(call["state"])
        names = [call["provider"]] if call["provider"] else list(providers)
        status, found, unreachable = None, None, False
        for n in names:
            try:
                s = providers[n].get_status(cid)
            except ProviderTimeout:
                unreachable = True
                continue
            if s:
                status, found = s, n
                break
        if status is None:
            if unreachable and now - call["lease_until"] < cfg.setup_lease:
                store.extend_lease(cid, cfg.ambiguous_lease)       # can't tell yet: ask again soon
                stats["deferred"] += 1
                continue
            reason = "worker_crash" if state == C.RESERVED else "setup_timeout"
            store.apply_transition(cid, C.FAILED, cause="recovery", reason=reason)
            stats["failed"] += 1
            continue
        if status in ("INITIATED", "RINGING", "ANSWERED") and state in (C.RESERVED, C.INITIATED, C.RINGING, C.ANSWERED):
            if status == "RINGING" and call["ringing_at"] is not None and state in (C.INITIATED, C.RINGING) \
                    and now - (call["initiated_at"] or now) > cfg.setup_lease:
                try:
                    providers[found].cancel(cid)
                finally:
                    store.apply_transition(cid, C.FAILED, cause="recovery", reason="setup_timeout")
                stats["failed"] += 1
                continue
            # adopt provider truth: walk the call forward through every state it skipped
            for st in ORDER[:ORDER.index(C(status)) + 1]:
                store.apply_transition(cid, st, cause="recovery", provider=found)
            store.extend_lease(cid, cfg.setup_lease)
            stats["adopted"] += 1
        elif status in EVENT_TO_STATE:
            store.apply_transition(cid, EVENT_TO_STATE[status], cause="recovery", provider=found,
                                   reason=status if status in ("NO_ANSWER", "BUSY", "FAILED") else None)
            stats["adopted"] += 1
    stats["repaired"] = store.repair_orphans()
    return stats
