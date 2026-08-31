# recongen — synthetic POS ↔ settlement ↔ bank reconciliation data

A seeded generator that produces the three ledgers a multi-location restaurant or
retail operator has to tie out every month — **what the POS rang up**, **what each
processor says it will pay**, and **what actually hit the bank** — plus the ground
truth linking them.

No public dataset covers this. Settlement files and bank statements are confidential
by nature, so the honest way to build and evaluate a reconciliation agent is to
simulate the domain faithfully and keep the answer key.

```
python3 -m recongen --out data            # ~65k orders, 180 days, 3 locations
python3 baseline.py --data data --out baseline_predictions.csv
python3 score.py --truth data --submission baseline_predictions.csv
```

Zero dependencies. Python 3.9+. Same seed → byte-identical output.

## Why this is hard

The bank statement is the only document that is authoritative, and it is the one
written to be useless: a truncated merchant descriptor, a trace number that appears
nowhere in the POS, and a single credit that may cover three locations. The generator
injects, and labels, every break a real operator meets:

| Break | What it looks like on the statement |
|---|---|
| **Timing** | A Friday batch funds T+2, which is Tuesday — and Tuesday after Labor Day is Wednesday. |
| **Gross vs net** | Toast withholds fees per deposit; Amex funds gross and bills the discount rate once a month as a separate debit. |
| **Combined deposits** | One ACH credit covers three locations' batches. Many settlements → one line. |
| **Split deposits** | One batch arrives as two credits on the same day. One settlement → many lines. |
| **Marketplace payouts** | DoorDash and Uber Eats remit weekly, net of ~30% commission, with correction lines against the prior week. |
| **Cash** | The manager banks one to three days of drawers at a time, and the envelope is sometimes short. |
| **Chargebacks** | A debit 25–70 days after a sale that already settled and already reconciled. |
| **Refund lag** | A refund clears in a later batch than the sale it reverses. |
| **Late / missing / duplicate** | Deposits that slip, never arrive, or post twice. |
| **Fee overcharge** | A rate that quietly drifts above contract on one batch. The match still ties — only the arithmetic betrays it. |
| **Business-date boundary** | A 12:40am ticket belongs to the previous business date, not the calendar date. |
| **Operating noise** | Payroll, rent, vendors, loan, insurance, ads, owner draws — and a `TOAST INC SOFTWARE FEE` debit whose descriptor looks exactly like a Toast deposit. |

## What gets written

| File | Contents |
|---|---|
| `pos_orders.csv` | Order-level sales: subtotal, discount, tax, tip, channel, close time. |
| `pos_payments.csv` | Tender-level: card brand, last 4, processor, refunds, chargeback dates. Split tenders included. |
| `processor_settlements.csv` | Batches, weekly payouts, cash drawers, chargebacks, monthly fee bills — with fees broken out and an expected deposit date. |
| `bank_transactions.csv` | The statement. Descriptor, signed amount, running balance. **No settlement ids, no categories.** |
| `reference_processors.csv` | Contract fee schedule — needed to detect overcharges. |
| `reference_merchant_ids.csv` | MID → location map, the only bridge from a descriptor to a store. |
| `ground_truth_links.csv` | `settlement_id, bank_txn_id, relation, amount`. |
| `ground_truth.json` | Per-settlement status and labels, every non-settlement bank line, the full anomaly log. |
| `run_manifest.json` | Seed, window, counts, injected anomaly rates. |

## Ground truth and scoring

`score.py` takes a submission of `settlement_id, bank_txn_id` rows (blank
`bank_txn_id` asserts "never deposited") and reports link precision/recall/F1,
recall split by match shape, settlement-level exact-set accuracy, missing-deposit
detection, and false links onto bank lines that are not settlements at all — the
error that costs an operator real money.

`baseline.py` is the floor: exact amount, inside the expected window, first unused
line wins.

```
LINK MATCHING
  truth 1806   predicted 1262   correct 1262
  precision 1.000   recall 0.699   f1 0.823

RECALL BY MATCH SHAPE
  ONE_TO_ONE    1157 links   recall 0.993
  ONE_TO_MANY     70 links   recall 0.000
  MANY_TO_ONE    579 links   recall 0.195

SETTLEMENT-LEVEL
  exact link-set match 1262/1771 (0.713)

MISSING-DEPOSIT DETECTION
  truth 10   declared 546   correct 10   f1 0.036
```

That profile is the point. Exact-amount matching solves the easy 70% and then falls
off a cliff: it cannot split, cannot aggregate, and it calls 546 settlements missing
when 10 are. Everything above 0.823 has to come from actually reasoning about the
domain.

## Design notes

- **Integer cents everywhere.** No float ever touches an amount; rates round half-up
  the way processors round.
- **A real banking calendar.** Federal holidays are computed, weekend holidays are
  observed on the adjacent weekday, and T+n counts banking days.
- **The answer key never leaks.** `bank_transactions.csv` carries no category and no
  settlement id; a test asserts it.
- **Determinism is tested,** not assumed — same seed, byte-identical CSVs.

## Configuring

Everything lives in [config.py](recongen/config.py): locations and their weekday
demand curves, processor fee schedules and payout rules, tip and refund rates, and
the injected break rates in `AnomalyRates`.

```
python3 -m recongen --start 2025-01-01 --days 90 --seed 42 --out data/q1
python3 -m recongen --clean --out data/clean     # a period that ties out perfectly
```

## Tests

```
python3 -m unittest discover -s tests -v
```

18 invariants: totals add up order → payment → settlement → link, links conserve
every net amount, the running balance is continuous, deposits land only on banking
days, missing deposits have no links, and the statement never leaks the answer.

## Data

The structure of this dataset — the settlement shapes, the fee mechanics, the
timing rules, and the specific ways reconciliation breaks — comes from operating a
real multi-location retail and recreation business. **No real business data is
included, exposed, or derived from here.** Every record is synthetic and generated
from a seed; the fee rates and payout schedules are plausible published 2024-era US
processor terms, not any counterparty's actual contract.
