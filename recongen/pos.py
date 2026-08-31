"""The POS side of the truth: orders as the restaurant recorded them, and the
payments that tender them. Everything downstream is derived from payments.
"""

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional

from . import bizcal
from .util import apply_rate, jitter, lognormal_cents, to_cents

# Recreation/retail demand: summer is the season, winter is not.
SEASONALITY = {1: 0.78, 2: 0.82, 3: 0.95, 4: 1.00, 5: 1.08, 6: 1.22,
               7: 1.30, 8: 1.24, 9: 1.02, 10: 0.95, 11: 0.88, 12: 0.92}

CARD_BRANDS = [("VISA", 0.52), ("MASTERCARD", 0.33), ("DISCOVER", 0.15)]


@dataclass
class Order:
    order_id: str
    location_id: str
    business_date: dt.date
    closed_at: dt.datetime
    channel: str
    guest_count: int
    item_count: int
    subtotal: int
    discount: int
    tax: int
    tip: int
    grand_total: int


@dataclass
class Payment:
    payment_id: str
    order_id: str
    location_id: str
    business_date: dt.date
    processed_at: dt.datetime
    method: str            # CARD | CASH | MARKETPLACE
    processor: Optional[str]
    card_brand: Optional[str]
    last4: Optional[str]
    amount: int            # sale portion (net of tip)
    tip: int
    total_charged: int
    status: str            # CAPTURED | REFUNDED | PARTIAL_REFUND
    refund_amount: int = 0
    refund_date: Optional[dt.date] = None
    chargeback_date: Optional[dt.date] = None


def _weighted(rng, pairs):
    r = rng.random()
    acc = 0.0
    for value, w in pairs:
        acc += w
        if r < acc:
            return value
    return pairs[-1][0]


def _is_closed(d):
    name = bizcal.holiday_name(d)
    return name in ("Thanksgiving", "Christmas Day")


def _day_multiplier(rng, loc, d):
    mult = loc.dow_multipliers[d.weekday()] * SEASONALITY[d.month]
    name = bizcal.holiday_name(d)
    if name in ("Independence Day", "Memorial Day", "Labor Day", "Juneteenth"):
        mult *= 1.35
    return jitter(rng, mult, 0.14)


def _closed_at(rng, business_date):
    """Service runs into the small hours; a 12:40am order still belongs to the
    business date that opened it. This is a classic source of off-by-one-day
    breaks between POS reports and processor batches."""
    if rng.random() < 0.045:
        hour = rng.choice([0, 0, 1])
        return dt.datetime.combine(business_date + dt.timedelta(days=1),
                                   dt.time(hour, rng.randrange(60), rng.randrange(60)))
    hour = min(23, max(10, int(rng.triangular(10, 24, 19))))
    return dt.datetime.combine(business_date, dt.time(hour, rng.randrange(60), rng.randrange(60)))


def _tip_cents(rng, base_cents, scenario, channel):
    if channel == "CASH":
        # Cash tips mostly stay off the ticket.
        return to_cents(round(base_cents / 100.0 * rng.uniform(0.0, 0.06), 2)) if rng.random() < 0.15 else 0
    rate = max(0.0, rng.gauss(scenario.tip_rate_mean, 0.07))
    if rng.random() < 0.08:
        rate = 0.0
    return apply_rate(base_cents, round(rate, 4))


def generate_orders_and_payments(scenario, rng):
    orders: List[Order] = []
    payments: List[Payment] = []
    mix = sorted(scenario.channel_mix.items(), key=lambda kv: -kv[1])
    order_seq = 0
    pay_seq = 0
    end_date = scenario.start_date + dt.timedelta(days=scenario.days - 1)

    for d in bizcal.date_range(scenario.start_date, scenario.days):
        if _is_closed(d):
            continue
        for loc in scenario.locations:
            if d.weekday() not in loc.open_days:
                continue
            count = max(0, int(round(loc.base_daily_orders * _day_multiplier(rng, loc, d))))
            for _ in range(count):
                order_seq += 1
                channel = _weighted(rng, mix)
                subtotal = lognormal_cents(rng, loc.median_ticket, 0.52)
                discount = apply_rate(subtotal, round(rng.uniform(0.05, 0.25), 4)) \
                    if rng.random() < scenario.discount_share else 0
                taxable = subtotal - discount
                tax = apply_rate(taxable, scenario.sales_tax_rate)
                tip = _tip_cents(rng, taxable, scenario, channel)
                grand = taxable + tax + tip
                closed = _closed_at(rng, d)
                order = Order(
                    order_id="O-%s-%06d" % (loc.location_id, order_seq),
                    location_id=loc.location_id, business_date=d, closed_at=closed,
                    channel=channel, guest_count=rng.randint(1, 6),
                    item_count=max(1, int(rng.triangular(1, 12, 3))),
                    subtotal=subtotal, discount=discount, tax=tax, tip=tip,
                    grand_total=grand,
                )
                orders.append(order)

                for part_amount, part_tip in _tender_split(rng, scenario, channel,
                                                           taxable + tax, tip):
                    pay_seq += 1
                    payments.append(_make_payment(
                        rng, scenario, order, channel, pay_seq, part_amount, part_tip, end_date))
    return orders, payments


def _tender_split(rng, scenario, channel, sale_cents, tip_cents):
    """Most orders are one tender; a few cards get split down the middle."""
    if channel in ("CASH", "DOORDASH", "UBEREATS") or rng.random() >= scenario.split_tender_share:
        return [(sale_cents, tip_cents)]
    first_sale = sale_cents // 2
    first_tip = tip_cents // 2
    return [(first_sale, first_tip), (sale_cents - first_sale, tip_cents - first_tip)]


def _make_payment(rng, scenario, order, channel, seq, amount, tip, end_date):
    if channel == "CASH":
        method, processor, brand, last4 = "CASH", None, None, None
    elif channel in ("DOORDASH", "UBEREATS"):
        method, processor, brand, last4 = "MARKETPLACE", channel, None, None
    elif channel == "AMEX":
        method, processor, brand = "CARD", "AMEX", "AMEX"
        last4 = "%04d" % rng.randrange(10000)
    else:
        method, processor, brand = "CARD", "TOAST", _weighted(rng, CARD_BRANDS)
        last4 = "%04d" % rng.randrange(10000)

    pay = Payment(
        payment_id="P-%08d" % seq, order_id=order.order_id,
        location_id=order.location_id, business_date=order.business_date,
        processed_at=order.closed_at, method=method, processor=processor,
        card_brand=brand, last4=last4, amount=amount, tip=tip,
        total_charged=amount + tip, status="CAPTURED",
    )

    if processor is not None and rng.random() < scenario.refund_share:
        full = rng.random() < 0.6
        pay.refund_amount = pay.total_charged if full else apply_rate(
            pay.total_charged, round(rng.uniform(0.2, 0.8), 4))
        pay.status = "REFUNDED" if full else "PARTIAL_REFUND"
        # Refunds routinely land in a later batch than the sale.
        rdate = pay.business_date + dt.timedelta(days=int(rng.triangular(0, 9, 1)))
        pay.refund_date = min(rdate, end_date)

    if (method == "CARD" and pay.status == "CAPTURED"
            and rng.random() < scenario.chargeback_share):
        cdate = pay.business_date + dt.timedelta(days=rng.randint(25, 70))
        if cdate <= end_date:
            pay.chargeback_date = cdate
    return pay
