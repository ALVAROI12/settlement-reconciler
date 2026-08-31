"""What each money source says it owes you, and when it says it will pay.

Five settlement shapes, because a real operator juggles all five at once:
  BATCH              daily card batch, fees withheld (Toast) or not (Amex)
  MARKETPLACE_PAYOUT weekly remittance, ~30% commission, net of refunds
  CASH_DRAWER        counted cash owed to the bank
  CHARGEBACK         a debit that shows up weeks after the sale
  MONTHLY_FEE        the Amex discount bill, charged separately from deposits
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional

from . import bizcal
from .util import apply_rate, to_cents


@dataclass
class Settlement:
    settlement_id: str
    settlement_type: str
    processor: str
    location_id: Optional[str]
    period_start: dt.date
    period_end: dt.date
    txn_count: int
    gross_amount: int
    tip_amount: int
    refund_amount: int
    discount_fee: int
    per_txn_fee: int
    marketing_fee: int
    adjustment_amount: int
    net_amount: int
    fee_billing: str
    expected_deposit_date: dt.date
    effective_rate: float = 0.0
    labels: List[str] = field(default_factory=list)


def _period_bounds(d):
    """Marketplace week: the Mon-Sun block that a payout covers."""
    start = d - dt.timedelta(days=d.weekday())
    return start, start + dt.timedelta(days=6)


def generate_settlements(scenario, payments, rng, anomalies_cfg, anomaly_log):
    procs = {p.code: p for p in scenario.processors}
    settlements: List[Settlement] = []

    daily = defaultdict(lambda: {"count": 0, "gross": 0, "tip": 0})
    refunds = defaultdict(int)
    weekly = defaultdict(lambda: {"count": 0, "gross": 0, "tip": 0, "refunds": 0})
    cash = defaultdict(lambda: {"count": 0, "gross": 0})

    for p in payments:
        if p.method == "CASH":
            c = cash[(p.location_id, p.business_date)]
            c["count"] += 1
            c["gross"] += p.total_charged
            continue
        proc = procs[p.processor]
        if proc.payout == "WEEKLY":
            start, end = _period_bounds(p.business_date)
            w = weekly[(p.processor, p.location_id, start, end)]
            w["count"] += 1
            w["gross"] += p.total_charged
            w["tip"] += p.tip
            if p.refund_amount:
                rstart, rend = _period_bounds(p.refund_date)
                weekly[(p.processor, p.location_id, rstart, rend)]["refunds"] += p.refund_amount
        else:
            k = (p.processor, p.location_id, p.business_date)
            daily[k]["count"] += 1
            daily[k]["gross"] += p.total_charged
            daily[k]["tip"] += p.tip
            if p.refund_amount:
                refunds[(p.processor, p.location_id, p.refund_date)] += p.refund_amount

    settlements += _daily_batches(scenario, procs, daily, refunds, rng,
                                  anomalies_cfg, anomaly_log)
    settlements += _weekly_payouts(procs, weekly, rng)
    settlements += _cash_drawers(cash)
    settlements += _chargebacks(payments, procs)
    settlements += _monthly_fees(scenario, procs, settlements)

    settlements.sort(key=lambda s: (s.expected_deposit_date, s.settlement_id))
    return settlements


def _finalize(s):
    total_fees = s.discount_fee + s.per_txn_fee + s.marketing_fee
    s.net_amount = s.gross_amount - s.refund_amount + s.adjustment_amount
    if s.fee_billing == "DAILY_NET":
        s.net_amount -= total_fees
    s.effective_rate = round(total_fees / s.gross_amount, 6) if s.gross_amount else 0.0
    return s


def _daily_batches(scenario, procs, daily, refunds, rng, cfg, log):
    out = []
    for key in sorted(set(list(daily.keys()) + list(refunds.keys()))):
        code, loc, d = key
        proc = procs[code]
        agg = daily.get(key, {"count": 0, "gross": 0, "tip": 0})
        refund = refunds.get(key, 0)
        sid = "%s-%s-%s" % (code, loc, d.isoformat())

        rate, labels = proc.discount_rate, []
        if agg["gross"] and rng.random() < cfg.fee_overcharge:
            # A rate spike that never breaks the match - only the math betrays it.
            rate = round(proc.discount_rate + rng.uniform(0.004, 0.011), 5)
            labels.append("fee_overcharge")
            log.append({"type": "fee_overcharge", "settlement_id": sid,
                        "expected_rate": proc.discount_rate, "billed_rate": rate})

        s = Settlement(
            settlement_id=sid, settlement_type="BATCH", processor=code, location_id=loc,
            period_start=d, period_end=d, txn_count=agg["count"], gross_amount=agg["gross"],
            tip_amount=agg["tip"], refund_amount=refund,
            discount_fee=apply_rate(agg["gross"], rate),
            per_txn_fee=to_cents(round(agg["count"] * proc.per_txn_fee, 2)),
            marketing_fee=0, adjustment_amount=0, net_amount=0,
            fee_billing=proc.fee_billing,
            expected_deposit_date=bizcal.add_business_days(d, proc.settlement_lag_days),
            labels=labels,
        )
        out.append(_finalize(s))
    return out


def _weekly_payouts(procs, weekly, rng):
    out = []
    for (code, loc, start, end), agg in sorted(weekly.items()):
        proc = procs[code]
        payout_day = end + dt.timedelta(days=1 + proc.payout_weekday)
        # Marketplaces post small correction lines against the prior week.
        adjustment = -to_cents(round(rng.uniform(0, 42), 2)) if rng.random() < 0.35 else 0
        s = Settlement(
            settlement_id="%s-%s-W%s" % (code, loc, start.isoformat()),
            settlement_type="MARKETPLACE_PAYOUT", processor=code, location_id=loc,
            period_start=start, period_end=end, txn_count=agg["count"],
            gross_amount=agg["gross"], tip_amount=agg["tip"], refund_amount=agg["refunds"],
            discount_fee=apply_rate(agg["gross"], proc.discount_rate),
            per_txn_fee=0, marketing_fee=apply_rate(agg["gross"], proc.marketing_rate),
            adjustment_amount=adjustment, net_amount=0, fee_billing=proc.fee_billing,
            expected_deposit_date=bizcal.add_business_days(payout_day, proc.settlement_lag_days),
        )
        out.append(_finalize(s))
    return out


def _cash_drawers(cash):
    out = []
    for (loc, d), agg in sorted(cash.items()):
        s = Settlement(
            settlement_id="CASH-%s-%s" % (loc, d.isoformat()), settlement_type="CASH_DRAWER",
            processor="CASH", location_id=loc, period_start=d, period_end=d,
            txn_count=agg["count"], gross_amount=agg["gross"], tip_amount=0, refund_amount=0,
            discount_fee=0, per_txn_fee=0, marketing_fee=0, adjustment_amount=0, net_amount=0,
            fee_billing="NONE", expected_deposit_date=bizcal.add_business_days(d, 1),
        )
        out.append(_finalize(s))
    return out


def _chargebacks(payments, procs):
    out = []
    for p in payments:
        if not p.chargeback_date:
            continue
        proc = procs[p.processor]
        d = bizcal.next_business_day(p.chargeback_date)
        s = Settlement(
            settlement_id="CB-%s" % p.payment_id, settlement_type="CHARGEBACK",
            processor=p.processor, location_id=p.location_id, period_start=p.business_date,
            period_end=p.chargeback_date, txn_count=1, gross_amount=0, tip_amount=0,
            refund_amount=p.total_charged, discount_fee=0, per_txn_fee=to_cents(25.00),
            marketing_fee=0, adjustment_amount=0, net_amount=0, fee_billing="DAILY_NET",
            expected_deposit_date=d, labels=["chargeback"],
        )
        out.append(_finalize(s))
    return out


def _monthly_fees(scenario, procs, settlements):
    """Amex funds gross and bills the discount rate once a month - the deposits
    reconcile only if you also find this debit."""
    out = []
    by_month = defaultdict(lambda: {"fees": 0, "gross": 0, "count": 0})
    for s in settlements:
        if s.fee_billing != "MONTHLY" or s.settlement_type != "BATCH":
            continue
        key = (s.processor, s.period_end.year, s.period_end.month)
        m = by_month[key]
        m["fees"] += s.discount_fee + s.per_txn_fee
        m["gross"] += s.gross_amount
        m["count"] += s.txn_count

    for (code, year, month), m in sorted(by_month.items()):
        if not m["fees"]:
            continue
        nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
        due = bizcal.add_business_days(nxt - dt.timedelta(days=1), 3)
        if due > scenario.start_date + dt.timedelta(days=scenario.days - 1):
            continue
        s = Settlement(
            settlement_id="%sFEE-%04d-%02d" % (code, year, month), settlement_type="MONTHLY_FEE",
            processor=code, location_id=None, period_start=dt.date(year, month, 1),
            period_end=nxt - dt.timedelta(days=1), txn_count=m["count"], gross_amount=0,
            tip_amount=0, refund_amount=m["fees"], discount_fee=0, per_txn_fee=0,
            marketing_fee=0, adjustment_amount=0, net_amount=0, fee_billing="MONTHLY",
            expected_deposit_date=due, labels=["monthly_fee_debit"],
        )
        out.append(_finalize(s))
    return out
