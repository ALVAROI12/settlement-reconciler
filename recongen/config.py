"""Scenario knobs. Every rate here is a plausible 2024-era US restaurant/retail
number; change them in one place and the whole dataset moves with you.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class Location:
    location_id: str
    name: str
    base_daily_orders: int
    median_ticket: float
    # Mon..Sun demand multipliers - weekends carry a recreation/retail business.
    dow_multipliers: Tuple[float, ...] = (0.72, 0.75, 0.86, 1.02, 1.38, 1.62, 1.25)
    open_days: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


@dataclass
class Processor:
    """How one money source pays out.

    fee_billing:
      DAILY_NET  - fees withheld from each deposit (Toast/Square style)
      MONTHLY    - deposits arrive gross, fees debited once a month (Amex style)
    payout:
      DAILY      - one batch per business date
      WEEKLY     - one remittance covering a Mon-Sun period (marketplaces)
    """
    code: str
    label: str
    payout: str
    settlement_lag_days: int
    discount_rate: float          # % of gross
    per_txn_fee: float            # $ per transaction
    fee_billing: str = "DAILY_NET"
    payout_weekday: int = 0       # WEEKLY only: 0=Mon
    marketing_rate: float = 0.0   # marketplaces only
    bank_descriptor: str = ""


@dataclass
class Scenario:
    start_date: dt.date = dt.date(2024, 4, 1)
    days: int = 180
    seed: int = 7
    opening_balance: float = 165_000.00
    sales_tax_rate: float = 0.0825          # Texas
    tip_rate_mean: float = 0.185
    discount_share: float = 0.07            # share of orders with a comp/discount
    refund_share: float = 0.014             # share of card payments refunded
    chargeback_share: float = 0.0012        # share of card payments disputed
    split_tender_share: float = 0.03        # orders paid with two cards

    # Payment mix on a normal order.
    channel_mix: Dict[str, float] = field(default_factory=lambda: {
        "TOAST_CARD": 0.60,
        "CASH": 0.11,
        "AMEX": 0.09,
        "DOORDASH": 0.11,
        "UBEREATS": 0.09,
    })

    locations: List[Location] = field(default_factory=lambda: [
        Location("L001", "Riverside Main", 138, 34.50),
        Location("L002", "Northgate", 96, 29.75),
        Location("L003", "Lakeway Kiosk", 54, 18.20,
                 dow_multipliers=(0.40, 0.45, 0.55, 0.80, 1.55, 2.10, 1.70)),
    ])

    processors: List[Processor] = field(default_factory=lambda: [
        Processor("TOAST", "Toast (Visa/MC/Disc)", "DAILY", 2, 0.0265, 0.15,
                  bank_descriptor="TST*TOAST INC"),
        Processor("AMEX", "American Express", "DAILY", 3, 0.0290, 0.10,
                  fee_billing="MONTHLY", bank_descriptor="AMEX EPAYMENT"),
        Processor("DOORDASH", "DoorDash Marketplace", "WEEKLY", 2, 0.0300, 0.00,
                  payout_weekday=0, marketing_rate=0.2700,
                  bank_descriptor="DOORDASH INC"),
        Processor("UBEREATS", "Uber Eats Marketplace", "WEEKLY", 2, 0.0300, 0.00,
                  payout_weekday=2, marketing_rate=0.2900,
                  bank_descriptor="UBER USA 6787"),
    ])


@dataclass
class AnomalyRates:
    """Injected break rates. Each one is labelled in the ground truth, so a
    reconciliation agent can be scored on finding it - not just on matching."""
    late_deposit: float = 0.040       # deposit lands 1-3 business days late
    combined_deposit: float = 0.070   # processor rolls locations into one ACH
    split_deposit: float = 0.030      # one batch paid as two ACH lines
    missing_deposit: float = 0.008    # batch never funded
    duplicate_deposit: float = 0.005  # bank posts the same credit twice
    fee_overcharge: float = 0.012     # discount rate spikes on one batch
    short_cash_deposit: float = 0.110 # counted cash != deposited cash


DEFAULT_SCENARIO = Scenario()
DEFAULT_ANOMALIES = AnomalyRates()
