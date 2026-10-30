"""Scenario simulator (virtual clock, deterministic seeds).
    python -m smartdialer.sim              # full run, writes docs/RESULTS.md
    python -m smartdialer.sim --quick      # short run for a smoke test
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
import time

from .rig import build
from .telecom import Behavior

SCENARIOS = {
    "A 20%/120s": dict(ar=lambda t: 0.20, talk=lambda t: 120),
    "B 50%/90s": dict(ar=lambda t: 0.50, talk=lambda t: 90),
    "C 70%/180s": dict(ar=lambda t: 0.70, talk=lambda t: 180),
    "D changing": dict(ar=lambda t: 0.65 if t < 300 else (0.10 if t < 600 else 0.55),
                       talk=lambda t: 60 if t < 300 else (200 if t < 600 else 90)),
    "E provider outage 300-420s": dict(ar=lambda t: 0.4, talk=lambda t: 90, outage=(300, 420)),
    "F 40/100 agents vanish @300s": dict(ar=lambda t: 0.4, talk=lambda t: 90, drop_at=300, drop_n=40),
}


def run(args):
    name, mode, duration, agents = args
    sc = SCENARIOS[name]
    beh = Behavior(sc["ar"], sc["talk"])
    r = build(agents=agents, borrowers=60 * duration, mode=mode, behavior=beh, seed=7, weights={"A": 0.7, "B": 0.3})
    r.store.kv_set("noop", 1)
    series, util, idle, infl, cap = [], [], [], [], []
    actions, react = {}, {}
    t0 = r.clock.now()
    dropped = False
    for i in range(duration):
        t = r.clock.now() - t0
        if "outage" in sc:
            on = sc["outage"][0] <= t < sc["outage"][1]
            for p in r.providers.values():
                p.set_outage(on)
        if "drop_at" in sc and t >= sc["drop_at"] and not dropped:
            dropped = True
            before = r.store.snapshot("c1", 5)
            r.store.agents_leave([f"a{k}" for k in range(sc["drop_n"])])
            react["unbound_before_drop"] = before.unbound_preanswer
            react["capacity_before"] = before.free + before.soon_free
            t_drop = i
        ap = r.dialer.tick()
        snap, st = r.dialer.last["snapshot"], r.dialer.last["stats"]
        ag = r.store.agent_counts("c1")
        logged = sum(v for k, v in ag.items() if k not in ("OFFLINE", "PAUSED"))
        if logged:
            util.append(ag["CONNECTED"] / logged)
            idle.append(ag["AVAILABLE"] / logged)
        infl.append(snap.unbound_preanswer + snap.bound_preanswer)
        cap.append(snap.free + snap.soon_free)
        actions[ap.action] = actions.get(ap.action, 0) + 1
        if dropped and "unbound_after" not in react and i > t_drop:
            react["unbound_after"] = r.store.snapshot("c1", 5).unbound_preanswer
            react["capacity_after"] = snap.free + snap.soon_free
        if i % 60 == 0:
            ex = r.dialer.last["request"].explain
            series.append(dict(t=i, util=round(sum(util[-60:]) / max(1, len(util[-60:])), 2), in_flight=infl[-1],
                               free=snap.free, p_used=ex.get("p_used"), req=r.dialer.last["request"].count,
                               approved=ap.count, action=ap.action, abandon=round(st.abandon_rate, 3)))
        r.clock.advance(1.0)
    q = lambda sql: r.store.q(sql)[0][0]
    answered, abandoned = q("SELECT COUNT(*) FROM calls WHERE answered_at IS NOT NULL"), q("SELECT COUNT(*) FROM calls WHERE state='ABANDONED'")
    steady = util[60:] or util
    return dict(scenario=name, mode=mode, agents=agents, duration=duration,
                utilization=round(sum(steady) / len(steady), 3), idle=round(sum(idle[60:]) / max(1, len(idle[60:])), 3),
                initiated=q("SELECT COUNT(*) FROM calls WHERE initiated_at IS NOT NULL"), answered=answered,
                connected=q("SELECT COUNT(*) FROM calls WHERE connected_at IS NOT NULL"), abandoned=abandoned,
                abandon_rate=round(abandoned / answered, 4) if answered else 0.0,
                avg_in_flight=round(sum(infl) / len(infl), 1), avg_capacity=round(sum(cap) / len(cap), 1),
                actions=actions, duplicate_events=r.store.counter("duplicate_event"),
                stale_events=r.store.counter("stale_event"), agent_lost_in_setup=r.store.counter("agent_lost_during_setup"),
                held_then_connected=r.store.counter("held_then_connected"), react=react, series=series,
                provider_calls={n: p.placed for n, p in r.providers.items()})


def markdown(results):
    L = ["# Simulation results", "", "Virtual-clock simulation, 100 agents, Provider A (70%: fast/clean) + Provider B (30%: slow, "
         "timeouts, duplicate and reordered events). Utilization = share of logged-in agent-time spent CONNECTED "
         "(after a 60s warm-up).", "",
         "| Scenario | Mode | Utilization | Idle (AVAILABLE) | Initiated | Answered | Connected | Abandoned | Abandon % | Avg in-flight vs capacity | Safety decisions |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in results:
        acts = ", ".join(f"{k}:{v}" for k, v in sorted(x["actions"].items()))
        L.append(f"| {x['scenario']} | {x['mode']} | {x['utilization']:.0%} | {x['idle']:.0%} | {x['initiated']} | {x['answered']} | "
                 f"{x['connected']} | {x['abandoned']} | {x['abandon_rate']:.2%} | {x['avg_in_flight']} / {x['avg_capacity']} | {acts} |")
    for x in results:
        if x["mode"] == "predictive" and (x["scenario"].startswith("D") or x["scenario"].startswith("F") or x["scenario"].startswith("E")):
            L += ["", f"## Time series: {x['scenario']} (predictive)", "", "| t(s) | util(60s) | in-flight | free agents | p used | requested | approved | action | abandon(win) |",
                  "|---|---|---|---|---|---|---|---|---|"]
            for s in x["series"]:
                L.append(f"| {s['t']} | {s['util']:.0%} | {s['in_flight']} | {s['free']} | {s['p_used']} | {s['req']} | {s['approved']} | {s['action']} | {s['abandon']:.1%} |")
            if x["react"]:
                L += ["", f"Agent-drop reaction: `{json.dumps(x['react'])}`"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "docs", "RESULTS.md"))
    a = ap.parse_args()
    duration, agents = (240, 40) if a.quick else (900, 100)
    jobs = [(n, m, duration, agents) for n in SCENARIOS for m in ("progressive", "predictive")]
    t = time.time()
    with mp.get_context("fork").Pool(min(4, os.cpu_count() or 1)) as pool:
        results = pool.map(run, jobs)
    md = markdown(results)
    print(md)
    print(f"[{time.time() - t:.0f}s wall]", file=sys.stderr)
    if not a.quick:
        open(a.out, "w").write(md)


if __name__ == "__main__":
    main()
