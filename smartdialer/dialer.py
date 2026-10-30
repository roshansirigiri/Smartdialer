"""One dialer worker. Stateless between ticks: everything it needs is re-read from the store,
so N identical workers can run against the same campaign (see docs/ARCHITECTURE.md)."""
from . import recovery
from .events import handle_event


class Dialer:
    def __init__(self, campaign, store, engine, safety, allocator, providers, cfg, clock):
        self.campaign, self.store, self.engine, self.safety = campaign, store, engine, safety
        self.allocator, self.providers, self.cfg, self.clock = allocator, providers, cfg, clock
        self.active = True
        self._last_recovery = -1e18
        self.last = {}          # last snapshot / request / approval, for observability

    def tick(self):
        st, now = self.store, self.clock.now()
        # 1. ingest provider events (idempotent; any worker may do this)
        for p in self.providers.values():
            for ev in p.poll_events():
                handle_event(st, ev)
        # 2. agent housekeeping: wrap-up expiry, match held calls, abandon on hold timeout
        for cid in st.tick_agents(self.campaign):
            self._hangup(cid)
        # 3. lease recovery (worker crashes, lost webhooks, ambiguous timeouts)
        if now - self._last_recovery >= self.cfg.recovery_interval:
            self._last_recovery = now
            recovery.sweep(st, self.providers, self.cfg, now)
        # 4. read state, ask pacing, pass through the Safety Controller
        stats = st.recent_stats(self.campaign, self.cfg.stats_window_long, self.cfg.stats_window_short,
                                self.safety.cfg.abandon_window)
        snap = st.snapshot(self.campaign, min(stats.avg_time_to_answer, 15.0))
        # 5. capacity dropped / fallback entered: cancel pre-answer excess immediately
        excess = self.safety.excess_unbound(snap, stats)
        if excess:
            for cid, prov in st.cancel_excess_unbound(self.campaign, excess):
                self._hangup(cid, prov)
            snap = st.snapshot(self.campaign, min(stats.avg_time_to_answer, 15.0))
        req = self.engine.request(snap, stats)
        ap = self.safety.review(req, snap, stats, self.active, list(self.providers))
        st.record_decision(self.campaign, req.mode, req.count, ap.count, ap.action, ap.bound,
                           {"reasons": ap.reasons, **req.explain, "excess_cancelled": excess})
        self.allocator.execute(ap)
        self.last = {"snapshot": snap, "request": req, "approval": ap, "stats": stats}
        return ap

    def _hangup(self, call_id, provider=None):
        row = self.store.q("SELECT provider FROM calls WHERE id=?", (call_id,))
        name = provider or (row[0][0] if row else None)
        for n in ([name] if name else list(self.providers)):
            try:
                self.providers[n].cancel(call_id)
            except Exception:
                self.store.count("hangup_failed")
