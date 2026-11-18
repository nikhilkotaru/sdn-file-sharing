# Experiment results

## E0 Functional tests

| Test | Result |
|---|---|
| ping with valid credentials | PASS |
| wrong password rejected | PASS |
| upload 1MB (alice) | PASS |
| list shows uploaded file | PASS |
| info has sha256 metadata | PASS |
| download + SHA-256 verify (bob) | PASS |
| readonly user cannot upload | PASS |
| non-owner cannot delete | PASS |
| path traversal name rejected | PASS |
| owner can delete | PASS |

## E1 Concurrent downloads: static vs dynamic routing

| Clients | Static agg. Mbps | Dynamic agg. Mbps | Static avg time (s) | Dynamic avg time (s) | Static fairness | Dynamic fairness |
|---|---|---|---|---|---|---|
| 1 | 3.175 | 3.582 | 13.25 | 11.883 | 1.0 | 1.0 |
| 2 | 2.506 | 11.977 | 38.018 | 6.984 | 0.998 | 1.0 |
| 4 | 2.079 | 9.878 | 75.396 | 15.468 | 0.934 | 0.982 |
| 8 | 2.134 | 7.133 | 134.025 | 42.817 | 0.956 | 0.904 |

## E2 Packet loss on core links (4 clients)

| Loss % | Static Mbps | Dynamic Mbps |
|---|---|---|
| 0 | 3.56 | 9.813 |
| 1 | 1.831 | 8.787 |
| 3 | 1.688 | 4.786 |

## E3 Link failure (s1-s2) during a 10 MB download

| Scenario | Success | Avg completion (s) | Avg max stall (s) | Reconnects |
|---|---|---|---|---|
| no_failure | 2/2 | 26.782 | 0.425 | 0 |
| failover_on | 2/2 | 24.587 | 0.933 | 0 |
| failover_off | 2/2 | 42.07 | 5.789 | 1 |

## E4 Security / access policy

| Check | Observed | Expected | Result |
|---|---|---|---|
| h5 (denied host) can ping server | True | True | PASS |
| h5 (denied host) reaches file service | False | False | PASS |
| h1 reaches file service | True | True | PASS |
| h1 reaches server port 22 (firewall) | False | False | PASS |
| brute force blocked by SDN after attempt # | 4 | 4 | PASS |
| blocked host h3 cannot log in even with valid password | False | False | PASS |
| other users (h1) unaffected | True | True | PASS |
| h3 works again after unblock | True | True | PASS |

Brute-force attempts:

- attempt 1 at t=0.27s: authentication failed
- attempt 2 at t=1.06s: authentication failed
- attempt 3 at t=2.09s: authentication failed
- attempt 4 at t=5.82s: TimeoutError: timed out
- attempt 5 at t=9.50s: TimeoutError: timed out
- attempt 6 at t=13.22s: TimeoutError: timed out

## E5 Server overload (3 slots, 10 simultaneous clients)

| Client retries | Succeeded | BUSY replies | Avg completion (s) |
|---|---|---|---|
| 0 | 3/10 | 7 | 7.305 |
| 8 | 9/10 | 41 | 8.788 |
