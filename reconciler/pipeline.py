"""Wire it together: load, match, explain, write."""

import csv
import json
import os
from collections import Counter, defaultdict

from . import dashboard
from . import findings as findings_mod
from . import llm
from .engine import Engine
from .loader import load_all


def reconcile(data_dir, out_dir, late_window=5, cash_window=4, reviewer=None,
              batched=False):
    data = load_all(data_dir)
    engine = Engine(data["settlements"], data["bank"], data["terms"],
                    late_window=late_window, cash_window=cash_window)
    engine.run()

    review = None
    if reviewer is not None:
        cases = llm.build_cases(engine)
        verdicts = (reviewer.review_batched(cases) if batched
                    else reviewer.review(cases))
        review = llm.apply_verdicts(engine, verdicts, cases)
        review["cases"] = len(cases)
        review["calls"] = reviewer.calls
        review["usage"] = reviewer.usage

    findings = findings_mod.collect(engine, data["settlements"], data["terms"], data["bank"])

    os.makedirs(out_dir, exist_ok=True)
    _write_predictions(out_dir, engine)
    _write_detail(out_dir, engine)
    summary = _summary(data, engine, findings)
    if review is not None:
        summary["review"] = {"cases": review["cases"], "calls": review["calls"],
                             "accepted": len(review["accepted"]),
                             "rejected": len(review["rejected"]),
                             "usage": review["usage"]}
    _write_report(out_dir, summary, findings, engine, review)
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    return {"engine": engine, "findings": findings, "summary": summary, "data": data,
            "review": review}


def _write_predictions(out_dir, engine):
    rows = [[l.settlement_id, l.bank_txn_id] for l in engine.links]
    rows += [[s.settlement_id, ""] for s in engine.declared_missing]
    with open(os.path.join(out_dir, "predictions.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["settlement_id", "bank_txn_id"])
        w.writerows(sorted(rows))


def _write_detail(out_dir, engine):
    with open(os.path.join(out_dir, "matches_detailed.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["settlement_id", "bank_txn_id", "amount", "stage", "confidence",
                    "rationale"])
        for l in sorted(engine.links, key=lambda l: (l.settlement_id, l.bank_txn_id)):
            w.writerow([l.settlement_id, l.bank_txn_id, "%.2f" % (l.amount / 100.0),
                        l.stage, "%.2f" % l.confidence, l.rationale])


def _summary(data, engine, findings):
    settlements = data["settlements"]
    matched = set(l.settlement_id for l in engine.links)
    zero = [s for s in settlements if s.net_amount == 0]
    return {
        "settlements": len(settlements),
        "bank_transactions": len(data["bank"]),
        "links_proposed": len(engine.links),
        "settlements_matched": len(matched),
        "settlements_declared_missing": len(engine.declared_missing),
        "settlements_no_deposit_expected": len(zero),
        "settlements_unresolved": len(engine.unresolved),
        "bank_lines_unexplained": len(engine.open_bank),
        "by_stage": dict(sorted(engine.stage_counts.items())),
        "findings": dict(Counter(f.kind for f in findings)),
        "exposure": {
            k: "%.2f" % (sum(abs(f.amount) for f in findings if f.kind == k) / 100.0)
            for k in sorted(set(f.kind for f in findings))
        },
    }


def _write_report(out_dir, summary, findings, engine, review=None):
    lines = ["# Reconciliation report", "",
             "%d settlements against %d bank lines." % (summary["settlements"],
                                                        summary["bank_transactions"]),
             "", "| | |", "|---|---:|"]
    for key in ("settlements_matched", "settlements_declared_missing",
                "settlements_no_deposit_expected", "settlements_unresolved",
                "links_proposed", "bank_lines_unexplained"):
        lines.append("| %s | %d |" % (key.replace("_", " "), summary[key]))

    dispositions = ("declared_missing", "unresolved", "not_yet_due")
    lines += ["", "## How each match was made", "", "| stage | links |", "|---|---:|"]
    for stage, n in summary["by_stage"].items():
        if stage not in dispositions:
            lines.append("| %s | %d |" % (stage, n))
    lines += ["", "## What was not matched", "", "| disposition | settlements |", "|---|---:|"]
    for stage in dispositions:
        lines.append("| %s | %d |" % (stage.replace("_", " "), summary["by_stage"].get(stage, 0)))

    if review is not None:
        lines += ["", "## Model review of the residual", "",
                  "%d case(s) referred, %d accepted after arithmetic checking, "
                  "%d proposal(s) rejected." % (review["cases"], len(review["accepted"]),
                                                len(review["rejected"])), ""]
        for entry in review["rejected"][:10]:
            lines.append("- **rejected** `%s`: %s" % (entry["settlement_id"],
                                                      entry["reason"]))
        if len(review["rejected"]) > 10:
            lines.append("- _...%d more_" % (len(review["rejected"]) - 10))

    lines += ["", "## Exceptions", ""]
    if not findings:
        lines.append("None.")
    grouped = defaultdict(list)
    for f in findings:
        grouped[f.kind].append(f)
    for kind, group in sorted(grouped.items(),
                              key=lambda kv: -sum(abs(f.amount) for f in kv[1])):
        total = sum(abs(f.amount) for f in group)
        lines += ["### %s - %d item(s), $%.2f at stake" % (
            kind.replace("_", " "), len(group), total / 100.0), ""]
        for f in group[:10]:
            lines.append("- **%s** %s" % (f.severity, f.summary))
        if len(group) > 10:
            lines.append("- _...%d more_" % (len(group) - 10))
        lines.append("")

    with open(os.path.join(out_dir, "report.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m reconciler",
        description="Reconcile processor settlements against a bank statement.")
    ap.add_argument("--data", default="data", help="directory with the input CSVs")
    ap.add_argument("--out", default="out", help="where to write predictions and report")
    ap.add_argument("--late-window", type=int, default=5,
                    help="days after the expected date a deposit may still land")
    ap.add_argument("--cash-window", type=int, default=4,
                    help="days a drawer may sit before it is banked")
    ap.add_argument("--llm", action="store_true",
                    help="refer the unresolved residual to Claude for review")
    ap.add_argument("--llm-model", default=llm.MODEL)
    ap.add_argument("--llm-effort", default="medium",
                    choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--llm-cache", default="llm_cache.json",
                    help="replay file; a cached case is never re-asked")
    ap.add_argument("--llm-batch", action="store_true",
                    help="use the Batches API - half price, not latency-sensitive")
    ap.add_argument("--llm-max-cases", type=int, default=None)
    ap.add_argument("--llm-offline", action="store_true",
                    help="answer only from the cache; never call the API")
    ap.add_argument("--html", nargs="?", const="dashboard.html", default=None,
                    metavar="PATH",
                    help="also write a self-contained HTML dashboard")
    ap.add_argument("--html-title", default="Reconciliation")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    reviewer = None
    if args.llm:
        reviewer = llm.ResidualReviewer(
            model=args.llm_model, cache_path=args.llm_cache, effort=args.llm_effort,
            max_cases=args.llm_max_cases, offline=args.llm_offline)

    result = reconcile(args.data, args.out, args.late_window, args.cash_window,
                       reviewer=reviewer, batched=args.llm_batch)
    if not args.quiet:
        s = result["summary"]
        print("matched %d/%d settlements, %d links, %d bank lines unexplained"
              % (s["settlements_matched"], s["settlements"], s["links_proposed"],
                 s["bank_lines_unexplained"]))
        for stage, n in s["by_stage"].items():
            print("  %-22s %5d" % (stage, n))
        if "review" in s:
            r = s["review"]
            print("  model review: %d case(s), %d call(s), %d accepted, %d rejected"
                  % (r["cases"], r["calls"], r["accepted"], r["rejected"]))
        print("wrote %s/predictions.csv, matches_detailed.csv, report.md, summary.json"
              % args.out)
    if args.html:
        path = args.html if os.path.isabs(args.html) else os.path.join(args.out, args.html)
        dashboard.write(result, path, args.html_title)
        if not args.quiet:
            print("wrote %s" % path)
    return 0
