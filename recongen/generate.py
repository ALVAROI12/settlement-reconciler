"""Top-level entry point: one seed in, a full reconciliation dataset out."""

import datetime as dt
import json
import os
import random
from collections import Counter

from . import writer
from .bank import build_bank_statement
from .config import AnomalyRates, Scenario
from .pos import generate_orders_and_payments
from .settlements import generate_settlements


def generate(scenario=None, anomalies=None, out_dir="data"):
    scenario = scenario or Scenario()
    anomalies = anomalies or AnomalyRates()
    rng = random.Random(scenario.seed)
    anomaly_log = []

    orders, payments = generate_orders_and_payments(scenario, rng)
    settlements = generate_settlements(scenario, payments, rng, anomalies, anomaly_log)
    txns, links, status = build_bank_statement(scenario, settlements, rng,
                                               anomalies, anomaly_log)

    os.makedirs(out_dir, exist_ok=True)
    writer.write_orders(out_dir, orders)
    writer.write_payments(out_dir, payments)
    writer.write_settlements(out_dir, settlements)
    writer.write_bank(out_dir, txns)
    writer.write_reference(out_dir, scenario)
    writer.write_ground_truth(out_dir, scenario, settlements, txns, links,
                              status, anomaly_log)

    manifest = {
        "seed": scenario.seed,
        "start_date": scenario.start_date.isoformat(),
        "days": scenario.days,
        "locations": [l.location_id for l in scenario.locations],
        "processors": [p.code for p in scenario.processors],
        "counts": {"orders": len(orders), "payments": len(payments),
                   "settlements": len(settlements), "bank_transactions": len(txns),
                   "links": len(links)},
        "anomaly_rates": vars(anomalies),
        "anomaly_counts": dict(Counter(a["type"] for a in anomaly_log)),
    }
    with open(os.path.join(out_dir, "run_manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    return {"orders": orders, "payments": payments, "settlements": settlements,
            "bank_transactions": txns, "links": links, "status": status,
            "anomalies": anomaly_log, "manifest": manifest}


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m recongen",
        description="Generate a synthetic POS/settlement/bank reconciliation dataset.")
    ap.add_argument("--out", default="data", help="output directory (default: data)")
    ap.add_argument("--seed", type=int, default=Scenario.seed, help="random seed")
    ap.add_argument("--start", default=Scenario.start_date.isoformat(),
                    help="first business date, YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=Scenario.days, help="days to simulate")
    ap.add_argument("--clean", action="store_true",
                    help="no injected anomalies - a dataset that reconciles perfectly")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    scenario = Scenario(seed=args.seed, days=args.days,
                        start_date=dt.date(*[int(x) for x in args.start.split("-")]))
    anomalies = AnomalyRates(**{k: 0.0 for k in vars(AnomalyRates())}) if args.clean \
        else AnomalyRates()

    result = generate(scenario, anomalies, args.out)
    if not args.quiet:
        m = result["manifest"]
        print("wrote %s/ for %s +%dd (seed %s)" % (args.out, m["start_date"],
                                                   m["days"], m["seed"]))
        for k, v in m["counts"].items():
            print("  %-18s %6d" % (k, v))
        if m["anomaly_counts"]:
            print("  anomalies:")
            for k, v in sorted(m["anomaly_counts"].items()):
                print("    %-22s %4d" % (k, v))
    return 0
