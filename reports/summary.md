# Simulation summary (synthetic data)

> Все данные синтетические и обезличенные; результат описывает поведение движка на сгенерированной популяции, а не реальный fraud rate. Не является сертифицированной AML-системой.

- seed: `7`, период: 30 дней, событий: **100,323**, игроков: **2,204** (fraud: 204)
- production: `production@2026.09.1`, shadow: `candidate@2026.10.0-rc1`
- decision latency (in-process, SQLite in-memory): p50 310 µs, p99 698 µs, ~3,088 events/s
- deterministic replay: **identical** (100,323 decisions re-derived from the event log)

## Decisions

| action | events | share |
|---|---:|---:|
| allow | 99,695 | 99.4% |
| review | 187 | 0.2% |
| step_up_kyc | 104 | 0.1% |
| block | 337 | 0.3% |

## Player-level detection (any non-allow decision)

- precision **65.7%**, recall **79.9%**, false positive rate **4.2%** (TP 163, FP 85, FN 41, TN 1915)
- synthetic loss covered by flagged players: 85 380 of 89 478 (95.4%)

| segment | flagged | total | recall |
|---|---:|---:|---:|
| bonus_abuser | 26 | 40 | 65.0% |
| card_tester | 25 | 25 | 100.0% |
| cash_cycling | 17 | 20 | 85.0% |
| chargeback_fraud | 28 | 30 | 93.3% |
| collusion | 16 | 20 | 80.0% |
| legit | 85 | 2000 | 4.2% |
| multi_account_ring | 48 | 66 | 72.7% |
| sanctioned | 3 | 3 | 100.0% |

For `legit` the column is the false-positive count (includes genuine PEP customers sent to EDD and namesakes of sanctions entries — intended behaviour, not fraud).

## Rule performance (offline, vs ground truth)

Review EV = 0.9 × loss of true-positive players − 15 × flagged players.

| rule | reason code | fired events | flagged players | precision | recall | FPR | review EV |
|---|---|---:|---:|---:|---:|---:|---:|
| AMOUNT_SPIKE | `AMOUNT_SPIKE_VS_HISTORY` | 82 | 76 | 38.2% | 14.2% | 2.4% | 49 575 |
| PRIOR_CHARGEBACK | `PRIOR_CHARGEBACK` | 13 | 13 | 100.0% | 6.4% | 0.0% | 17 904 |
| SANCTIONS_HIT | `SANCTIONS_LIST_HIT` | 6 | 3 | 100.0% | 1.5% | 0.0% | 13 455 |
| DEVICE_MANY_INSTRUMENTS | `DEVICE_MANY_PAYMENT_INSTRUMENTS` | 309 | 32 | 87.5% | 13.7% | 0.2% | 3 154 |
| CASH_CYCLING | `CASH_CYCLING_LOW_WAGERING` | 16 | 16 | 100.0% | 7.8% | 0.0% | 3 006 |
| COLLUSION_OPPOSITE_BETS | `COLLUSION_OPPOSITE_BETS` | 54 | 15 | 100.0% | 7.4% | 0.0% | 2 937 |
| CARD_TESTING | `CARD_TESTING_PATTERN` | 304 | 25 | 100.0% | 12.3% | 0.0% | 2 355 |
| DEPOSIT_VELOCITY_1H | `VELOCITY_DEPOSITS_1H` | 220 | 25 | 100.0% | 12.3% | 0.0% | 2 355 |
| KYC_BEFORE_WITHDRAWAL | `KYC_REQUIRED_FOR_WITHDRAWAL` | 15 | 15 | 73.3% | 5.4% | 0.2% | 2 098 |
| NEW_ACCOUNT_HIGH_VALUE | `NEW_ACCOUNT_HIGH_VALUE` | 21 | 21 | 52.4% | 5.4% | 0.5% | 2 098 |
| BONUS_ABUSE | `BONUS_ABUSE_LOW_WAGERING` | 89 | 89 | 68.5% | 29.9% | 1.4% | 1 573 |
| SHARED_INSTRUMENT | `SHARED_PAYMENT_INSTRUMENT` | 56 | 30 | 100.0% | 14.7% | 0.0% | 475 |
| MULTI_ACCOUNT_RING | `MULTI_ACCOUNT_RING` | 49 | 27 | 100.0% | 13.2% | 0.0% | 417 |
| __score__ | `SCORE_GE_90` | 14 | 14 | 100.0% | 6.9% | 0.0% | 208 |
| SUBNET_CLUSTER | `SUBNET_CLUSTER` | 15 | 8 | 100.0% | 3.9% | 0.0% | 124 |
| PEP_EDD | `PEP_ENHANCED_DUE_DILIGENCE` | 7 | 2 | 0.0% | 0.0% | 0.1% | -30 |

## Analyst feedback loop

248 cases worked by 3 simulated analysts with 5.0% label noise. Precision below is what the online feedback loop (`GET /rules/performance`) reports, i.e. without access to ground truth.

| rule | alerts | reviewed cases | TP | FP | feedback precision |
|---|---:|---:|---:|---:|---:|
| AMOUNT_SPIKE | 82 | 76 | 31 | 45 | 40.8% |
| BONUS_ABUSE | 89 | 89 | 60 | 29 | 67.4% |
| CARD_TESTING | 304 | 25 | 24 | 1 | 96.0% |
| CASH_CYCLING | 16 | 16 | 16 | 0 | 100.0% |
| COLLUSION_OPPOSITE_BETS | 54 | 15 | 14 | 1 | 93.3% |
| DEPOSIT_VELOCITY_1H | 220 | 25 | 24 | 1 | 96.0% |
| DEVICE_MANY_INSTRUMENTS | 309 | 32 | 27 | 5 | 84.4% |
| KYC_BEFORE_WITHDRAWAL | 15 | 15 | 11 | 4 | 73.3% |
| MULTI_ACCOUNT_RING | 49 | 27 | 25 | 2 | 92.6% |
| PEP_EDD | 7 | 2 | 0 | 2 | 0.0% |
| PRIOR_CHARGEBACK | 13 | 13 | 13 | 0 | 100.0% |
| SANCTIONS_HIT | 6 | 3 | 3 | 0 | 100.0% |
| SHARED_INSTRUMENT | 26 | 26 | 25 | 1 | 96.2% |
| SUBNET_CLUSTER | 6 | 6 | 5 | 1 | 83.3% |

## Shadow mode

- events where `candidate@2026.10.0-rc1` differs from production (action or reason codes): **133**
- action changes (production → shadow): allow→review: 75, allow→block: 21, review→block: 6, step_up_kyc→block: 2
- back-test of the shadow rule set: precision 57.6%, recall 85.3%, FPR 6.4% (production: 65.7% / 79.9% / 4.2%)

## Approval rate vs fraud loss (score threshold on deposits and withdrawals)

A money-moving event is approved when `score < threshold` (score = sum of fired rule scores, capped at 100).

"Fraud-linked amount" = amounts of deposits/withdrawals made by players labelled as fraud (a proxy for exposure, not realised loss).

| threshold | approval (all) | approval (legit players) | fraud-linked amount approved | share |
|---:|---:|---:|---:|---:|
| 10 | 91.7% | 98.4% | 127 526 | 60.2% |
| 20 | 91.8% | 98.5% | 127 526 | 60.2% |
| 25 | 92.2% | 98.7% | 134 518 | 63.5% |
| 30 | 92.6% | 98.7% | 135 684 | 64.0% |
| 35 | 94.9% | 100.0% | 170 474 | 80.4% |
| 40 | 94.9% | 100.0% | 170 545 | 80.5% |
| 50 | 95.1% | 100.0% | 180 800 | 85.3% |
| 60 | 95.1% | 100.0% | 183 730 | 86.7% |
| 70 | 95.6% | 100.0% | 194 629 | 91.8% |
| 90 | 95.8% | 100.0% | 202 654 | 95.6% |
| 101 | 100.0% | 100.0% | 211 926 | 100.0% |

Lowest fraud-linked amount with legit approval ≥ 99.0%: threshold **35** (legit approval 100.0%, overall approval 94.9%, fraud-linked amount approved 170 474 = 80.4%).

## Drift (PSI, legit deposits, first vs second half of the period)

| feature | PSI | level | mean ref | mean current |
|---|---:|---|---:|---:|
| account_age_hours | 0.724 | significant ⚠ alert | 66.24 | 186.09 |
| amount | 0.290 | significant ⚠ alert | 75.09 | 116.61 |
| deposits_24h | 0.000 | stable | 1.06 | 1.06 |
| distinct_instruments_24h | 0.000 | stable | 1.01 | 1.01 |

The `amount` shift is injected by the simulator (x1.6 from mid-period) to verify that the monitor fires. `account_age_hours` drifts structurally (the population ages as the period goes on), which is a reminder that raw age-like features need a stable reference window or should not be monitored raw.

## Device / payment graph

- connected components with ≥2 players: **62** (size distribution: 2: 54, 3: 3, 5: 2, 6: 1, 7: 2)
- components consisting only of fraud players: 12; the rest are households sharing a device (legit hard case)

## Payment routing under a PSP outage

psp_alpha returns technical errors 90% of the time between day 10 and day 12.

| strategy | transactions | approval rate | customer-visible errors | fees, bps | PSP calls |
|---|---:|---:|---:|---:|---:|
| naive | 6,062 | 84.1% | 394 | 190 | 6,062 |
| smart | 6,062 | 92.1% | 0 | 264 | 6,387 |

Circuit breaker for psp_alpha: first opened at day 10.03, 145 open cycles (half-open probe every 5 min during the outage), finally closed at day 12.00.

## Settlement reconciliation

- matched: 5,392; breaks found: **59**
- missing in settlement: 22, amount mismatch: 17, status mismatch: 13, unknown in settlement: 3, duplicates: 4
- injected by the simulator: amount_mismatch: 17, duplicates_in_settlement: 4, missing_in_settlement: 22, status_mismatch: 13, unknown_in_settlement: 3
- internal approved total 545 943 vs settled 543 304 (difference 2,639.22), PSP fees 13 068
