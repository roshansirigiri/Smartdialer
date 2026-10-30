"""Agent-side primitives. All functions take an open transaction connection `c`.

The only way an agent changes state is `cas`: UPDATE ... WHERE agent_id=? AND
state=<expected>. If two workers race for the same AVAILABLE agent, exactly one
UPDATE matches a row (rowcount == 1); the other sees 0 and must pick another.
"""
from __future__ import annotations

from .models import AGENT_TRANSITIONS, AgentState as A


def cas(c, agent_id: str, expect: A, new: A, now: float, call_id=None, wrap_until=None) -> bool:
    if new not in AGENT_TRANSITIONS[expect]:
        raise ValueError(f"illegal agent transition {expect.value} -> {new.value}")
    cur = c.execute(
        "UPDATE agents SET state=?, version=version+1, state_since=?, call_id=?, wrap_until=? "
        "WHERE agent_id=? AND state=?",
        (new.value, now, call_id, wrap_until, agent_id, expect.value))
    return cur.rowcount == 1


def get(c, agent_id: str):
    return c.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()


def idle_agents(c, campaign: str, limit: int):
    # longest-idle first: fair, and deterministic
    return c.execute(
        "SELECT agent_id FROM agents WHERE campaign=? AND state='AVAILABLE' "
        "ORDER BY state_since, agent_id LIMIT ?", (campaign, limit)).fetchall()


def claim_idle(c, campaign: str, now: float, call_id: str, new: A = A.RESERVED):
    """Atomically take one idle agent. Returns agent_id or None."""
    for row in idle_agents(c, campaign, 5):
        if cas(c, row["agent_id"], A.AVAILABLE, new, now, call_id=call_id):
            return row["agent_id"]
    return None


def release_after_call(c, agent_id: str, now: float, talked: bool, wrap_s: float) -> None:
    """Call ended: send the agent where it belongs (honouring a pending logout/pause)."""
    row = get(c, agent_id)
    if row is None:
        return
    cur = A(row["state"])
    if cur in (A.OFFLINE, A.PAUSED, A.AVAILABLE, A.WRAP_UP):
        return  # already released (idempotent)
    pending = row["pending_state"]
    if pending:
        target = A(pending)
    else:
        target = A.WRAP_UP if talked else A.AVAILABLE
    wrap_until = now + wrap_s if target == A.WRAP_UP else None
    if cas(c, agent_id, cur, target, now, call_id=None, wrap_until=wrap_until):
        c.execute("UPDATE agents SET pending_state=NULL WHERE agent_id=?", (agent_id,))


def finish_wrap_ups(c, campaign: str, now: float) -> int:
    rows = c.execute(
        "SELECT agent_id, pending_state FROM agents WHERE campaign=? AND state='WRAP_UP' AND wrap_until<=?",
        (campaign, now)).fetchall()
    n = 0
    for r in rows:
        target = A(r["pending_state"]) if r["pending_state"] else A.AVAILABLE
        if cas(c, r["agent_id"], A.WRAP_UP, target, now):
            c.execute("UPDATE agents SET pending_state=NULL WHERE agent_id=?", (r["agent_id"],))
            n += 1
    return n
