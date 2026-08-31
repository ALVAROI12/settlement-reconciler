#!/usr/bin/env python3
"""Score a reconciliation against ground truth.

    python3 score.py --truth data --submission predictions.csv

Submission CSV: settlement_id,bank_txn_id  - one row per proposed link.
Leave bank_txn_id blank to assert "this settlement was never deposited".

Reported: link precision/recall/F1, recall split by match shape, settlement-level
exact-set accuracy, missing-deposit detection, and false links onto bank lines
that are not settlements at all (the expensive mistake).
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict


def load_truth(truth_dir):
    pairs, by_settlement, relation = set(), defaultdict(set), {}
    with open(os.path.join(truth_dir, "ground_truth_links.csv")) as fh:
        for row in csv.DictReader(fh):
            sid, bid = row["settlement_id"], row["bank_txn_id"]
            pairs.add((sid, bid))
            by_settlement[sid].add(bid)
            relation[sid] = row["relation"]
    with open(os.path.join(truth_dir, "ground_truth.json")) as fh:
        meta = json.load(fh)
    return pairs, by_settlement, relation, meta


def load_submission(path):
    pairs, declared_missing = set(), set()
    with open(path) as fh:
        reader = csv.DictReader(fh)
        required = {"settlement_id", "bank_txn_id"}
        if not required.issubset(set(reader.fieldnames or [])):
            sys.exit("submission must have columns: settlement_id,bank_txn_id")
        for row in reader:
            sid = (row["settlement_id"] or "").strip()
            bid = (row["bank_txn_id"] or "").strip()
            if not sid:
                continue
            if bid:
                pairs.add((sid, bid))
            else:
                declared_missing.add(sid)
    return pairs, declared_missing


def prf(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def score(truth_dir, submission_path):
    truth, by_settlement, relation, meta = load_truth(truth_dir)
    pred, declared_missing = load_submission(submission_path)

    tp = len(truth & pred)
    precision, recall, f1 = prf(tp, len(pred - truth), len(truth - pred))

    per_relation = {}
    for rel in ("ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE"):
        rel_pairs = set(p for p in truth if relation.get(p[0]) == rel)
        if rel_pairs:
            per_relation[rel] = {
                "links": len(rel_pairs),
                "recall": round(len(rel_pairs & pred) / len(rel_pairs), 4),
            }

    pred_by_settlement = defaultdict(set)
    for sid, bid in pred:
        pred_by_settlement[sid].add(bid)
    exact = sum(1 for sid, bids in by_settlement.items()
                if pred_by_settlement.get(sid) == bids)

    non_settlement = set(t["bank_txn_id"] for t in meta["unmatched_bank_transactions"])
    false_on_noise = [p for p in (pred - truth) if p[1] in non_settlement]
    noise_categories = Counter(
        next(t["category"] for t in meta["unmatched_bank_transactions"]
             if t["bank_txn_id"] == bid) for _, bid in false_on_noise)

    truth_missing = set(sid for sid, s in meta["settlements"].items()
                        if s["status"] == "MISSING_DEPOSIT")
    m_tp = len(truth_missing & declared_missing)
    m_p, m_r, m_f1 = prf(m_tp, len(declared_missing - truth_missing),
                         len(truth_missing - declared_missing))

    return {
        "links": {"truth": len(truth), "predicted": len(pred), "correct": tp,
                  "precision": round(precision, 4), "recall": round(recall, 4),
                  "f1": round(f1, 4)},
        "by_match_shape": per_relation,
        "settlements": {"total": len(by_settlement), "exact_set_match": exact,
                        "accuracy": round(exact / len(by_settlement), 4) if by_settlement else 0.0},
        "false_links_to_non_settlement_lines": {
            "count": len(false_on_noise), "by_category": dict(noise_categories)},
        "missing_deposit_detection": {
            "truth": len(truth_missing), "declared": len(declared_missing),
            "correct": m_tp, "precision": round(m_p, 4), "recall": round(m_r, 4),
            "f1": round(m_f1, 4)},
    }


def render(report):
    L = report["links"]
    out = ["LINK MATCHING",
           "  truth %d   predicted %d   correct %d" % (L["truth"], L["predicted"], L["correct"]),
           "  precision %.3f   recall %.3f   f1 %.3f" % (L["precision"], L["recall"], L["f1"]),
           "", "RECALL BY MATCH SHAPE"]
    for rel, v in report["by_match_shape"].items():
        out.append("  %-12s %5d links   recall %.3f" % (rel, v["links"], v["recall"]))
    S = report["settlements"]
    out += ["", "SETTLEMENT-LEVEL",
            "  exact link-set match %d/%d (%.3f)" % (S["exact_set_match"], S["total"],
                                                     S["accuracy"]),
            "", "FALSE LINKS TO NON-SETTLEMENT BANK LINES"]
    F = report["false_links_to_non_settlement_lines"]
    out.append("  %d%s" % (F["count"], (" -> " + ", ".join(
        "%s x%d" % (k, v) for k, v in sorted(F["by_category"].items()))) if F["by_category"] else ""))
    M = report["missing_deposit_detection"]
    out += ["", "MISSING-DEPOSIT DETECTION",
            "  truth %d   declared %d   correct %d   f1 %.3f" % (
                M["truth"], M["declared"], M["correct"], M["f1"])]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--truth", default="data", help="directory holding ground truth")
    ap.add_argument("--submission", required=True, help="predicted links CSV")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    report = score(args.truth, args.submission)
    print(json.dumps(report, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
