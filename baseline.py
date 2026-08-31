#!/usr/bin/env python3
"""A deliberately naive matcher: exact amount, inside the expected deposit window,
first unused bank line wins. It is the floor an agent has to beat, and it fails
in exactly the places the dataset is designed to be hard -- combined deposits,
split deposits, cash, and anything that lands late.

    python3 baseline.py --data data --out baseline_predictions.csv
"""

import argparse
import csv
import datetime as dt
import os
from collections import defaultdict


def _date(s):
    return dt.date(*[int(x) for x in s.split("-")])


def _cents(s):
    return int(round(float(s) * 100))


def run(data_dir, out_path, window=4):
    with open(os.path.join(data_dir, "processor_settlements.csv")) as fh:
        settlements = list(csv.DictReader(fh))
    with open(os.path.join(data_dir, "bank_transactions.csv")) as fh:
        bank = list(csv.DictReader(fh))

    by_amount = defaultdict(list)
    for t in bank:
        by_amount[_cents(t["amount"])].append(t)

    used = set()
    rows = []
    for s in sorted(settlements, key=lambda s: s["expected_deposit_date"]):
        net = _cents(s["net_amount"])
        if net == 0:
            continue
        expected = _date(s["expected_deposit_date"])
        hit = None
        for t in by_amount.get(net, []):
            if t["bank_txn_id"] in used:
                continue
            delta = (_date(t["posted_date"]) - expected).days
            if 0 <= delta <= window:
                hit = t
                break
        if hit:
            used.add(hit["bank_txn_id"])
            rows.append([s["settlement_id"], hit["bank_txn_id"]])
        else:
            rows.append([s["settlement_id"], ""])  # asserted: never deposited

    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["settlement_id", "bank_txn_id"])
        w.writerows(rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="baseline_predictions.csv")
    ap.add_argument("--window", type=int, default=4,
                    help="calendar days after the expected date to search")
    args = ap.parse_args()
    rows = run(args.data, args.out, args.window)
    print("wrote %s (%d rows, %d linked)" % (args.out, len(rows),
                                             sum(1 for r in rows if r[1])))


if __name__ == "__main__":
    main()
