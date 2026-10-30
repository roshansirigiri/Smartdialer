"""Call + borrower lifecycle. Every function runs inside a caller-supplied transaction.

Rules that make this safe against a hostile provider:
  * Calls are born RESERVED together with their agent/borrower claim, in one txn.
  * Provider events are deduplicated by (provider, event_id) in the same txn that
    applies them, so "applied" and "recorded as seen" can never diverge.
  * Even a *new* event id cannot move a call backwards: events only apply when
    they advance CALL_RANK, and terminal states are absorbing.
"""
from __future__ import annotations

import uuid

from . import agents as ag
from .config import DialerConfig
from .models import (CALL_RANK, TERMINAL_CALL_STATES, AgentState as A, CallState as S,
                     EventType as E, Snapshot)

LIVE_PRE_ANSWER = ("RESERVED", "INITIATED", "RINGING")
UNBOUND_INFLIGHT = ("RESERVED", "INITIATED", "RINGING", "ANSWERED")


# ---------------------------------------------------------------- borrowers
def release_borrower(c, cfg: DialerConfig, borrower_id: str, now: float,
                     count_attempt: bool, delay: float) -> None:
    row = c.execute("SELECT attempts FROM borrowers WHERE borrower_id=?", (borrower_id,)).fetchone()
    if row is None:
        return
    attempts = row["attempts"] + (1 if count_attempt else 0)
    state = "EXHAUSTED" if attempts >= cfg.max_attempts else "READY"
    c.execute("UPDATE borrowers SET state=?, attempts=?, next_attempt_at=?, call_id=NULL "
              "WHERE borrower_id=? AND state='RESERVED'", (state, attempts, now + delay, borrower_id))


def _borrower_done(c, borrower_id: str) -> None:
    c.execute("UPDATE borrowers SET state='DONE', call_id=NULL WHERE borrower_id=?", (borrower_id,))


def _ready_borrowers(c, campaign: str, now: float, limit: int):
    return c.execute(
        "SELECT borrower_id, phone FROM borrowers WHERE campaign=? AND state='READY' AND next_attempt_at<=? "
        "ORDER BY priority DESC, next_attempt_at, borrower_id LIMIT ?", (campaign, now, limit)).fetchall()


def _claim_borrower(c, borrower_id: str, call_id: str) -> bool:
    cur = c.execute("UPDATE borrowers SET state='RESERVED', call_id=? WHERE borrower_id=? AND state='READY'",
                    (call_id, borrower_id))
    return cur.rowcount == 1


def _insert_call(c, call_id, campaign, agent_id, borrower_id, mode, now):
    c.execute("INSERT INTO calls(call_id,campaign,agent_id,borrower_id,state,mode,created_at,updated_at) "
              "VALUES(?,?,?,?,?,?,?,?)", (call_id, campaign, agent_id, borrower_id, "RESERVED", mode, now, now))


# ---------------------------------------------------------------- reservation
def reserve_bound(c, campaign: str, now: float, n: int) -> list[dict]:
    """Progressive: pick n idle agents + n borrowers and bind them 1:1. Never returns more than idle agents."""
    if n <= 0:
        return []
    agents = ag.idle_agents(c, campaign, n)
    borrowers = _ready_borrowers(c, campaign, now, len(agents))
    out = []
    for a, b in zip(agents, borrowers):
        call_id = uuid.uuid4().hex
        if not ag.cas(c, a["agent_id"], A.AVAILABLE, A.RESERVED, now, call_id=call_id):
            continue  # lost the race for this agent
        if not _claim_borrower(c, b["borrower_id"], call_id):
            ag.cas(c, a["agent_id"], A.RESERVED, A.AVAILABLE, now)  # undo, give agent back
            continue
        _insert_call(c, call_id, campaign, a["agent_id"], b["borrower_id"], "PROGRESSIVE", now)
        out.append(dict(call_id=call_id, phone=b["phone"], agent_id=a["agent_id"], borrower_id=b["borrower_id"]))
    return out


def count_unbound_inflight(c, campaign: str) -> int:
    return c.execute(
        "SELECT COUNT(*) FROM calls WHERE campaign=? AND agent_id IS NULL AND mode='PREDICTIVE' "
        "AND state IN ('RESERVED','INITIATED','RINGING','ANSWERED')", (campaign,)).fetchone()[0]


def reserve_unbound(c, campaign: str, now: float, n: int, ceiling: int) -> list[dict]:
    """Predictive: no agent yet. The ceiling is re-checked *inside* the txn, so two workers that
    each computed 'room for 15' cannot both create 15."""
    room = ceiling - count_unbound_inflight(c, campaign)
    n = min(n, room)
    if n <= 0:
        return []
    out = []
    for b in _ready_borrowers(c, campaign, now, n):
        call_id = uuid.uuid4().hex
        if not _claim_borrower(c, b["borrower_id"], call_id):
            continue
        _insert_call(c, call_id, campaign, None, b["borrower_id"], "PREDICTIVE", now)
        out.append(dict(call_id=call_id, phone=b["phone"], agent_id=None, borrower_id=b["borrower_id"]))
    return out


# ---------------------------------------------------------------- call transitions
def _get_call(c, call_id: str):
    return c.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()


def _set_state(c, call_id: str, expect: str, new: str, now: float, **cols) -> bool:
    sets = ["state=?", "updated_at=?"]
    args = [new, now]
    if new in ("COMPLETED", "FAILED", "CANCELLED"):
        sets.append("ended_at=?")
        args.append(now)
    for k, v in cols.items():
        sets.append(f"{k}=?")
        args.append(v)
    args += [call_id, expect]
    return c.execute(f"UPDATE calls SET {', '.join(sets)} WHERE call_id=? AND state=?", args).rowcount == 1


def mark_initiated(c, call_id: str, provider: str, provider_call_id: str, now: float) -> bool:
    """RESERVED -> INITIATED. False means the call is no longer RESERVED (cancelled/reaped):
    the caller must hang up at the provider."""
    if not _set_state(c, call_id, "RESERVED", "INITIATED", now, provider=provider,
                      provider_call_id=provider_call_id, initiated_at=now):
        return False
    call = _get_call(c, call_id)
    if call["agent_id"]:
        ag.cas(c, call["agent_id"], A.RESERVED, A.DIALING, now, call_id=call_id)
    return True


def fail_setup(c, cfg: DialerConfig, call_id: str, reason: str, now: float, count_attempt: bool = False) -> bool:
    call = _get_call(c, call_id)
    if call is None or not _set_state(c, call_id, "RESERVED", "FAILED", now, fail_reason=reason):
        return False
    if call["agent_id"]:
        ag.release_after_call(c, call["agent_id"], now, talked=False, wrap_s=cfg.wrap_up_s)
    release_borrower(c, cfg, call["borrower_id"], now, count_attempt, cfg.provider_error_delay_s)
    return True


def cancel_call(c, cfg: DialerConfig, call_id: str, reason: str, now: float):
    """Cancel a not-yet-connected call. Returns (provider, provider_call_id) to hang up, or None."""
    call = _get_call(c, call_id)
    if call is None or S(call["state"]) in TERMINAL_CALL_STATES or call["state"] == "CONNECTED":
        return None
    if not _set_state(c, call_id, call["state"], "CANCELLED", now, fail_reason=reason):
        return None
    if call["agent_id"]:
        ag.release_after_call(c, call["agent_id"], now, talked=False, wrap_s=cfg.wrap_up_s)
    release_borrower(c, cfg, call["borrower_id"], now, False, cfg.provider_error_delay_s)
    return (call["provider"], call["provider_call_id"]) if call["provider_call_id"] else (None, None)


def adopt_placement(c, call_id: str, provider: str, provider_call_id: str, now: float) -> bool:
    """Crash recovery: the provider has the call but we never recorded it."""
    return mark_initiated(c, call_id, provider, provider_call_id, now)


# ---------------------------------------------------------------- provider events
def apply_event(c, cfg: DialerConfig, ev, now: float):
    """Idempotent, order-tolerant. Returns (outcome, provider_call_id_to_hangup_or_None)."""
    ins = c.execute("INSERT OR IGNORE INTO call_events(provider,event_id,call_id,type,outcome,ts) "
                    "VALUES(?,?,?,?,?,?)", (ev.provider, ev.event_id, ev.idem_key, ev.type.value, "PENDING", now))
    if ins.rowcount == 0:
        return "DUPLICATE", None
    outcome, hangup = _apply(c, cfg, ev, now)
    c.execute("UPDATE call_events SET outcome=? WHERE provider=? AND event_id=?", (outcome, ev.provider, ev.event_id))
    return outcome, hangup


def _apply(c, cfg, ev, now):
    call = _get_call(c, ev.idem_key)
    if call is None:
        row = c.execute("SELECT * FROM calls WHERE provider=? AND provider_call_id=?",
                        (ev.provider, ev.provider_call_id)).fetchone()
        call = row
    if call is None:
        return "UNKNOWN_CALL", None
    call_id = call["call_id"]
    state = S(call["state"])

    if state in TERMINAL_CALL_STATES:
        # e.g. we cancelled/timed it out but the provider kept going: make sure it is torn down
        if ev.type == E.ANSWERED and state != S.COMPLETED and call["answered_at"] is None:
            return "ORPHAN_ANSWER", ev.provider_call_id
        return "STALE_IGNORED", None

    if state == S.RESERVED:
        # provider event before we recorded INITIATED (crash between place_call and mark_initiated)
        mark_initiated(c, call_id, ev.provider, ev.provider_call_id, now)
        call = _get_call(c, call_id)
        state = S(call["state"])

    if ev.type == E.RINGING:
        if CALL_RANK[state] < CALL_RANK[S.RINGING] and _set_state(c, call_id, state.value, "RINGING", now):
            return "APPLIED", None
        return "STALE_IGNORED", None

    if ev.type == E.ANSWERED:
        if CALL_RANK[state] >= CALL_RANK[S.ANSWERED]:
            return "STALE_IGNORED", None
        return _on_answered(c, call, state, now), None

    if ev.type == E.COMPLETED:
        return _on_completed(c, cfg, call, state, now), None

    if ev.type == E.FAILED:
        return _on_failed(c, cfg, call, state, ev.reason or "ERROR", now), None
    return "UNKNOWN_TYPE", None


def _on_answered(c, call, state: S, now: float) -> str:
    call_id = call["call_id"]
    if call["agent_id"]:  # bound (progressive): the agent was reserved before dialing
        arow = ag.get(c, call["agent_id"])
        if arow["state"] in ("DIALING", "RESERVED"):
            ag.cas(c, call["agent_id"], A(arow["state"]), A.CONNECTED, now, call_id=call_id)
        _set_state(c, call_id, state.value, "CONNECTED", now, answered_at=now, connected_at=now)
        return "APPLIED"
    agent_id = ag.claim_idle(c, call["campaign"], now, call_id, new=A.CONNECTED)  # late bind
    if agent_id:
        _set_state(c, call_id, state.value, "CONNECTED", now, answered_at=now, connected_at=now, agent_id=agent_id)
        return "APPLIED"
    _set_state(c, call_id, state.value, "ANSWERED", now, answered_at=now)  # held: no agent yet
    return "HELD_NO_AGENT"


def _on_completed(c, cfg, call, state: S, now: float) -> str:
    call_id = call["call_id"]
    if state == S.CONNECTED:
        _set_state(c, call_id, "CONNECTED", "COMPLETED", now)
        if call["agent_id"]:
            ag.release_after_call(c, call["agent_id"], now, talked=True, wrap_s=cfg.wrap_up_s)
        _borrower_done(c, call["borrower_id"])
        return "APPLIED"
    if state == S.ANSWERED:  # borrower hung up while we were still hunting for an agent
        _set_state(c, call_id, "ANSWERED", "FAILED", now, fail_reason="ABANDONED_HUNGUP", abandoned=1)
        release_borrower(c, cfg, call["borrower_id"], now, False, cfg.retry_delay_s)
        return "ABANDONED"
    # COMPLETED seen before any ANSWERED: we never saw a conversation start, so no agent time was used.
    _set_state(c, call_id, state.value, "COMPLETED", now, fail_reason="NO_CONVERSATION_SEEN")
    if call["agent_id"]:
        ag.release_after_call(c, call["agent_id"], now, talked=False, wrap_s=cfg.wrap_up_s)
    release_borrower(c, cfg, call["borrower_id"], now, True, cfg.retry_delay_s)
    return "APPLIED_EARLY_COMPLETE"


def _on_failed(c, cfg, call, state: S, reason: str, now: float) -> str:
    call_id = call["call_id"]
    if state == S.ANSWERED:
        _set_state(c, call_id, "ANSWERED", "FAILED", now, fail_reason="ABANDONED_" + reason, abandoned=1)
        release_borrower(c, cfg, call["borrower_id"], now, False, cfg.retry_delay_s)
        return "ABANDONED"
    if state == S.CONNECTED:  # dropped mid-conversation
        _set_state(c, call_id, "CONNECTED", "FAILED", now, fail_reason="DROPPED")
        if call["agent_id"]:
            ag.release_after_call(c, call["agent_id"], now, talked=True, wrap_s=cfg.wrap_up_s)
        release_borrower(c, cfg, call["borrower_id"], now, True, cfg.retry_delay_s)
        return "APPLIED"
    _set_state(c, call_id, state.value, "FAILED", now, fail_reason=reason)
    if call["agent_id"]:
        ag.release_after_call(c, call["agent_id"], now, talked=False, wrap_s=cfg.wrap_up_s)
    ours = reason == "ERROR"
    release_borrower(c, cfg, call["borrower_id"], now, not ours,
                     cfg.provider_error_delay_s if ours else cfg.retry_delay_s)
    return "APPLIED"


# ---------------------------------------------------------------- predictive hold queue
def assign_held(c, campaign: str, now: float) -> int:
    rows = c.execute("SELECT call_id FROM calls WHERE campaign=? AND state='ANSWERED' AND agent_id IS NULL "
                     "ORDER BY answered_at", (campaign,)).fetchall()
    n = 0
    for r in rows:
        agent_id = ag.claim_idle(c, campaign, now, r["call_id"], new=A.CONNECTED)
        if agent_id is None:
            break
        if _set_state(c, r["call_id"], "ANSWERED", "CONNECTED", now, agent_id=agent_id, connected_at=now):
            n += 1
        else:
            ag.cas(c, agent_id, A.CONNECTED, A.AVAILABLE, now)
    return n


def expire_held(c, cfg: DialerConfig, campaign: str, now: float) -> list[tuple]:
    """Answered but no agent within hold_s => abandoned. This is the compliance event."""
    rows = c.execute("SELECT * FROM calls WHERE campaign=? AND state='ANSWERED' AND agent_id IS NULL "
                     "AND answered_at<=?", (campaign, now - cfg.hold_s)).fetchall()
    out = []
    for r in rows:
        if _set_state(c, r["call_id"], "ANSWERED", "FAILED", now, fail_reason="ABANDONED_NO_AGENT", abandoned=1):
            release_borrower(c, cfg, r["borrower_id"], now, False, cfg.retry_delay_s)
            out.append((r["provider"], r["provider_call_id"]))
    return out


# ---------------------------------------------------------------- agent lifecycle events
def agent_event(c, cfg: DialerConfig, agent_id: str, event: str, now: float) -> list[tuple]:
    """LOGIN | RESUME | LOGOUT | PAUSE | DISCONNECT. Returns provider calls to hang up."""
    row = ag.get(c, agent_id)
    if row is None:
        return []
    st = A(row["state"])
    hangups = []
    if event in ("LOGIN", "RESUME"):
        if st in (A.OFFLINE, A.PAUSED):
            ag.cas(c, agent_id, st, A.AVAILABLE, now)
        return hangups
    target = A.PAUSED if event == "PAUSE" else A.OFFLINE
    if st == target or (st == A.OFFLINE):
        return hangups
    if st in (A.RESERVED, A.DIALING):
        h = cancel_call(c, cfg, row["call_id"], "AGENT_UNAVAILABLE", now) if row["call_id"] else None
        if h and h[1]:
            hangups.append(h)
        row = ag.get(c, agent_id)
        st = A(row["state"])
    if st == A.CONNECTED:
        if event == "DISCONNECT":  # hard loss: the borrower is on the line with nobody
            call_id = row["call_id"]
            if call_id and _set_state(c, call_id, "CONNECTED", "FAILED", now, fail_reason="AGENT_LOST", abandoned=1):
                call = _get_call(c, call_id)
                release_borrower(c, cfg, call["borrower_id"], now, False, cfg.provider_error_delay_s)
                if call["provider_call_id"]:
                    hangups.append((call["provider"], call["provider_call_id"]))
            ag.cas(c, agent_id, A.CONNECTED, A.OFFLINE, now)
            c.execute("UPDATE agents SET pending_state=NULL WHERE agent_id=?", (agent_id,))
        else:  # graceful: finish the call first
            c.execute("UPDATE agents SET pending_state=? WHERE agent_id=?", (target.value, agent_id))
        return hangups
    if st in (A.AVAILABLE, A.WRAP_UP, A.PAUSED):
        ag.cas(c, agent_id, st, target, now)
    return hangups


# ---------------------------------------------------------------- reads used by pacing / safety
def snapshot(c, campaign: str, now: float, cap: int = 5000) -> Snapshot:
    agents = {r["state"]: r["n"] for r in c.execute(
        "SELECT state, COUNT(*) n FROM agents WHERE campaign=? GROUP BY state", (campaign,))}
    unbound = count_unbound_inflight(c, campaign)
    bound = c.execute("SELECT COUNT(*) FROM calls WHERE campaign=? AND agent_id IS NOT NULL "
                      "AND state IN ('RESERVED','INITIATED','RINGING')", (campaign,)).fetchone()[0]
    ready = c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM borrowers WHERE campaign=? AND state='READY' "
                      "AND next_attempt_at<=? LIMIT ?)", (campaign, now, cap)).fetchone()[0]
    return Snapshot(campaign, agents, unbound, bound, ready)


def campaign_stats(c, campaign: str, now: float, window: float) -> dict:
    since = now - window
    answered = c.execute("SELECT COUNT(*) FROM calls WHERE campaign=? AND answered_at>=?", (campaign, since)).fetchone()[0]
    noans = c.execute("SELECT COUNT(*) FROM calls WHERE campaign=? AND state='FAILED' AND ended_at>=? AND answered_at IS NULL "
                      "AND fail_reason IN ('NO_ANSWER','BUSY','RING_TIMEOUT')", (campaign, since)).fetchone()[0]
    talk = c.execute("SELECT AVG(ended_at-connected_at), COUNT(*) FROM calls WHERE campaign=? AND state='COMPLETED' "
                     "AND connected_at IS NOT NULL AND ended_at>=?", (campaign, since)).fetchone()
    setup = c.execute("SELECT AVG(answered_at-initiated_at), COUNT(*) FROM calls WHERE campaign=? AND answered_at>=? "
                      "AND initiated_at IS NOT NULL", (campaign, since)).fetchone()
    return dict(answered=answered, resolved=answered + noans, avg_talk=talk[0], n_talk=talk[1],
                avg_setup=setup[0], n_setup=setup[1])


def abandon_stats(c, campaign: str, now: float, window: float) -> tuple[int, int]:
    since = now - window
    answered = c.execute("SELECT COUNT(*) FROM calls WHERE campaign=? AND answered_at>=?", (campaign, since)).fetchone()[0]
    abandoned = c.execute("SELECT COUNT(*) FROM calls WHERE campaign=? AND abandoned=1 AND ended_at>=?",
                          (campaign, since)).fetchone()[0]
    return answered, abandoned


# ---------------------------------------------------------------- recovery sweeps
def stale_reserved(c, campaign: str, older_than: float):
    return c.execute("SELECT * FROM calls WHERE campaign=? AND state='RESERVED' AND created_at<=?",
                     (campaign, older_than)).fetchall()


def stale_inflight(c, campaign: str, older_than: float):
    return c.execute("SELECT * FROM calls WHERE campaign=? AND state IN ('INITIATED','RINGING') AND initiated_at<=?",
                     (campaign, older_than)).fetchall()


def stale_connected(c, campaign: str, older_than: float):
    return c.execute("SELECT * FROM calls WHERE campaign=? AND state='CONNECTED' AND connected_at<=?",
                     (campaign, older_than)).fetchall()


def fail_stale(c, cfg, call_id: str, reason: str, now: float):
    call = _get_call(c, call_id)
    if call is None or call["state"] not in ("INITIATED", "RINGING"):
        return None
    _set_state(c, call_id, call["state"], "FAILED", now, fail_reason=reason)
    if call["agent_id"]:
        ag.release_after_call(c, call["agent_id"], now, talked=False, wrap_s=cfg.wrap_up_s)
    release_borrower(c, cfg, call["borrower_id"], now, True, cfg.retry_delay_s)
    return (call["provider"], call["provider_call_id"])


def force_complete(c, cfg, call_id: str, now: float):
    call = _get_call(c, call_id)
    if call is None or call["state"] != "CONNECTED":
        return None
    _set_state(c, call_id, "CONNECTED", "COMPLETED", now, fail_reason="MAX_DURATION")
    if call["agent_id"]:
        ag.release_after_call(c, call["agent_id"], now, talked=True, wrap_s=cfg.wrap_up_s)
    _borrower_done(c, call["borrower_id"])
    return (call["provider"], call["provider_call_id"])


def repair_orphans(c, cfg: DialerConfig, campaign: str, now: float) -> dict:
    """Invariant repair: nothing may stay claimed by a call that is terminal or missing."""
    fixed = {"agents": 0, "borrowers": 0}
    rows = c.execute(
        "SELECT a.agent_id, a.state FROM agents a WHERE a.campaign=? AND a.state IN ('RESERVED','DIALING','CONNECTED') "
        "AND NOT EXISTS (SELECT 1 FROM calls k WHERE k.call_id=a.call_id AND k.state NOT IN ('COMPLETED','FAILED','CANCELLED'))",
        (campaign,)).fetchall()
    for r in rows:
        if ag.cas(c, r["agent_id"], A(r["state"]), A.AVAILABLE, now):
            fixed["agents"] += 1
    rows = c.execute(
        "SELECT b.borrower_id FROM borrowers b WHERE b.campaign=? AND b.state='RESERVED' "
        "AND NOT EXISTS (SELECT 1 FROM calls k WHERE k.call_id=b.call_id AND k.state NOT IN ('COMPLETED','FAILED','CANCELLED'))",
        (campaign,)).fetchall()
    for r in rows:
        release_borrower(c, cfg, r["borrower_id"], now, False, cfg.provider_error_delay_s)
        fixed["borrowers"] += 1
    return fixed
