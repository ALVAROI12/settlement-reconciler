"""Records the reconciler works with. It reads only what an operator actually
has: settlements, the bank statement, and the contract reference data. Ground
truth is never opened.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional


def parse_date(s):
    return dt.date(*[int(x) for x in s.split("-")]) if s else None


def parse_cents(s):
    """'-1234.56' -> -123456. Text in, integers out; no float arithmetic after."""
    s = (s or "0").strip()
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    whole, _, frac = s.partition(".")
    return (-1 if neg else 1) * (int(whole or 0) * 100 + int((frac + "00")[:2]))


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
    refund_amount: int
    discount_fee: int
    per_txn_fee: int
    marketing_fee: int
    total_fees: int
    net_amount: int
    fee_billing: str
    expected_deposit_date: dt.date
    effective_rate: float


@dataclass
class BankTxn:
    bank_txn_id: str
    posted_date: dt.date
    description: str
    amount: int
    balance: int
    # Filled in by the descriptor parser.
    processor: Optional[str] = None
    merchant_id: Optional[str] = None
    location_id: Optional[str] = None
    kind: str = "UNKNOWN"          # DEPOSIT|PAYOUT|CHARGEBACK|FEE|CASH_DEPOSIT|OTHER


@dataclass
class ProcessorTerms:
    processor: str
    label: str
    payout: str
    settlement_lag_days: int
    contract_discount_rate: float
    contract_per_txn_fee: float
    fee_billing: str
    marketing_rate: float
    bank_descriptor: str


@dataclass
class MatchLink:
    settlement_id: str
    bank_txn_id: str
    amount: int
    stage: str
    confidence: float
    rationale: str = ""


@dataclass
class Finding:
    kind: str
    severity: str                  # HIGH | MEDIUM | LOW
    summary: str
    amount: int = 0
    settlement_ids: List[str] = field(default_factory=list)
    bank_txn_ids: List[str] = field(default_factory=list)
