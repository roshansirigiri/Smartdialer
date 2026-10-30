# Simulation results

Virtual-clock simulation, 100 agents, Provider A (70%: fast/clean) + Provider B (30%: slow, timeouts, duplicate and reordered events). Utilization = share of logged-in agent-time spent CONNECTED (after a 60s warm-up).

| Scenario | Mode | Utilization | Idle (AVAILABLE) | Initiated | Answered | Connected | Abandoned | Abandon % | Avg in-flight vs capacity | Safety decisions |
|---|---|---|---|---|---|---|---|---|---|---|
| A 20%/120s | progressive | 52% | 0% | 2164 | 444 | 444 | 0 | 0.00% | 43.4 / 6.6 | APPROVE:900 |
| A 20%/120s | predictive | 75% | 19% | 3483 | 644 | 644 | 0 | 0.00% | 64.3 / 27.4 | APPROVE:899, REDUCE:1 |
| B 50%/90s | progressive | 66% | 0% | 1554 | 765 | 765 | 0 | 0.00% | 25.5 / 9.3 | APPROVE:900 |
| B 50%/90s | predictive | 77% | 14% | 1890 | 898 | 898 | 0 | 0.00% | 30.6 / 24.3 | APPROVE:900 |
| C 70%/180s | progressive | 86% | 0% | 746 | 521 | 521 | 0 | 0.00% | 10.7 / 5.6 | APPROVE:900 |
| C 70%/180s | predictive | 91% | 4% | 804 | 543 | 542 | 1 | 0.18% | 10.9 / 11.1 | APPROVE:900 |
| D changing | progressive | 69% | 0% | 1439 | 781 | 781 | 0 | 0.00% | 23.1 / 9.4 | APPROVE:900 |
| D changing | predictive | 79% | 12% | 1675 | 885 | 885 | 0 | 0.00% | 26.2 / 22.5 | APPROVE:900 |
| E provider outage 300-420s | progressive | 57% | 6% | 1642 | 636 | 636 | 0 | 0.00% | 31.1 / 13.1 | APPROVE:782, REDUCE:15, REJECT:103 |
| E provider outage 300-420s | predictive | 70% | 22% | 2113 | 772 | 772 | 0 | 0.00% | 37.5 / 30.2 | APPROVE:783, REDUCE:15, REJECT:102 |
| F 40/100 agents vanish @300s | progressive | 64% | 0% | 1265 | 516 | 516 | 0 | 0.00% | 22.2 / 6.4 | APPROVE:900 |
| F 40/100 agents vanish @300s | predictive | 75% | 16% | 1689 | 625 | 625 | 0 | 0.00% | 27.8 / 20.0 | APPROVE:900 |

## Time series: D changing (predictive)

| t(s) | util(60s) | in-flight | free agents | p used | requested | approved | action | abandon(win) |
|---|---|---|---|---|---|---|---|---|
| 0 | 0% | 0 | 100 | 1.0 | 100 | 100 | APPROVE | 0.0% |
| 60 | 60% | 18 | 3 | 0.602 | 0 | 0 | APPROVE | 0.0% |
| 120 | 82% | 18 | 7 | 0.739 | 0 | 0 | APPROVE | 0.0% |
| 180 | 82% | 24 | 12 | 0.672 | 2 | 2 | APPROVE | 0.0% |
| 240 | 80% | 26 | 13 | 0.694 | 2 | 2 | APPROVE | 0.0% |
| 300 | 78% | 29 | 13 | 0.672 | 1 | 1 | APPROVE | 0.0% |
| 360 | 78% | 23 | 10 | 0.554 | 1 | 1 | APPROVE | 0.0% |
| 420 | 80% | 23 | 13 | 0.649 | 0 | 0 | APPROVE | 0.0% |
| 480 | 75% | 32 | 18 | 0.672 | 2 | 2 | APPROVE | 0.0% |
| 540 | 80% | 17 | 7 | 0.803 | 1 | 1 | APPROVE | 0.0% |
| 600 | 76% | 22 | 9 | 0.666 | 1 | 1 | APPROVE | 0.0% |
| 660 | 83% | 13 | 7 | 0.672 | 1 | 1 | APPROVE | 0.0% |
| 720 | 74% | 31 | 18 | 0.587 | 2 | 2 | APPROVE | 0.0% |
| 780 | 82% | 23 | 10 | 0.602 | 1 | 1 | APPROVE | 0.0% |
| 840 | 80% | 27 | 14 | 0.844 | 0 | 0 | APPROVE | 0.0% |

## Time series: E provider outage 300-420s (predictive)

| t(s) | util(60s) | in-flight | free agents | p used | requested | approved | action | abandon(win) |
|---|---|---|---|---|---|---|---|---|
| 0 | 0% | 0 | 100 | 1.0 | 100 | 100 | APPROVE | 0.0% |
| 60 | 57% | 25 | 11 | 0.649 | 0 | 0 | APPROVE | 0.0% |
| 120 | 77% | 32 | 17 | 0.578 | 0 | 0 | APPROVE | 0.0% |
| 180 | 75% | 26 | 14 | 0.53 | 4 | 4 | APPROVE | 0.0% |
| 240 | 76% | 36 | 21 | 0.554 | 7 | 7 | APPROVE | 0.0% |
| 300 | 76% | 46 | 20 | 0.529 | 0 | 0 | APPROVE | 0.0% |
| 360 | 60% | 25 | 44 | 0.824 | 36 | 0 | REJECT | 0.0% |
| 420 | 32% | 22 | 70 | 0.994 | 57 | 0 | REJECT | 0.0% |
| 480 | 51% | 40 | 15 | 0.385 | 0 | 0 | APPROVE | 0.0% |
| 540 | 76% | 35 | 15 | 0.529 | 0 | 0 | APPROVE | 0.0% |
| 600 | 73% | 42 | 18 | 0.505 | 3 | 3 | APPROVE | 0.0% |
| 660 | 77% | 42 | 21 | 0.602 | 0 | 0 | APPROVE | 0.0% |
| 720 | 75% | 33 | 16 | 0.602 | 6 | 6 | APPROVE | 0.0% |
| 780 | 72% | 33 | 15 | 0.45 | 0 | 0 | APPROVE | 0.0% |
| 840 | 78% | 38 | 20 | 0.578 | 4 | 4 | APPROVE | 0.0% |

## Time series: F 40/100 agents vanish @300s (predictive)

| t(s) | util(60s) | in-flight | free agents | p used | requested | approved | action | abandon(win) |
|---|---|---|---|---|---|---|---|---|
| 0 | 0% | 0 | 100 | 1.0 | 100 | 100 | APPROVE | 0.0% |
| 60 | 57% | 25 | 11 | 0.649 | 0 | 0 | APPROVE | 0.0% |
| 120 | 77% | 32 | 17 | 0.578 | 0 | 0 | APPROVE | 0.0% |
| 180 | 75% | 26 | 14 | 0.53 | 4 | 4 | APPROVE | 0.0% |
| 240 | 76% | 36 | 21 | 0.554 | 7 | 7 | APPROVE | 0.0% |
| 300 | 76% | 31 | 13 | 0.529 | 0 | 0 | APPROVE | 0.0% |
| 360 | 79% | 19 | 14 | 0.602 | 2 | 2 | APPROVE | 0.0% |
| 420 | 76% | 28 | 14 | 0.529 | 0 | 0 | APPROVE | 0.0% |
| 480 | 72% | 17 | 7 | 0.578 | 0 | 0 | APPROVE | 0.0% |
| 540 | 79% | 16 | 6 | 0.649 | 1 | 1 | APPROVE | 0.0% |
| 600 | 76% | 28 | 14 | 0.505 | 1 | 1 | APPROVE | 0.0% |
| 660 | 72% | 23 | 12 | 0.505 | 1 | 1 | APPROVE | 0.0% |
| 720 | 70% | 29 | 18 | 0.626 | 4 | 4 | APPROVE | 0.0% |
| 780 | 72% | 23 | 9 | 0.505 | 1 | 1 | APPROVE | 0.0% |
| 840 | 74% | 21 | 7 | 0.454 | 0 | 0 | APPROVE | 0.0% |

Agent-drop reaction: `{"unbound_before_drop": 49, "capacity_before": 24, "unbound_after": 31, "capacity_after": 20}`
