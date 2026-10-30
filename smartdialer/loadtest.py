"""Basic load test.
    python -m smartdialer.loadtest
Part 1: N real threads (own SQLite connections) hammer reserve_calls on one campaign and we
        verify the hard invariants: no agent or borrower is ever double-booked.
Part 2: cost of one dialer tick and of event ingestion at 100 / 1,000 / 10,000 agents.
"""
import os
import tempfile
import threading
import time

from .clock import VirtualClock
from .config import Config
from .rig import build
from .store import Store
from .telecom import ProviderEvent


def contention(agents=500, threads=8, borrowers=20000):
    r = build(agents=agents, borrowers=borrowers, mode="progressive")
    got, lock = [], threading.Lock()

    def work(i):
        st = Store(r.store.path, r.clock, r.cfg, fast=True)
        while True:
            refs = st.reserve_calls("c1", 25, True, f"w{i}")
            if not refs:
                return
            with lock:
                got.extend(refs)

    t = time.time()
    ts = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    [x.start() for x in ts]; [x.join() for x in ts]
    dt = time.time() - t
    ag, br = [x.agent_id for x in got], [x.borrower_id for x in got]
    assert len(ag) == len(set(ag)) == agents, (len(ag), len(set(ag)))
    assert len(br) == len(set(br)), "borrower double-booked"
    print(f"[contention] {threads} threads, {agents} agents: {len(got)} reservations in {dt:.2f}s "
          f"({len(got)/dt:,.0f}/s), 0 double-bookings OK")


def scale(n_agents):
    r = build(agents=n_agents, borrowers=n_agents * 20, mode="predictive", providers=("A",), answer_rate=0.3)
    r.step(5)                                   # warm
    t = time.time(); ticks = 8
    for _ in range(ticks):
        r.dialer.tick(); r.clock.advance(1)
    tick_ms = (time.time() - t) / ticks * 1000
    # event ingestion throughput (idempotent transitions, 1 write txn each)
    calls = [c["id"] for c in r.calls("RINGING")][:300] or [c["id"] for c in r.calls()][:300]
    t = time.time()
    from .events import handle_event
    for cid in calls:
        handle_event(r.store, ProviderEvent("A", f"A:{cid}:x", cid, "RINGING", 0))
    ev_s = len(calls) / max(1e-9, time.time() - t)
    snap_t = time.time(); r.store.snapshot("c1", 5); r.store.recent_stats("c1", 200, 40, 100)
    read_ms = (time.time() - snap_t) * 1000
    print(f"[scale] {n_agents:>6} agents: tick {tick_ms:7.1f} ms  (reads {read_ms:5.1f} ms)  "
          f"events {ev_s:7,.0f}/s  in-flight {r.store.snapshot('c1',5).unbound_preanswer}")


if __name__ == "__main__":
    contention()
    for n in (100, 1000, 10000):
        scale(n)
