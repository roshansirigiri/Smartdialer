# SmartDialer

Progressive **and** predictive dialing, with a Safety Controller the pacing logic cannot bypass.
Python 3.10+, standard library only, SQLite as the single source of truth. No install step.

```
python -m unittest discover -s tests      # 22 tests, <1s
python -m smartdialer.scenarios           # narrated demos of the 5 required failure cases
python -m smartdialer.sim                 # 6 scenarios x {progressive, predictive}; writes docs/RESULTS.md (~3 min)
python -m smartdialer.sim --quick         # 30s smoke run
python -m smartdialer.loadtest            # thread contention + 100/1k/10k agent tick cost
```

## Layout

| File | Role |
|---|---|
| `smartdialer/states.py` | Agent + call state machines (transition tables) |
| `smartdialer/store.py` | All state + atomic operations (CAS reservation, idempotent transitions, recovery reads) |
| `smartdialer/pacing.py` | `ProgressivePacing`, `PredictivePacing` (binomial overflow budget). Proposals only |
| `smartdialer/safety.py` | `SafetyController`: approve / reduce / reject / fall back; circuit breakers; abandon governor |
| `smartdialer/allocator.py` | Reserves agent+borrower, calls provider. Refuses to run without a one-shot `Approval` |
| `smartdialer/telecom.py` | `Provider` interface; mock A (fast, clean) and B (slow, timeouts, duplicate + reordered events) |
| `smartdialer/events.py`, `recovery.py` | Idempotent event ingestion; lease-based crash recovery |
| `smartdialer/dialer.py` | One stateless worker tick |
| `smartdialer/sim.py`, `loadtest.py`, `scenarios.py`, `rig.py` | Simulation, load test, failure demos, wiring |

Design, diagrams, ADRs, failure analysis and scaling: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Measured results: **[docs/RESULTS.md](docs/RESULTS.md)**.

> Note: the folder also contains `agents.py`, `calls.py`, `db.py`, `models.py` and `providers/` from an earlier session. They are not used by this implementation and were left untouched; delete them if unwanted.

## Real telecom

Implement `smartdialer.telecom.Provider` (`place_call` idempotent on `call_id`, `cancel`, `get_status`, `poll_events`; a webhook handler can feed `events.handle_event`). Nothing else changes.
