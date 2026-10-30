"""SQLite-backed source of truth. Every multi-row decision happens inside ONE
BEGIN IMMEDIATE transaction, and every ownership change is a compare-and-swap
(`UPDATE ... WHERE state = <expected>` + rowcount check).  No in-memory/cached state is
ever authoritative, so a crashed worker loses nothing.  The same SQL works on Postgres
(row locks / SKIP LOCKED) if you outgrow SQLite's single writer."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .states import (AGENT_TRANSITIONS, AgentState as A, CallState as C, TERMINAL,
                     TERMINAL_SQL, call_transition_allowed)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, campaign_id TEXT, state TEXT, call_id TEXT, version INTEGER DEFAULT 0,
  state_since REAL, wrap_until REAL, pending TEXT, last_free_at REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_agents ON agents(campaign_id, state, last_free_at);
CREATE TABLE IF NOT EXISTS borrowers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, phone TEXT, status TEXT DEFAULT 'READY',
  attempts INTEGER DEFAULT 0, priority INTEGER DEFAULT 0, next_attempt_at REAL DEFAULT 0,
  claimed_by TEXT);
CREATE INDEX IF NOT EXISTS ix_borrowers ON borrowers(campaign_id, status, next_attempt_at);
CREATE TABLE IF NOT EXISTS calls(
  id TEXT PRIMARY KEY, campaign_id TEXT, borrower_id INTEGER, call_seq INTEGER, agent_id TEXT,
  provider TEXT, state TEXT, bound INTEGER, created_at REAL, lease_until REAL,
  initiated_at REAL, ringing_at REAL, answered_at REAL, connected_at REAL, ended_at REAL,
  end_reason TEXT, hold_deadline REAL, UNIQUE(borrower_id, call_seq));
CREATE INDEX IF NOT EXISTS ix_calls_state ON calls(campaign_id, state);
CREATE INDEX IF NOT EXISTS ix_calls_ended ON calls(campaign_id, ended_at);
CREATE INDEX IF NOT EXISTS ix_calls_answered ON calls(campaign_id, answered_at);
CREATE TABLE IF NOT EXISTS call_transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, from_state TEXT, to_state TEXT,
  cause TEXT, at REAL);
CREATE INDEX IF NOT EXISTS ix_trans ON call_transitions(call_id);
CREATE TABLE IF NOT EXISTS provider_events(
  provider TEXT, event_id TEXT, received_at REAL, PRIMARY KEY(provider, event_id));
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, campaign_id TEXT, mode TEXT, requested INTEGER,
  approved INTEGER, action TEXT, bound INTEGER, detail TEXT);
"""

INFRA_REASONS = {"worker_crash", "provider_error", "provider_rejected", "provider_unavailable",
                 "setup_timeout", "cancelled_capacity", "provider_timeout"}


@dataclass
class CallRef:
    call_id: str
    borrower_id: int
    phone: str
    agent_id: Optional[str]
    bound: bool


@dataclass
class Result:
    applied: bool
    note: str
    state: Optional[str] = None


class Store:
    def __init__(self, path: str, clock, cfg: Config = None, fast: bool = False):
        self.path, self.clock, self.cfg, self.fast = path, clock, cfg or Config(), fast
        self._local = threading.local()
        self._conn().executescript(SCHEMA)

    # ---------------------------------------------------------------- plumbing
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, timeout=60)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=%s" % ("OFF" if self.fast else "NORMAL"))
            self._local.c = c
        return c

    @contextmanager
    def _tx(self):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")      # serialises writers: no lost updates
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def q(self, sql, args=()):
        return self._conn().execute(sql, args).fetchall()

    def count(self, name: str, c=None, by: int = 1):
        (c or self._conn()).execute(
            "INSERT INTO counters(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+?",
            (name, by, by))

    def counter(self, name: str) -> int:
        r = self.q("SELECT value FROM counters WHERE name=?", (name,))
        return r[0][0] if r else 0

    def kv_get(self, k, default=None):
        r = self.q("SELECT value FROM kv WHERE key=?", (k,))
        return json.loads(r[0][0]) if r else default

    def kv_set(self, k, v):
        self._conn().execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))

    def _log(self, c, call_id, frm, to, cause):
        c.execute("INSERT INTO call_transitions(call_id,from_state,to_state,cause,at) VALUES(?,?,?,?,?)",
                  (call_id, frm, to, cause, self.clock.now()))

    # ---------------------------------------------------------------- CAS helpers
    def _cas_agent(self, c, agent_id, expected, new: A, call_id="__keep__", **cols) -> bool:
        """Compare-and-swap an agent's state. `expected` is a state or tuple of states.
        This is THE guard that stops two workers from reserving the same agent: only one
        UPDATE can match `state = AVAILABLE`; the loser sees rowcount == 0."""
        exp = (expected,) if isinstance(expected, A) else tuple(expected)
        for e in exp:
            assert new in AGENT_TRANSITIONS[e], f"illegal agent transition {e}->{new}"
        sets = ["state=?", "version=version+1", "state_since=?"]
        args = [new.value, self.clock.now()]
        if call_id != "__keep__":
            sets.append("call_id=?")
            args.append(call_id)
        for k, v in cols.items():
            sets.append(f"{k}=?")
            args.append(v)
        ph = ",".join("?" * len(exp))
        cur = c.execute(f"UPDATE agents SET {','.join(sets)} WHERE id=? AND state IN ({ph})",
                        (*args, agent_id, *[e.value for e in exp]))
        return cur.rowcount == 1

    # ---------------------------------------------------------------- seeding / agents API
    def add_agents(self, campaign, n, prefix="a", login=True):
        now = self.clock.now()
        with self._tx() as c:
            c.executemany("INSERT INTO agents(id,campaign_id,state,state_since,last_free_at) VALUES(?,?,?,?,?)",
                          [(f"{prefix}{i}", campaign, "AVAILABLE" if login else "OFFLINE", now, now)
                           for i in range(n)])
        return [f"{prefix}{i}" for i in range(n)]

    def add_borrowers(self, campaign, n, start=0):
        with self._tx() as c:
            c.executemany("INSERT INTO borrowers(campaign_id,phone) VALUES(?,?)",
                          [(campaign, f"+91{9000000000 + start + i}") for i in range(n)])

    def agent_login(self, agent_id):
        with self._tx() as c:
            return self._cas_agent(c, agent_id, (A.OFFLINE, A.PAUSED), A.AVAILABLE,
                                   call_id=None, last_free_at=self.clock.now())

    def agents_leave(self, agent_ids, target: A = A.OFFLINE):
        """Agent(s) pause / go offline. Setup-phase agents are detached from their call
        immediately (call becomes unbound); connected agents finish the call first."""
        lost = []
        with self._tx() as c:
            for aid in agent_ids:
                a = c.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
                st = A(a["state"])
                if st in (A.OFFLINE, A.PAUSED):
                    continue
                if st == A.CONNECTED:
                    c.execute("UPDATE agents SET pending=? WHERE id=?", (target.value, aid))
                    continue
                if st in (A.RESERVED, A.DIALING) and a["call_id"]:
                    c.execute("UPDATE calls SET agent_id=NULL WHERE id=? AND agent_id=?", (a["call_id"], aid))
                    lost.append(a["call_id"])
                    self.count("agent_lost_during_setup", c)
                self._cas_agent(c, aid, st, target, call_id=None)
        return lost

    # ---------------------------------------------------------------- reservation
    def reserve_calls(self, campaign, n, bound: bool, worker_id: str):
        """Atomically pick borrowers (and, if `bound`, agents), claim them via CAS, and create
        call rows in RESERVED. All-or-nothing per pair; crash => rollback or lease recovery."""
        now, cfg, out = self.clock.now(), self.cfg, []
        if n <= 0:
            return out
        with self._tx() as c:
            brs = c.execute("SELECT id,phone FROM borrowers WHERE campaign_id=? AND status='READY' "
                            "AND next_attempt_at<=? ORDER BY priority DESC,next_attempt_at,id LIMIT ?",
                            (campaign, now, n)).fetchall()
            agents = []
            if bound:
                agents = c.execute("SELECT id FROM agents WHERE campaign_id=? AND state='AVAILABLE' "
                                   "ORDER BY last_free_at,id LIMIT ?", (campaign, len(brs))).fetchall()
                brs = brs[:len(agents)]
            for i, b in enumerate(brs):
                cur = c.execute("UPDATE borrowers SET status='CLAIMED',claimed_by=? WHERE id=? AND status='READY'",
                                (worker_id, b["id"]))
                if cur.rowcount != 1:
                    continue
                call_id, aid = uuid.uuid4().hex[:14], None
                if bound:
                    aid = agents[i]["id"]
                    if not self._cas_agent(c, aid, A.AVAILABLE, A.RESERVED, call_id=call_id):
                        c.execute("UPDATE borrowers SET status='READY',claimed_by=NULL WHERE id=?", (b["id"],))
                        self.count("reserve_race_lost", c)
                        continue
                seq = c.execute("SELECT COALESCE(MAX(call_seq),0)+1 FROM calls WHERE borrower_id=?",
                                (b["id"],)).fetchone()[0]
                c.execute("INSERT INTO calls(id,campaign_id,borrower_id,call_seq,agent_id,state,bound,"
                          "created_at,lease_until) VALUES(?,?,?,?,?,?,?,?,?)",
                          (call_id, campaign, b["id"], seq, aid, "RESERVED", int(bound), now, now + cfg.reserve_lease))
                self._log(c, call_id, None, "RESERVED", "reserve")
                out.append(CallRef(call_id, b["id"], b["phone"], aid, bound))
        return out

    # ---------------------------------------------------------------- transitions
    def apply_transition(self, call_id, target: C, *, cause, event_key=None, reason=None,
                         provider=None, lease=None) -> Result:
        with self._tx() as c:
            return self._apply(c, call_id, target, cause, event_key, reason, provider, lease)

    def _apply(self, c, call_id, target: C, cause, event_key=None, reason=None, provider=None, lease=None):
        now, cfg = self.clock.now(), self.cfg
        if event_key:  # idempotency: same (provider,event_id) is processed at most once, atomically
            cur = c.execute("INSERT OR IGNORE INTO provider_events VALUES(?,?,?)", (*event_key, now))
            if cur.rowcount == 0:
                self.count("duplicate_event", c)
                return Result(False, "duplicate")
        call = c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if call is None:
            self.count("unknown_call_event", c)
            return Result(False, "unknown_call")
        cur_state = C(call["state"])
        # remap provider semantics that depend on where we are
        if target == C.FAILED and cur_state in (C.ANSWERED, C.CONNECTED):
            target, reason = C.COMPLETED, reason or "failed_mid_call"
        if target == C.COMPLETED and cur_state == C.ANSWERED and call["agent_id"] is None:
            target, reason = C.ABANDONED, "hangup_while_held"
        if not call_transition_allowed(cur_state, target):
            self.count("stale_event", c)          # out-of-order / late: ignored, state untouched
            return Result(False, "stale", cur_state.value)
        sets, args = ["state=?"], [target.value]
        if provider:
            sets.append("provider=?"); args.append(provider)
        if lease is not None:
            sets.append("lease_until=?"); args.append(now + lease)
        self._log(c, call_id, cur_state.value, target.value, cause)
        if target == C.INITIATED:
            sets += ["initiated_at=?", "lease_until=?"]; args += [now, now + (lease if lease is not None else cfg.setup_lease)]
            if call["agent_id"]:
                self._cas_agent(c, call["agent_id"], A.RESERVED, A.DIALING)
        elif target == C.RINGING:
            sets += ["ringing_at=?", "lease_until=?"]; args += [now, now + cfg.setup_lease]
        elif target == C.ANSWERED:
            sets += ["answered_at=?", "lease_until=?"]; args += [now, now + cfg.max_call_secs]
        elif target in TERMINAL:
            sets += ["ended_at=?", "end_reason=?", "lease_until=NULL"]; args += [now, reason or cause]
        c.execute(f"UPDATE calls SET {','.join(sets)} WHERE id=?", (*args, call_id))
        if target == C.ANSWERED:
            if self._bridge(c, call_id, now):
                return Result(True, "answered+connected", C.CONNECTED.value)
            c.execute("UPDATE calls SET hold_deadline=? WHERE id=?", (now + cfg.hold_secs, call_id))
            self.count("answered_held", c)
        if target in TERMINAL:
            self._finalize(c, call_id, target, reason or cause, now)
        return Result(True, "applied", target.value)

    def _bridge(self, c, call_id, now) -> bool:
        """Connect an ANSWERED call to an agent: its own bound agent if still held, else claim
        any AVAILABLE agent via CAS. Returns False if no agent (call is 'held')."""
        call = c.execute("SELECT agent_id,campaign_id FROM calls WHERE id=?", (call_id,)).fetchone()
        aid, ok = call["agent_id"], False
        if aid:
            ok = self._cas_agent(c, aid, (A.RESERVED, A.DIALING), A.CONNECTED, call_id=call_id)
        if not ok:
            for r in c.execute("SELECT id FROM agents WHERE campaign_id=? AND state='AVAILABLE' "
                               "ORDER BY last_free_at,id LIMIT 3", (call["campaign_id"],)).fetchall():
                if self._cas_agent(c, r["id"], A.AVAILABLE, A.CONNECTED, call_id=call_id):
                    aid, ok = r["id"], True
                    break
        if ok:
            c.execute("UPDATE calls SET state='CONNECTED',agent_id=?,connected_at=?,hold_deadline=NULL WHERE id=?",
                      (aid, now, call_id))
            self._log(c, call_id, "ANSWERED", "CONNECTED", "bridge")
        return ok

    def _finalize(self, c, call_id, target: C, reason, now):
        call = c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        aid = call["agent_id"]
        if aid:
            a = c.execute("SELECT state,call_id,pending FROM agents WHERE id=?", (aid,)).fetchone()
            if a and a["call_id"] == call_id:
                st = A(a["state"])
                if st == A.CONNECTED:
                    self._cas_agent(c, aid, A.CONNECTED, A.WRAP_UP, call_id=None, wrap_until=now + self.cfg.wrap_up_secs)
                elif st in (A.RESERVED, A.DIALING):
                    self._cas_agent(c, aid, st, A.AVAILABLE, call_id=None, last_free_at=now)
        bid, attempts = call["borrower_id"], c.execute("SELECT attempts FROM borrowers WHERE id=?",
                                                       (call["borrower_id"],)).fetchone()[0]
        if target == C.COMPLETED and call["connected_at"]:
            c.execute("UPDATE borrowers SET status='DONE',claimed_by=NULL WHERE id=?", (bid,))
        elif reason in INFRA_REASONS:
            c.execute("UPDATE borrowers SET status='READY',claimed_by=NULL,next_attempt_at=? WHERE id=?",
                      (now + self.cfg.infra_retry_delay, bid))
        elif target == C.ABANDONED:
            c.execute("UPDATE borrowers SET status='READY',claimed_by=NULL,attempts=attempts+1,priority=priority+1,"
                      "next_attempt_at=? WHERE id=?", (now + self.cfg.abandon_callback_delay, bid))
        else:
            if attempts + 1 >= self.cfg.max_attempts:
                c.execute("UPDATE borrowers SET status='EXHAUSTED',attempts=attempts+1,claimed_by=NULL WHERE id=?", (bid,))
            else:
                c.execute("UPDATE borrowers SET status='READY',claimed_by=NULL,attempts=attempts+1,next_attempt_at=? WHERE id=?",
                          (now + self.cfg.retry_delay, bid))

    def extend_lease(self, call_id, secs):
        self._conn().execute("UPDATE calls SET lease_until=? WHERE id=? AND state NOT IN " + TERMINAL_SQL,
                             (self.clock.now() + secs, call_id))

    # ---------------------------------------------------------------- per-tick housekeeping
    def tick_agents(self, campaign):
        """WRAP_UP expiry, match held (answered, agent-less) calls to free agents, abandon on
        hold timeout. Returns call_ids abandoned (caller hangs up at the provider)."""
        now, abandoned = self.clock.now(), []
        with self._tx() as c:
            for a in c.execute("SELECT id,pending FROM agents WHERE campaign_id=? AND state='WRAP_UP' AND wrap_until<=?",
                               (campaign, now)).fetchall():
                tgt = A(a["pending"]) if a["pending"] else A.AVAILABLE
                self._cas_agent(c, a["id"], A.WRAP_UP, tgt, call_id=None, pending=None, last_free_at=now)
            for h in c.execute("SELECT id,hold_deadline FROM calls WHERE campaign_id=? AND state='ANSWERED' "
                               "AND agent_id IS NULL ORDER BY answered_at", (campaign,)).fetchall():
                if self._bridge(c, h["id"], now):
                    self.count("held_then_connected", c)
                elif h["hold_deadline"] is not None and h["hold_deadline"] <= now:
                    self._apply(c, h["id"], C.ABANDONED, "hold_timeout", reason="hold_timeout")
                    abandoned.append(h["id"])
            # connected agent whose call ended while they were pending pause is handled in WRAP_UP above
        return abandoned

    def cancel_excess_unbound(self, campaign, n, reason="cancelled_capacity"):
        """Cancel up to n unbound, pre-answer calls; least-progressed first (cheapest to drop)."""
        with self._tx() as c:
            rows = c.execute("SELECT id,provider,state FROM calls WHERE campaign_id=? AND bound=0 AND agent_id IS NULL "
                             "AND state IN ('RESERVED','INITIATED','RINGING') "
                             "ORDER BY CASE state WHEN 'RESERVED' THEN 0 WHEN 'INITIATED' THEN 1 ELSE 2 END, created_at DESC "
                             "LIMIT ?", (campaign, n)).fetchall()
            out = []
            for r in rows:
                if self._apply(c, r["id"], C.CANCELLED, "safety", reason=reason).applied:
                    out.append((r["id"], r["provider"]))
            return out

    # ---------------------------------------------------------------- recovery helpers
    def expired_calls(self, now):
        return self.q("SELECT * FROM calls WHERE lease_until IS NOT NULL AND lease_until<? AND state NOT IN "
                      + TERMINAL_SQL, (now,))

    def repair_orphans(self):
        """Idempotent self-heal: free borrowers/agents that point at terminal or missing calls."""
        with self._tx() as c:
            r1 = c.execute("UPDATE borrowers SET status='READY',claimed_by=NULL WHERE status='CLAIMED' AND NOT EXISTS("
                           "SELECT 1 FROM calls WHERE borrower_id=borrowers.id AND state NOT IN " + TERMINAL_SQL + ")").rowcount
            r2 = c.execute("UPDATE agents SET state='AVAILABLE',call_id=NULL,version=version+1,last_free_at=? "
                           "WHERE state IN ('RESERVED','DIALING') AND NOT EXISTS("
                           "SELECT 1 FROM calls WHERE id=agents.call_id AND state NOT IN " + TERMINAL_SQL + ")",
                           (self.clock.now(),)).rowcount
            if r1 or r2:
                self.count("orphans_repaired", c, r1 + r2)
            return r1 + r2

    # ---------------------------------------------------------------- reads for pacing / safety
    def record_decision(self, campaign, mode, requested, approved, action, bound, detail):
        self._conn().execute("INSERT INTO decisions(at,campaign_id,mode,requested,approved,action,bound,detail) "
                             "VALUES(?,?,?,?,?,?,?,?)", (self.clock.now(), campaign, mode, requested, approved,
                                                         action, int(bound), json.dumps(detail)))

    def agent_counts(self, campaign):
        d = {s.value: 0 for s in A}
        for r in self.q("SELECT state,COUNT(*) n FROM agents WHERE campaign_id=? GROUP BY state", (campaign,)):
            d[r["state"]] = r["n"]
        return d

    def snapshot(self, campaign, soon_horizon: float):
        now = self.clock.now()
        ag = self.agent_counts(campaign)
        soon = self.q("SELECT COUNT(*) FROM agents WHERE campaign_id=? AND state='WRAP_UP' AND pending IS NULL "
                      "AND wrap_until<=?", (campaign, now + soon_horizon))[0][0]
        u = b = held = 0
        for r in self.q("SELECT state,bound,agent_id IS NULL AS unb,COUNT(*) n FROM calls WHERE campaign_id=? "
                        "AND state NOT IN " + TERMINAL_SQL + " GROUP BY state,bound,unb", (campaign,)):
            if r["state"] in ("RESERVED", "INITIATED", "RINGING"):
                if r["bound"] and not r["unb"]:
                    b += r["n"]
                else:
                    u += r["n"]
            elif r["state"] == "ANSWERED":
                held += r["n"]
        ready = self.q("SELECT COUNT(*) FROM borrowers WHERE campaign_id=? AND status='READY' AND next_attempt_at<=?",
                       (campaign, now))[0][0]
        return Snapshot(campaign, now, ag, ag["AVAILABLE"], soon, u, b, held, ready)

    def recent_stats(self, campaign, long_n, short_n, abandon_n):
        rows = self.q("SELECT answered_at,initiated_at,connected_at,ended_at,state,end_reason FROM calls "
                      "WHERE campaign_id=? AND ended_at IS NOT NULL AND initiated_at IS NOT NULL "
                      "ORDER BY ended_at DESC LIMIT ?", (campaign, long_n))
        informative = [r for r in rows if r["answered_at"] or r["end_reason"] in ("NO_ANSWER", "BUSY")]
        answered_flags = [1 if r["answered_at"] else 0 for r in informative]
        pre = [r["answered_at"] - r["initiated_at"] for r in informative if r["answered_at"]]
        talk = [r["ended_at"] - r["connected_at"] for r in rows if r["connected_at"] and r["state"] == "COMPLETED"]
        ab = self.q("SELECT state FROM calls WHERE campaign_id=? AND answered_at IS NOT NULL "
                    "ORDER BY answered_at DESC LIMIT ?", (campaign, abandon_n))
        n_ab = sum(1 for r in ab if r["state"] == "ABANDONED")
        return Stats(answered_flags[:short_n], answered_flags,
                     sum(pre) / len(pre) if pre else 8.0, sum(talk) / len(talk) if talk else 60.0,
                     len(ab), n_ab)


@dataclass
class Snapshot:
    campaign_id: str
    now: float
    agents: dict
    free: int                 # AVAILABLE agents
    soon_free: int            # WRAP_UP agents freeing within the horizon
    unbound_preanswer: int    # predictive calls dialing/ringing with no agent attached
    bound_preanswer: int      # progressive calls with an agent reserved
    held: int                 # answered, waiting for an agent
    ready_borrowers: int


@dataclass
class Stats:
    flags_short: list
    flags_long: list
    avg_time_to_answer: float
    avg_talk: float
    answered_window: int      # answered calls in abandon window
    abandoned_window: int

    @property
    def abandon_rate(self):
        return self.abandoned_window / self.answered_window if self.answered_window else 0.0
