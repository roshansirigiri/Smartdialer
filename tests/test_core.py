import os
import sys
import threading
import unittest
import warnings

warnings.simplefilter('ignore', ResourceWarning)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartdialer.allocator import WorkerCrash
from smartdialer.config import Config, SafetyConfig
from smartdialer.pacing import ProgressivePacing, max_dialable, tail_prob
from smartdialer.rig import build
from smartdialer.safety import Approval, SafetyViolation
from smartdialer.states import (AGENT_TRANSITIONS, AgentState, CallState as C, call_transition_allowed)
from smartdialer.store import Store
from smartdialer.telecom import ProviderEvent, ProviderTimeout


def ev(call_id, typ, n=None, prov="A"):
    return ProviderEvent(prov, f"{prov}:{call_id}:{typ}" if n is None else f"{prov}:{call_id}:{typ}:{n}", call_id, typ, 0)


def one_call(mode="progressive", agents=1, **kw):
    r = build(agents=agents, borrowers=50, mode=mode, providers=("A",), **kw)
    r.dialer.tick()
    return r, r.calls()[0]["id"]


class StateMachines(unittest.TestCase):
    def test_agent_table_has_no_self_loops_and_covers_all_states(self):
        self.assertEqual(set(AGENT_TRANSITIONS), set(AgentState))
        for s, t in AGENT_TRANSITIONS.items():
            self.assertNotIn(s, t)

    def test_call_forward_only_and_terminal_absorbing(self):
        self.assertTrue(call_transition_allowed(C.RINGING, C.ANSWERED))
        self.assertFalse(call_transition_allowed(C.ANSWERED, C.RINGING))
        for t in (C.COMPLETED, C.FAILED, C.CANCELLED, C.ABANDONED):
            for n in C:
                self.assertFalse(call_transition_allowed(t, n))


class Concurrency(unittest.TestCase):
    def test_two_workers_cannot_reserve_same_agent(self):
        r = build(agents=50, borrowers=500)
        results, errs = [], []

        def worker(i):
            try:
                st = Store(r.store.path, r.clock, r.cfg, fast=True)   # own connection, like a separate process
                results.extend(st.reserve_calls("c1", 50, True, f"w{i}"))
            except Exception as e:      # pragma: no cover
                errs.append(e)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertFalse(errs)
        agents = [x.agent_id for x in results]
        self.assertEqual(len(agents), 50)                     # exactly one reservation per agent
        self.assertEqual(len(set(agents)), 50)
        self.assertEqual(len({x.borrower_id for x in results}), 50)   # and per borrower
        self.assertEqual(r.store.agent_counts("c1")["RESERVED"], 50)

    def test_progressive_never_exceeds_available_agents(self):
        r = build(agents=50, borrowers=1000, mode="progressive")
        for _ in range(60):
            r.step()
            live = r.store.q("SELECT COUNT(*) FROM calls WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED','ABANDONED')")[0][0]
            self.assertLessEqual(live, 50)
        self.assertEqual(r.store.q("SELECT COUNT(*) FROM calls WHERE state='ABANDONED'")[0][0], 0)


class Events(unittest.TestCase):
    def test_duplicate_events_single_transition(self):
        r, cid = one_call()
        for _ in range(3):
            r.store  # noqa
            from smartdialer.events import handle_event
            handle_event(r.store, ev(cid, "RINGING"))
            handle_event(r.store, ev(cid, "ANSWERED"))
        n = r.store.q("SELECT COUNT(*) FROM call_transitions WHERE call_id=? AND to_state='ANSWERED'", (cid,))[0][0]
        self.assertEqual(n, 1)
        self.assertEqual(r.store.counter("duplicate_event"), 4)

    def test_same_semantic_event_different_ids_is_still_one_transition(self):
        from smartdialer.events import handle_event
        r, cid = one_call()
        for i in range(3):
            handle_event(r.store, ev(cid, "ANSWERED", n=i))
        self.assertEqual(r.store.q("SELECT COUNT(*) FROM call_transitions WHERE call_id=? AND to_state='ANSWERED'", (cid,))[0][0], 1)
        self.assertEqual(r.store.counter("stale_event"), 2)

    def test_completed_answered_ringing_out_of_order(self):
        from smartdialer.events import handle_event
        r, cid = one_call()
        for t in ("COMPLETED", "ANSWERED", "RINGING"):
            handle_event(r.store, ev(cid, t))
        c = r.calls()[0]
        self.assertEqual(c["state"], "COMPLETED")
        self.assertEqual(r.store.agent_counts("c1")["AVAILABLE"] + r.store.agent_counts("c1")["WRAP_UP"], 1)
        self.assertEqual(r.store.counter("stale_event"), 2)

    def test_answered_connects_agent_then_completed_wraps_up_then_frees(self):
        from smartdialer.events import handle_event
        r, cid = one_call()
        handle_event(r.store, ev(cid, "RINGING")); handle_event(r.store, ev(cid, "ANSWERED"))
        self.assertEqual(r.store.agent_counts("c1")["CONNECTED"], 1)
        handle_event(r.store, ev(cid, "COMPLETED"))
        self.assertEqual(r.store.agent_counts("c1")["WRAP_UP"], 1)
        r.clock.advance(r.cfg.wrap_up_secs + 1); r.store.tick_agents("c1")
        self.assertEqual(r.store.agent_counts("c1")["AVAILABLE"], 1)

    def test_unknown_call_event_ignored(self):
        from smartdialer.events import handle_event
        r, _ = one_call()
        self.assertFalse(handle_event(r.store, ev("nope", "ANSWERED")).applied)


class FailureHandling(unittest.TestCase):
    def test_call_failure_releases_agent_and_requeues_borrower(self):
        from smartdialer.events import handle_event
        r, cid = one_call()
        handle_event(r.store, ev(cid, "NO_ANSWER"))
        self.assertEqual(r.store.agent_counts("c1")["AVAILABLE"], 1)
        b = r.store.q("SELECT status,attempts FROM borrowers WHERE id=(SELECT borrower_id FROM calls WHERE id=?)", (cid,))[0]
        self.assertEqual((b["status"], b["attempts"]), ("READY", 1))

    def test_agent_disappears_during_setup(self):
        from smartdialer.events import handle_event
        r, cid = one_call()
        lost = r.store.agents_leave(["a0"])
        self.assertEqual(lost, [cid])
        self.assertEqual(r.store.q("SELECT agent_id FROM calls WHERE id=?", (cid,))[0][0], None)
        handle_event(r.store, ev(cid, "RINGING")); handle_event(r.store, ev(cid, "ANSWERED"))
        self.assertEqual(r.calls()[0]["state"], "ANSWERED")     # held: nobody to connect to
        r.clock.advance(r.cfg.hold_secs + 0.1)
        self.assertEqual(r.store.tick_agents("c1"), [cid])
        self.assertEqual(r.calls()[0]["state"], "ABANDONED")    # recorded, never silent

    def test_held_call_connects_when_agent_frees_up(self):
        from smartdialer.events import handle_event
        r, cid = one_call()
        r.store.agents_leave(["a0"])
        handle_event(r.store, ev(cid, "ANSWERED"))
        r.store.agent_login("a0")
        r.store.tick_agents("c1")
        self.assertEqual(r.calls()[0]["state"], "CONNECTED")

    def test_worker_crash_after_reserve_recovers(self):
        r = build(agents=3, borrowers=20, mode="progressive", providers=("A",))
        r.allocator.crash_at = "after_reserve"
        with self.assertRaises(WorkerCrash):
            r.dialer.tick()
        self.assertEqual(r.store.agent_counts("c1")["RESERVED"], 3)     # stuck... until lease expiry
        r.allocator.crash_at = None
        r.clock.advance(r.cfg.reserve_lease + 1)
        r.dialer._last_recovery = -1e18
        r.dialer.tick()
        self.assertGreaterEqual(r.store.counter("orphans_repaired") + len(r.calls("FAILED")), 3)
        self.assertEqual(r.store.q("SELECT COUNT(*) FROM borrowers WHERE status='CLAIMED'")[0][0], 3)  # re-dialed fresh calls
        self.assertEqual(r.store.q("SELECT COUNT(DISTINCT agent_id) FROM calls WHERE state='INITIATED'")[0][0], 3)

    def test_worker_crash_after_place_adopts_provider_call_not_double_dial(self):
        r = build(agents=2, borrowers=20, mode="progressive", providers=("A",))
        r.allocator.crash_at = "after_place"
        with self.assertRaises(WorkerCrash):
            r.dialer.tick()
        placed = r.providers["A"].placed
        r.allocator.crash_at = None
        r.clock.advance(r.cfg.reserve_lease + 1)
        r.dialer._last_recovery = -1e18
        recov = __import__("smartdialer.recovery", fromlist=["sweep"]).sweep(r.store, r.providers, r.cfg)
        self.assertEqual(recov["adopted"], 1 if placed == 1 else placed)
        self.assertEqual(r.providers["A"].placed, placed)           # recovery never re-dials
        live = [c for c in r.calls() if c["state"] in ("INITIATED", "RINGING")]
        self.assertEqual(len(live), placed)                          # the placed call was adopted
        self.assertTrue(all(c["state"] == "FAILED" and c["end_reason"] == "worker_crash"
                            for c in r.calls() if c not in live))   # never-placed one is failed, borrower requeued

    def test_crash_after_answered_state_is_in_db(self):
        from smartdialer.events import handle_event
        r, cid = one_call()
        handle_event(r.store, ev(cid, "ANSWERED"))
        r2 = Store(r.store.path, r.clock, r.cfg)       # "new worker process" after crash
        self.assertEqual(r2.q("SELECT state FROM calls WHERE id=?", (cid,))[0][0], "CONNECTED")
        handle_event(r2, ev(cid, "COMPLETED"))
        self.assertEqual(r2.q("SELECT state FROM calls WHERE id=?", (cid,))[0][0], "COMPLETED")

    def test_provider_outage_trips_breaker_and_preserves_borrowers(self):
        r = build(agents=10, borrowers=200, mode="progressive", providers=("A",))
        r.providers["A"].set_outage(True)
        r.step(30)
        st = r.safety.breaker("A").state(r.clock.now())
        self.assertIn(st, ("OPEN", "HALF_OPEN"))
        self.assertLess(r.providers["A"].placed, 5)
        dec = r.store.q("SELECT action FROM decisions ORDER BY id DESC LIMIT 1")[0][0]
        self.assertIn(dec, ("REJECT", "REDUCE", "APPROVE"))
        lost = r.store.q("SELECT COUNT(*) FROM borrowers WHERE status='EXHAUSTED'")[0][0]
        self.assertEqual(lost, 0)                      # outage never burns a borrower's attempts
        r.providers["A"].set_outage(False)
        r.step(60)
        self.assertGreater(r.providers["A"].placed, 5)  # half-open probe succeeds -> resumes


class SafetyBoundary(unittest.TestCase):
    def test_allocator_rejects_forged_and_reused_approvals(self):
        r = build(agents=5, borrowers=50)
        forged = Approval("c1", 50, False, "APPROVE", 50, (), "deadbeef")
        with self.assertRaises(SafetyViolation):
            r.allocator.execute(forged)
        r.dialer.tick()          # legit path works
        snap = r.store.snapshot("c1", 5)
        stats = r.store.recent_stats("c1", 200, 40, 100)
        ap = r.safety.review(ProgressivePacing().request(snap, stats), snap, stats)
        r.allocator.execute(ap)
        with self.assertRaises(SafetyViolation):
            r.allocator.execute(ap)                       # one-shot

    def test_safety_caps_greedy_pacing(self):
        from smartdialer.pacing import PacingRequest
        r = build(agents=10, borrowers=5000)
        snap = r.store.snapshot("c1", 5); stats = r.store.recent_stats("c1", 200, 40, 100)
        ap = r.safety.review(PacingRequest("c1", "PREDICTIVE", 4000), snap, stats)
        self.assertLessEqual(ap.count, 4 * 10)
        self.assertIn(ap.action, ("REDUCE",))

    def test_abandon_rate_forces_progressive_fallback(self):
        r = build(agents=10, borrowers=500)
        now = r.clock.now()
        with r.store._tx() as c:      # 100 answered calls, 5 abandoned
            for i in range(100):
                c.execute("INSERT INTO calls(id,campaign_id,borrower_id,call_seq,state,bound,created_at,initiated_at,"
                          "answered_at,ended_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (f"h{i}", "c1", 1000 + i, 1, "ABANDONED" if i < 5 else "COMPLETED", 0, now, now, now, now))
        stats = r.store.recent_stats("c1", 200, 40, 100)
        self.assertAlmostEqual(stats.abandon_rate, 0.05)
        r.dialer.tick()
        self.assertEqual(r.dialer.last["approval"].action, "FALLBACK_PROGRESSIVE")
        self.assertTrue(r.dialer.last["approval"].bound)
        self.assertLessEqual(r.dialer.last["approval"].count, 10)

    def test_capacity_drop_cancels_excess_in_one_tick(self):
        r = build(agents=100, borrowers=5000, answer_rate=0.2)
        r.step(150)
        before = r.store.snapshot("c1", 5).unbound_preanswer
        self.assertGreater(before, 100)                        # we are over-dialing at 20% answer rate
        r.store.agents_leave([f"a{i}" for i in range(100)])    # everyone vanishes (OFFLINE)
        r.dialer.tick()
        self.assertEqual(r.store.snapshot("c1", 5).unbound_preanswer, 0)
        self.assertGreater(r.store.counter("stale_event") + len(r.calls("CANCELLED")), 0)


class Pacing(unittest.TestCase):
    def test_binomial_tail_and_max_dialable(self):
        self.assertAlmostEqual(tail_prob(2, 1, 0.5), 0.25)
        self.assertEqual(max_dialable(10, 1.0, 0.05, 100), 10)          # everyone answers -> no overdial
        n = max_dialable(10, 0.3, 0.05, 100)
        self.assertGreater(n, 10)
        self.assertLessEqual(tail_prob(n, 10, 0.3), 0.05)
        self.assertGreater(tail_prob(n + 1, 10, 0.3), 0.05)             # maximal
        self.assertLessEqual(tail_prob(*(max_dialable(1000, 0.4, 0.05, 4000), 1000, 0.4)), 0.05)

    def test_cold_start_is_progressive_and_decision_is_explained(self):
        r = build(agents=20, borrowers=500)
        r.dialer.tick()
        req = r.dialer.last["request"]
        self.assertEqual(req.explain["p_used"], 1.0)
        self.assertEqual(r.dialer.last["approval"].count, 20)
        self.assertIn("start", req.explain["sentence"])


if __name__ == "__main__":
    unittest.main()
