"""Mock telecom providers with very different personalities, driven by virtual time."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Optional

from ..models import EventType as E, ProviderEvent
from .base import ProviderRejected, ProviderTimeout, TelecomProvider


@dataclass
class Outcome:
    kind: str          # ANSWER | NO_ANSWER | BUSY
    talk_s: float = 0.0


BorrowerModel = Callable[[str, float, random.Random], Outcome]


def constant_borrowers(answer_rate: float, mean_talk_s: float) -> BorrowerModel:
    def model(phone, now, rng):
        if rng.random() < answer_rate:
            return Outcome("ANSWER", max(10.0, rng.gauss(mean_talk_s, mean_talk_s * 0.3)))
        return Outcome("BUSY" if rng.random() < 0.15 else "NO_ANSWER")
    return model


@dataclass
class Profile:
    place_latency: tuple = (0.2, 0.5)     # delay before the network starts ringing
    ring_range: tuple = (5.0, 12.0)       # ring time before an answer
    no_answer_range: tuple = (20.0, 30.0)
    reject_rate: float = 0.0              # definite failure at placement
    timeout_rate: float = 0.0             # placement times out, call NOT created
    ghost_rate: float = 0.0               # placement times out but the call WAS created
    dup_rate: float = 0.0                 # event delivered a second time
    reorder_rate: float = 0.0             # event delayed so it can overtake/be overtaken
    reorder_max_s: float = 0.0
    jitter_s: float = 0.1


PROVIDER_A = Profile(place_latency=(0.2, 0.5), ring_range=(5, 12), reject_rate=0.005, jitter_s=0.1)
PROVIDER_B = Profile(place_latency=(1.0, 3.0), ring_range=(8, 20), reject_rate=0.02, timeout_rate=0.03,
                     ghost_rate=0.01, dup_rate=0.10, reorder_rate=0.10, reorder_max_s=6.0, jitter_s=1.5)


class MockProvider(TelecomProvider):
    def __init__(self, name: str, profile: Profile, model: BorrowerModel, seed: int = 1):
        self.name = name
        self.profile = profile
        self.model = model
        self.rng = random.Random(seed)
        self.outage = False
        self._by_key: dict[str, dict] = {}
        self._by_pcid: dict[str, dict] = {}
        self._entries: list[list] = []      # [deliver_at, event, is_dup, call_key]
        self._n = 0
        self.stats = dict(placed=0, timeouts=0, rejected=0, ghosts=0, dups=0, reordered=0)

    # ---- API
    def place_call(self, idem_key, phone, now):
        self._maybe_fail()
        p = self.profile
        r = self.rng.random()
        if r < p.timeout_rate:
            self.stats["timeouts"] += 1
            raise ProviderTimeout(f"{self.name}: placement timeout")
        if r < p.timeout_rate + p.reject_rate:
            self.stats["rejected"] += 1
            raise ProviderRejected(f"{self.name}: rejected")
        rec = self._by_key.get(idem_key)
        if rec is None:
            rec = self._create(idem_key, phone, now)
        if self.rng.random() < p.ghost_rate and rec["fresh"]:
            rec["fresh"] = False
            self.stats["ghosts"] += 1
            raise ProviderTimeout(f"{self.name}: timeout after accept (ghost)")
        rec["fresh"] = False
        return rec["pcid"]

    def lookup(self, idem_key) -> Optional[str]:
        self._maybe_fail()
        rec = self._by_key.get(idem_key)
        return rec["pcid"] if rec else None

    def hangup(self, pcid, now):
        self._maybe_fail()
        rec = self._by_pcid.get(pcid)
        if rec is None or rec["hung_up"]:
            return
        rec["hung_up"] = True
        key = rec["key"]
        self._entries = [e for e in self._entries if not (e[3] == key and e[0] > now)]
        if not rec["completed_sent"]:
            rec["completed_sent"] = True
            self._emit(rec, E.COMPLETED if rec["answered"] else E.FAILED, now, reason="" if rec["answered"] else "NO_ANSWER")

    def poll_events(self, now):
        self._maybe_fail()
        due = [e for e in self._entries if e[0] <= now]
        due.sort(key=lambda e: e[0])
        self._entries = [e for e in self._entries if not (e[0] <= now and e[2])]  # dups are delivered once
        return [e[1] for e in due]

    def ack(self, event_ids):
        ids = set(event_ids)
        self._entries = [e for e in self._entries if e[2] or e[1].event_id not in ids]

    # ---- internals
    def _maybe_fail(self):
        if self.outage:
            self.stats["timeouts"] += 1
            raise ProviderTimeout(f"{self.name}: outage")

    def _create(self, key, phone, now):
        p, rng = self.profile, self.rng
        self._n += 1
        pcid = f"{self.name}-{self._n}"
        outcome = self.model(phone, now, rng)
        rec = dict(key=key, pcid=pcid, answered=False, hung_up=False, completed_sent=False, fresh=True, seq=0)
        self._by_key[key] = rec
        self._by_pcid[pcid] = rec
        self.stats["placed"] += 1
        t0 = now + rng.uniform(*p.place_latency)
        self._emit(rec, E.RINGING, t0 + 0.5)
        if outcome.kind == "ANSWER":
            t_ans = t0 + rng.uniform(*p.ring_range)
            rec["answered_planned"] = True
            self._emit(rec, E.ANSWERED, t_ans, mark_answered=True)
            self._emit(rec, E.COMPLETED, t_ans + outcome.talk_s, final=True)
        elif outcome.kind == "BUSY":
            self._emit(rec, E.FAILED, t0 + 2.0, reason="BUSY", final=True)
        else:
            self._emit(rec, E.FAILED, t0 + rng.uniform(*p.no_answer_range), reason="NO_ANSWER", final=True)
        return rec

    def _emit(self, rec, etype, at, reason="", mark_answered=False, final=False):
        p, rng = self.profile, self.rng
        rec["seq"] += 1
        ev = ProviderEvent(self.name, f"{rec['pcid']}:{etype.value}:{rec['seq']}", rec["pcid"], rec["key"],
                           etype, at, reason)
        deliver = at + rng.uniform(0, p.jitter_s)
        if rng.random() < p.reorder_rate:
            deliver += rng.uniform(0, p.reorder_max_s)
            self.stats["reordered"] += 1
        self._entries.append([deliver, ev, False, rec["key"]])
        if rng.random() < p.dup_rate:
            self.stats["dups"] += 1
            self._entries.append([deliver + rng.uniform(0.0, 3.0), ev, True, rec["key"]])
        if mark_answered:
            rec["answered"] = True
        if final and etype != E.RINGING:
            pass
