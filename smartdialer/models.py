"""State machines (agent + call) and small value types.

The transition tables here are the single source of truth. The store refuses any
agent transition that is not in AGENT_TRANSITIONS, and call events are applied
with the monotonic CALL_RANK rule (see calls.apply_event).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"      # claimed for one specific call, not yet dialing
    DIALING = "DIALING"        # the provider accepted the call
    CONNECTED = "CONNECTED"    # a borrower is on the line
    WRAP_UP = "WRAP_UP"        # after-call work
    PAUSED = "PAUSED"          # agent asked for a break


A = AgentState
AGENT_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    A.OFFLINE: {A.AVAILABLE, A.PAUSED},
    # AVAILABLE -> CONNECTED is the predictive "late bind": an unbound call was
    # answered and an idle agent is claimed atomically at that moment.
    A.AVAILABLE: {A.RESERVED, A.CONNECTED, A.PAUSED, A.OFFLINE},
    A.RESERVED: {A.DIALING, A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.DIALING: {A.CONNECTED, A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.CONNECTED: {A.WRAP_UP, A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.WRAP_UP: {A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.PAUSED: {A.AVAILABLE, A.OFFLINE},
}


class CallState(str, Enum):
    QUEUED = "QUEUED"          # reserved for completeness; calls are born RESERVED
    RESERVED = "RESERVED"      # borrower (and agent, if bound) claimed
    INITIATED = "INITIATED"    # provider accepted the call
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"      # borrower picked up, no agent yet (predictive hold)
    CONNECTED = "CONNECTED"    # borrower + agent talking
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


C = CallState
TERMINAL_CALL_STATES = {C.COMPLETED, C.FAILED, C.CANCELLED}
# Forward-only ordering. An event may only move a call to a strictly higher rank.
CALL_RANK = {C.QUEUED: 0, C.RESERVED: 1, C.INITIATED: 2, C.RINGING: 3,
             C.ANSWERED: 4, C.CONNECTED: 5, C.COMPLETED: 9, C.FAILED: 9, C.CANCELLED: 9}

CALL_TRANSITIONS: dict[CallState, set[CallState]] = {
    C.QUEUED: {C.RESERVED, C.CANCELLED},
    C.RESERVED: {C.INITIATED, C.RINGING, C.ANSWERED, C.CONNECTED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.INITIATED: {C.RINGING, C.ANSWERED, C.CONNECTED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.RINGING: {C.ANSWERED, C.CONNECTED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.ANSWERED: {C.CONNECTED, C.COMPLETED, C.FAILED, C.CANCELLED},
    C.CONNECTED: {C.COMPLETED, C.FAILED},
    C.COMPLETED: set(), C.FAILED: set(), C.CANCELLED: set(),
}


class EventType(str, Enum):
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"          # reason: NO_ANSWER | BUSY | ERROR


class BorrowerState(str, Enum):
    READY = "READY"
    RESERVED = "RESERVED"
    DONE = "DONE"              # reached and spoken to
    EXHAUSTED = "EXHAUSTED"    # max attempts used


class Mode(str, Enum):
    PROGRESSIVE = "PROGRESSIVE"
    PREDICTIVE = "PREDICTIVE"


class Action(str, Enum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    FALLBACK_PROGRESSIVE = "FALLBACK_PROGRESSIVE"


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    event_id: str              # stable per logical event; duplicates share it
    provider_call_id: str
    idem_key: str              # our call_id, echoed back by the provider
    type: EventType
    ts: float
    reason: str = ""


@dataclass
class PacingRequest:
    """What a pacing engine is allowed to say: 'I would like N calls'. Nothing more."""
    campaign: str
    mode: Mode
    requested: int
    explain: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    campaign: str
    agents: dict            # AgentState.value -> count
    unbound_inflight: int   # predictive calls with no agent yet (reserved..answered)
    bound_inflight: int
    ready_borrowers: int

    @property
    def free(self) -> int:
        return self.agents.get("AVAILABLE", 0)

    @property
    def busy(self) -> int:
        return self.agents.get("CONNECTED", 0) + self.agents.get("WRAP_UP", 0)

    @property
    def logged_in(self) -> int:
        return sum(v for k, v in self.agents.items() if k not in ("OFFLINE", "PAUSED"))


@dataclass(frozen=True)
class Decision:
    action: Action
    approved: int
    bound: bool                 # True => agent is reserved BEFORE dialing (progressive semantics)
    unbound_ceiling: int        # absolute cap on unbound in-flight, enforced in the DB txn
    reasons: tuple = ()
    providers: tuple = ()       # providers the allocator may use
