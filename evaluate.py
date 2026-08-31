#!/usr/bin/env python3
"""Run both systems over several seeded periods and print the comparison.

    python3 evaluate.py --seeds 7 21 55 99 --days 180

Each seed is a different six months of trading - different volumes, different
breaks, different weekends - so a number that only holds on one dataset shows up
here as variance rather than as a headline.
"""

import argparse
import datetime as dt
import json
import os
import statistics
import tempfile

import baseline
import score as scorer
from recongen.config import AnomalyRates, Scenario
from recongen.generate import generate
from reconciler.pipeline import reconcile

COLUMNS = [
    ("seed", "{seed}"),
    ("baseline f1", "{base_f1:.3f}"),
    ("agent f1", "{f1:.3f}"),
    ("precision", "{precision:.3f}"),
    ("recall", "{recall:.3f}"),
    ("1:1", "{one_to_one:.3f}"),
    ("1:many", "{one_to_many:.3f}"),
    ("many:1", "{many_to_one:.3f}"),
    ("settlement acc", "{accuracy:.3f}"),
    ("bad links", "{false_links}"),
    ("missing f1", "{missing_f1:.3f}"),
]


def evaluate_seed(seed, days, start, work_dir):
    data_dir = os.path.join(work_dir, "seed_%s" % seed)
    generate(Scenario(seed=seed, days=days, start_date=start), AnomalyRates(), data_dir)

    base_path = os.path.join(data_dir, "baseline_predictions.csv")
    baseline.run(data_dir, base_path)
    base = scorer.score(data_dir, base_path)

    out_dir = os.path.join(work_dir, "out_%s" % seed)
    reconcile(data_dir, out_dir)
    agent = scorer.score(data_dir, os.path.join(out_dir, "predictions.csv"))

    shapes = agent["by_match_shape"]
    return {
        "seed": seed,
        "base_f1": base["links"]["f1"],
        "f1": agent["links"]["f1"],
        "precision": agent["links"]["precision"],
        "recall": agent["links"]["recall"],
        "one_to_one": shapes.get("ONE_TO_ONE", {}).get("recall", 0.0),
        "one_to_many": shapes.get("ONE_TO_MANY", {}).get("recall", 0.0),
        "many_to_one": shapes.get("MANY_TO_ONE", {}).get("recall", 0.0),
        "accuracy": agent["settlements"]["accuracy"],
        "false_links": agent["false_links_to_non_settlement_lines"]["count"],
        "missing_f1": agent["missing_deposit_detection"]["f1"],
    }


def render(rows):
    headers = [c[0] for c in COLUMNS]
    body = [[fmt.format(**row) for _, fmt in COLUMNS] for row in rows]

    mean = {"seed": "mean"}
    for key in ("base_f1", "f1", "precision", "recall", "one_to_one", "one_to_many",
                "many_to_one", "accuracy", "missing_f1"):
        mean[key] = statistics.mean(r[key] for r in rows)
    mean["false_links"] = sum(r["false_links"] for r in rows)
    body.append([fmt.format(**mean) for _, fmt in COLUMNS])

    widths = [max(len(headers[i]), max(len(r[i]) for r in body)) for i in range(len(headers))]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
           "  ".join("-" * w for w in widths)]
    for i, row in enumerate(body):
        if i == len(body) - 1:
            out.append("  ".join("-" * w for w in widths))
        out.append("  ".join(cell.rjust(widths[j]) for j, cell in enumerate(row)))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, nargs="+", default=[7, 21, 55, 99])
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--start", default="2024-04-01")
    ap.add_argument("--work", default=None,
                    help="keep the generated datasets here (default: a temp dir)")
    ap.add_argument("--json", default=None, help="also write the raw results here")
    args = ap.parse_args()

    start = dt.date(*[int(x) for x in args.start.split("-")])
    work_dir = args.work or tempfile.mkdtemp(prefix="recon-eval-")
    os.makedirs(work_dir, exist_ok=True)

    rows = []
    for seed in args.seeds:
        rows.append(evaluate_seed(seed, args.days, start, work_dir))
        print("  ...seed %s done" % seed)
    print()
    print(render(rows))

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"days": args.days, "start": args.start, "results": rows}, fh, indent=2)
        print("\nwrote %s" % args.json)


if __name__ == "__main__":
    main()


