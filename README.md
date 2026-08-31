# Reconciliation agent for multi-location retail

Two halves of one problem.

**`recongen`** generates the three ledgers a multi-location restaurant or retail
operator has to tie out every month — what the POS rang up, what each processor
says it will pay, and what actually hit the bank — plus the ground truth linking
them. No public dataset covers this; settlement files and bank statements are
confidential by nature.

**`reconciler`** ties them out. It reads only what an operator actually has, matches
settlements to bank lines through a cascade of stages, and reports the exceptions —
deposits that never funded, fees billed over contract, cash that went short, credits
posted twice.

```
python3 -m recongen --out data                       # 65k orders, 180 days, 3 stores
python3 -m reconciler --data data --out out          # match and explain
python3 score.py --truth data --submission out/predictions.csv
python3 evaluate.py --seeds 7 21 55 99               # both systems, four periods
```

Zero dependencies. Python 3.9+. Same seed → byte-identical output.

## Results

Four independent six-month periods. `baseline` is exact-amount matching inside the
expected window — the obvious approach, and the floor worth beating.

```
seed  baseline f1  agent f1  precision  recall  1:1    1:many  many:1  settlement acc  bad links  missing f1
----  -----------  --------  ---------  ------  -----  ------  ------  --------------  ---------  ----------
   7        0.823     0.993      0.999   0.986  0.999   1.000   0.957           0.985          0       0.952
  21        0.816     0.985      0.998   0.973  0.999   1.000   0.917           0.972          0       0.947
  55        0.812     0.987      0.997   0.978  0.994   0.972   0.949           0.978          0       0.667
  99        0.812     0.991      1.000   0.982  0.996   1.000   0.956           0.982          0       0.667
----  -----------  --------  ---------  ------  -----  ------  ------  --------------  ---------  ----------
mean        0.816     0.989      0.998   0.980  0.997   0.993   0.945           0.979          0       0.808
```

**bad links** is the count of settlements matched to a bank line that is not a
settlement at all — payroll, rent, a vendor draft. Zero across all four periods, and
that matters more than the headline: a reconciliation that quietly books rent as a
deposit is worse than one that gives up.

**missing f1** swings between 0.667 and 0.952 because each period contains only
about ten genuinely unfunded deposits; one false claim moves it several points. The
recall inside it is 10/10 on every period — nothing unfunded goes unreported. The
variance is in precision, on a very small base.

On exceptions, measured against the injected answer key across five periods: fee
overcharges 22/22 with no false positives, duplicate credits 4/4, unfunded deposits
10/10.

## Why this is hard

The bank statement is the only authoritative document and the one written to be
useless: a truncated descriptor, a trace number appearing nowhere in the POS, and a
single credit that may cover three stores and three days.

| Break | What it looks like on the statement |
|---|---|
| **Timing** | A Friday batch funds T+2, which is Tuesday — and Tuesday after Labor Day is Wednesday. |
| **Gross vs net** | Toast withholds fees per deposit; Amex funds gross and bills the discount rate once a month as a separate debit. |
| **Combined deposits** | One ACH credit covers several stores. Amex funds T+3, so Friday, Saturday and Sunday land together: nine batches in one credit. |
| **Split deposits** | One batch arrives as two credits the same day. |
| **Marketplace payouts** | DoorDash and Uber Eats remit weekly, net of ~30% commission, with corrections against the prior week. |
| **Cash** | The manager banks one to three days of drawers at a time. The deposit names no store, no day, and is sometimes short. |
| **Chargebacks** | A debit 25–70 days after a sale that already settled and already reconciled. |
| **Refund lag** | A refund clears in a later batch than the sale it reverses. |
| **Late / missing / duplicate** | Deposits that slip, never arrive, or post twice. |
| **Fee overcharge** | A rate that drifts above contract on one batch. The match still ties — only the arithmetic betrays it. |
| **Business-date boundary** | A 12:40am ticket belongs to the previous business date, not the calendar date. |
| **Operating noise** | Payroll, rent, vendors, loans, ads, owner draws — and a `TOAST INC SOFTWARE FEE` debit whose descriptor looks exactly like a Toast deposit. |

## How the reconciler works

Stages run most-certain first. Each consumes what it can explain and hands the rest
down; nothing is matched twice, and every link records the stage that made it and
why. No business calendar is needed anywhere — the settlement states the date the
processor expects to fund, and every stage reasons about drift from that date, which
is how a real bank feed would have to be handled.

| Stage | What it resolves |
|---|---|
| `exact_on_time` | Amount, processor and date agree. Most of the book clears here. |
| `exact_late` | Same certainty, funded a few days behind. |
| `combined_deposit` | Which batches add up to this credit, solved as exact subset-sum. |
| `split_deposit` | The mirror case: which credits add up to this batch. |
| `cash_rollup` | Which store banked which days, solved per store over the whole period. |
| `debit_match` | Chargebacks and the monthly Amex discount bill. |
| `declare_missing` | What is provably absent, separated from what is merely unmatched. |

Three of those took real work:

**Cash.** Teller deposits name no store and no date, and three stores bank on the
same Monday. Matching them one deposit at a time by closest amount is how a person
gets this wrong — the nearest-looking run usually belongs to another store, and one
bad pick strands that store's drawers for the rest of the month. Each store is
instead solved over the whole period as a shortest-path problem: drawers can only be
banked front to back, so the store's history is a partition into consecutive runs.
Stores are then reconciled against each other — when two claim the same deposit, the
closer count keeps it and the other re-plans without it. This one change took
many-to-one recall from 0.28 to 0.94.

**Combined deposits.** Solved as exact subset-sum over a reachability table rather
than by trying combinations, because a nine-way Amex credit has 512 of them. If two
different subsets both hit the target, the match is ambiguous and is left for a
human — an ambiguous match is worse than none.

**Missing deposits.** "I could not match this" and "you were never paid" are
different claims, and only one starts an argument with a processor. The engine
asserts a deposit is missing only when the statement can be shown not to contain it.
Cash never qualifies, because it is banked in aggregate and a missing amount proves
nothing. Everything else short of proof is left unresolved for review.

## What gets written

Generator (`data/`):

| File | Contents |
|---|---|
| `pos_orders.csv`, `pos_payments.csv` | Order-level and tender-level sales, with refunds, chargeback dates and split tenders. |
| `processor_settlements.csv` | Batches, weekly payouts, cash drawers, chargebacks, monthly fee bills — fees broken out, expected deposit date stated. |
| `bank_transactions.csv` | The statement. Descriptor, signed amount, running balance. **No settlement ids, no categories.** |
| `reference_processors.csv` | Contract fee schedule — needed to detect overcharges. |
| `reference_merchant_ids.csv` | MID → store map, the only bridge from a descriptor to a location. |
| `ground_truth_links.csv`, `ground_truth.json` | The answer key: links, per-settlement status and labels, the full anomaly log. |

Reconciler (`out/`):

| File | Contents |
|---|---|
| `predictions.csv` | `settlement_id, bank_txn_id`. A blank id asserts the deposit never arrived. |
| `matches_detailed.csv` | Every link with the stage, confidence and rationale behind it. |
| `report.md` | The operator-facing view: what matched, what did not, and the exceptions ranked by money at stake. |
| `summary.json` | Machine-readable counts and exposure by finding type. |

## Design notes

- **Integer cents everywhere.** No float ever touches an amount, on either side.
- **The agent is blind to the answer key.** It opens only the four operator-visible
  files; a test runs it against a directory with the ground truth deleted.
- **Every match is arguable.** Stage, confidence and a plain-English reason, because
  an operator has to be able to disagree with a specific line.
- **Refusing to guess is a feature.** Ambiguous subset sums and unprovable absences
  become review items, not matches.
- **A real banking calendar** on the generator side: federal holidays computed,
  weekend holidays observed on the adjacent weekday, T+n counted in banking days.

## Tests

```
python3 -m unittest discover -s tests -v      # 33 tests
```

Generator invariants: totals tie out order → payment → settlement → link, links
conserve every net amount, the running balance is continuous, deposits land only on
banking days, the statement never leaks the answer, same seed is byte-identical.

Reconciler gates: descriptor parsing (including the software-fee trap), no bank line
spent twice, links never exceed what a credit paid, every injected fee overcharge
found, F1 above 0.95 and precision above 0.99 on freshly generated data — so a
regression in matching quality fails the build.

## Data

The structure of this dataset — the settlement shapes, the fee mechanics, the timing
rules, and the specific ways reconciliation breaks — comes from operating a real
multi-location retail and recreation business. **No real business data is included,
exposed, or derived from here.** Every record is synthetic and generated from a seed;
the fee rates and payout schedules are plausible published 2024-era US processor
terms, not any counterparty's actual contract.
