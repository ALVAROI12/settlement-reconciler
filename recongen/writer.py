"""CSV/JSON output. Amounts are written as decimal strings, dates as ISO."""

import csv
import datetime as dt
import json
import os

from .bank import merchant_id
from .util import fmt


def _w(path, header, rows):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _d(value):
    return value.isoformat() if value else ""


def write_orders(out_dir, orders):
    return _w(os.path.join(out_dir, "pos_orders.csv"),
              ["order_id", "location_id", "business_date", "closed_at", "channel",
               "guest_count", "item_count", "subtotal", "discount", "tax", "tip",
               "grand_total"],
              [[o.order_id, o.location_id, _d(o.business_date),
                o.closed_at.strftime("%Y-%m-%d %H:%M:%S"), o.channel, o.guest_count,
                o.item_count, fmt(o.subtotal), fmt(o.discount), fmt(o.tax), fmt(o.tip),
                fmt(o.grand_total)] for o in orders])


def write_payments(out_dir, payments):
    return _w(os.path.join(out_dir, "pos_payments.csv"),
              ["payment_id", "order_id", "location_id", "business_date", "processed_at",
               "method", "processor", "card_brand", "last4", "amount", "tip",
               "total_charged", "status", "refund_amount", "refund_date", "chargeback_date"],
              [[p.payment_id, p.order_id, p.location_id, _d(p.business_date),
                p.processed_at.strftime("%Y-%m-%d %H:%M:%S"), p.method, p.processor or "",
                p.card_brand or "", p.last4 or "", fmt(p.amount), fmt(p.tip),
                fmt(p.total_charged), p.status, fmt(p.refund_amount), _d(p.refund_date),
                _d(p.chargeback_date)] for p in payments])


def write_settlements(out_dir, settlements):
    return _w(os.path.join(out_dir, "processor_settlements.csv"),
              ["settlement_id", "settlement_type", "processor", "location_id",
               "period_start", "period_end", "txn_count", "gross_amount", "tip_amount",
               "refund_amount", "discount_fee", "per_txn_fee", "marketing_fee",
               "adjustment_amount", "total_fees", "net_amount", "fee_billing",
               "expected_deposit_date", "effective_rate"],
              [[s.settlement_id, s.settlement_type, s.processor, s.location_id or "",
                _d(s.period_start), _d(s.period_end), s.txn_count, fmt(s.gross_amount),
                fmt(s.tip_amount), fmt(s.refund_amount), fmt(s.discount_fee),
                fmt(s.per_txn_fee), fmt(s.marketing_fee), fmt(s.adjustment_amount),
                fmt(s.discount_fee + s.per_txn_fee + s.marketing_fee), fmt(s.net_amount),
                s.fee_billing, _d(s.expected_deposit_date), "%.6f" % s.effective_rate]
               for s in settlements])


def write_bank(out_dir, txns):
    """The statement as the bank hands it over: no category, no settlement id."""
    return _w(os.path.join(out_dir, "bank_transactions.csv"),
              ["bank_txn_id", "posted_date", "description", "amount", "balance", "direction"],
              [[t.bank_txn_id, _d(t.posted_date), t.description, fmt(t.amount),
                fmt(t.balance), "CREDIT" if t.amount >= 0 else "DEBIT"] for t in txns])


def write_reference(out_dir, scenario):
    _w(os.path.join(out_dir, "reference_processors.csv"),
       ["processor", "label", "payout", "settlement_lag_days", "contract_discount_rate",
        "contract_per_txn_fee", "fee_billing", "marketing_rate", "bank_descriptor"],
       [[p.code, p.label, p.payout, p.settlement_lag_days, "%.5f" % p.discount_rate,
         "%.2f" % p.per_txn_fee, p.fee_billing, "%.4f" % p.marketing_rate,
         p.bank_descriptor] for p in scenario.processors])

    rows = []
    for loc in scenario.locations:
        for proc in scenario.processors:
            rows.append([merchant_id(loc.location_id, proc.code), loc.location_id,
                         loc.name, proc.code, proc.bank_descriptor])
    return _w(os.path.join(out_dir, "reference_merchant_ids.csv"),
              ["merchant_id", "location_id", "location_name", "processor", "bank_descriptor"],
              rows)


def write_ground_truth(out_dir, scenario, settlements, txns, links, status, anomaly_log):
    _w(os.path.join(out_dir, "ground_truth_links.csv"),
       ["settlement_id", "bank_txn_id", "relation", "amount"],
       [[l.settlement_id, l.txn.bank_txn_id, l.relation, fmt(l.amount)]
        for l in sorted(links, key=lambda l: (l.settlement_id, l.txn.bank_txn_id))])

    linked_txn_ids = set(l.txn.bank_txn_id for l in links)
    unmatched = [{"bank_txn_id": t.bank_txn_id, "posted_date": t.posted_date.isoformat(),
                  "amount": fmt(t.amount), "category": t.category}
                 for t in txns if t.bank_txn_id not in linked_txn_ids]

    payload = {
        "generated_at": dt.datetime.now().replace(microsecond=0).isoformat(),
        "seed": scenario.seed,
        "period": {"start": scenario.start_date.isoformat(),
                   "days": scenario.days,
                   "end": (scenario.start_date
                           + dt.timedelta(days=scenario.days - 1)).isoformat()},
        "summary": {
            "settlements": len(settlements),
            "bank_transactions": len(txns),
            "links": len(links),
            "unmatched_bank_transactions": len(unmatched),
            "matched_settlements": sum(1 for v in status.values() if v["status"] == "MATCHED"),
        },
        "settlements": {
            s.settlement_id: {
                "type": s.settlement_type,
                "processor": s.processor,
                "location_id": s.location_id,
                "net_amount": fmt(s.net_amount),
                "expected_deposit_date": s.expected_deposit_date.isoformat(),
                "status": status.get(s.settlement_id, {}).get("status", "UNKNOWN"),
                "labels": sorted(set(status.get(s.settlement_id, {}).get("labels", [])
                                     + s.labels)),
            } for s in settlements
        },
        "unmatched_bank_transactions": unmatched,
        "anomalies": anomaly_log,
    }
    path = os.path.join(out_dir, "ground_truth.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    return path
