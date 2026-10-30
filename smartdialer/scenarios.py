"""Narrated demos of the five required failure cases.   python -m smartdialer.scenarios"""
from . import recovery
from .allocator import WorkerCrash
from .events import handle_event
from .rig import build
from .telecom import ProviderEvent


def ev(call_id, typ, i=0):
    return ProviderEvent("B", f"B:{call_id}:{typ}:{i}", call_id, typ, 0)


def hdr(s):
    print(f"\n=== {s}")


def worker_crash():
    hdr("1. Worker crash: agent+borrower reserved, call placed, worker dies before recording INITIATED")
    r = build(agents=3, borrowers=20, mode="progressive", providers=("A",))
    r.allocator.crash_at = "after_place"
    try:
        r.dialer.tick()
    except WorkerCrash as e:
        print("worker died:", e)
    print("DB after crash   agents:", {k: v for k, v in r.store.agent_counts("c1").items() if v},
          "| calls:", [c["state"] for c in r.calls()], "| provider placed:", r.providers["A"].placed)
    r.allocator.crash_at = None
    r.clock.advance(r.cfg.reserve_lease + 1)                     # leases expire
    print("recovery sweep  :", recovery.sweep(r.store, r.providers, r.cfg))
    print("DB after recovery agents:", {k: v for k, v in r.store.agent_counts("c1").items() if v},
          "| calls:", [(c["state"], c["end_reason"]) for c in r.calls()], "| provider placed:", r.providers["A"].placed)
    print("=> the placed call was ADOPTED (not re-dialed); never-placed calls FAILED(worker_crash) and their borrowers/agents were freed.")


def provider_outage():
    hdr("2. Provider outage: both providers time out for 60s")
    r = build(agents=20, borrowers=500, mode="predictive", answer_rate=0.4)
    r.step(60)
    live = len(r.calls("RINGING")) + len(r.calls("INITIATED"))
    print(f"before outage: {live} calls in flight")
    for p in r.providers.values():
        p.set_outage(True)
    placed0 = sum(p.placed for p in r.providers.values())
    acts = []
    for _ in range(60):
        r.step()
        acts.append(r.dialer.last["approval"].action)
    print("safety decisions during outage:", {a: acts.count(a) for a in set(acts)})
    print("new calls placed during outage:", sum(p.placed for p in r.providers.values()) - placed0,
          "| breakers:", {n: r.safety.breaker(n).state(r.clock.now()) for n in r.providers})
    print("existing calls: events still delivered ->", len(r.calls("CONNECTED")), "connected now; recovery uses get_status() only when reachable")
    print("borrowers burned (EXHAUSTED):", r.store.q("SELECT COUNT(*) FROM borrowers WHERE status='EXHAUSTED'")[0][0],
          "(infra failures never consume a retry attempt)")
    for p in r.providers.values():
        p.set_outage(False)
    r.step(60)
    print("after recovery: breakers", {n: r.safety.breaker(n).state(r.clock.now()) for n in r.providers},
          "| in flight:", r.dialer.last["snapshot"].unbound_preanswer)


def agents_drop():
    hdr("3. Availability drops: 100 agents, 40 vanish at once")
    r = build(agents=100, borrowers=5000, mode="predictive", answer_rate=0.3)
    r.step(120)
    s = r.store.snapshot("c1", 5)
    print(f"before: free={s.free} soon_free={s.soon_free} unbound in flight={s.unbound_preanswer}")
    r.store.agents_leave([f"a{i}" for i in range(40)])
    r.dialer.tick()
    s2 = r.store.snapshot("c1", 5)
    print(f"1 tick after: free={s2.free} soon_free={s2.soon_free} unbound in flight={s2.unbound_preanswer}"
          f" | cap={r.safety.max_unbound(s2)} | cancelled={len(r.calls('CANCELLED'))}")
    print("=> reaction time = one tick (<=1s here). Ringing calls above the new ratio cap are cancelled least-progressed-first.")


def duplicate_and_out_of_order():
    hdr("4+5. Duplicate and out-of-order provider events")
    r = build(agents=1, borrowers=10, mode="progressive", providers=("A",))
    r.dialer.tick()
    cid = r.calls()[0]["id"]
    for t, i in [("ANSWERED", 0), ("ANSWERED", 1), ("ANSWERED", 2), ("COMPLETED", 0), ("ANSWERED", 3), ("RINGING", 0)]:
        res = handle_event(r.store, ev(cid, t, i))
        print(f"  {t:<10} -> applied={res.applied!s:<5} note={res.note}")
    print("  transitions:", [(x["from_state"], x["to_state"]) for x in
                              r.store.q("SELECT * FROM call_transitions WHERE call_id=? ORDER BY id", (cid,))])
    r2 = build(agents=1, borrowers=10, mode="progressive", providers=("A",))
    r2.dialer.tick(); c2 = r2.calls()[0]["id"]
    print("  out-of-order COMPLETED, ANSWERED, RINGING:")
    for t in ("COMPLETED", "ANSWERED", "RINGING"):
        res = handle_event(r2.store, ev(c2, t))
        print(f"  {t:<10} -> applied={res.applied!s:<5} note={res.note}")
    print("  final call state:", r2.calls()[0]["state"], "| agents:", {k: v for k, v in r2.store.agent_counts("c1").items() if v})


if __name__ == "__main__":
    worker_crash(); provider_outage(); agents_drop(); duplicate_and_out_of_order()
