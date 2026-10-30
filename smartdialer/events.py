"""Provider event ingestion. Stateless and idempotent: any worker may process any event, any
number of times, in any order; Store.apply_transition decides (forward-only + dedupe)."""
from .states import CallState as C

EVENT_TO_STATE = {"INITIATED": C.INITIATED, "RINGING": C.RINGING, "ANSWERED": C.ANSWERED,
                  "COMPLETED": C.COMPLETED, "NO_ANSWER": C.FAILED, "BUSY": C.FAILED,
                  "FAILED": C.FAILED, "CANCELLED": C.CANCELLED}


def handle_event(store, ev):
    target = EVENT_TO_STATE.get(ev.type)
    if target is None:
        store.count("unknown_event_type")
        return None
    reason = ev.type if target == C.FAILED else None
    return store.apply_transition(ev.call_id, target, cause=f"event:{ev.type}",
                                  event_key=(ev.provider, ev.event_id), reason=reason, provider=ev.provider)
