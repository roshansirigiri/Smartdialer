"""Explicit agent and call state machines."""
from enum import Enum


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


A = AgentState
AGENT_TRANSITIONS = {
    A.OFFLINE: {A.AVAILABLE, A.PAUSED},
    A.AVAILABLE: {A.RESERVED, A.CONNECTED, A.PAUSED, A.OFFLINE},
    A.RESERVED: {A.DIALING, A.CONNECTED, A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.DIALING: {A.CONNECTED, A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.CONNECTED: {A.WRAP_UP, A.OFFLINE},
    A.WRAP_UP: {A.AVAILABLE, A.PAUSED, A.OFFLINE},
    A.PAUSED: {A.AVAILABLE, A.OFFLINE},
}


class CallState(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"      # customer picked up (may be waiting for an agent)
    CONNECTED = "CONNECTED"    # bridged to an agent
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"    # answered by a human, no agent -> compliance-relevant


C = CallState
# Rank = position in the forward-only lifecycle. A transition is applied only if it
# moves strictly forward and the call is not terminal. This single rule makes duplicate
# and out-of-order provider events harmless (see store.apply_transition).
CALL_RANK = {C.QUEUED: 0, C.RESERVED: 1, C.INITIATED: 2, C.RINGING: 3, C.ANSWERED: 4,
             C.CONNECTED: 5, C.COMPLETED: 6, C.FAILED: 6, C.CANCELLED: 6, C.ABANDONED: 6}
TERMINAL = {C.COMPLETED, C.FAILED, C.CANCELLED, C.ABANDONED}
TERMINAL_SQL = "('COMPLETED','FAILED','CANCELLED','ABANDONED')"


def agent_transition_allowed(cur: AgentState, new: AgentState) -> bool:
    return new in AGENT_TRANSITIONS[cur]


def call_transition_allowed(cur: CallState, new: CallState) -> bool:
    return cur not in TERMINAL and CALL_RANK[new] > CALL_RANK[cur]
