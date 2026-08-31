"""Read the operator-visible files and decode bank descriptors."""

import csv
import os
import re

from .model import BankTxn, ProcessorTerms, Settlement, parse_cents, parse_date

MID_RE = re.compile(r"MID (\d{6})")


def load_settlements(data_dir):
    with open(os.path.join(data_dir, "processor_settlements.csv")) as fh:
        return [Settlement(
            settlement_id=r["settlement_id"], settlement_type=r["settlement_type"],
            processor=r["processor"], location_id=r["location_id"] or None,
            period_start=parse_date(r["period_start"]), period_end=parse_date(r["period_end"]),
            txn_count=int(r["txn_count"]), gross_amount=parse_cents(r["gross_amount"]),
            refund_amount=parse_cents(r["refund_amount"]),
            discount_fee=parse_cents(r["discount_fee"]),
            per_txn_fee=parse_cents(r["per_txn_fee"]),
            marketing_fee=parse_cents(r["marketing_fee"]),
            total_fees=parse_cents(r["total_fees"]), net_amount=parse_cents(r["net_amount"]),
            fee_billing=r["fee_billing"],
            expected_deposit_date=parse_date(r["expected_deposit_date"]),
            effective_rate=float(r["effective_rate"]),
        ) for r in csv.DictReader(fh)]


def load_terms(data_dir):
    with open(os.path.join(data_dir, "reference_processors.csv")) as fh:
        return {r["processor"]: ProcessorTerms(
            processor=r["processor"], label=r["label"], payout=r["payout"],
            settlement_lag_days=int(r["settlement_lag_days"]),
            contract_discount_rate=float(r["contract_discount_rate"]),
            contract_per_txn_fee=float(r["contract_per_txn_fee"]),
            fee_billing=r["fee_billing"], marketing_rate=float(r["marketing_rate"]),
            bank_descriptor=r["bank_descriptor"],
        ) for r in csv.DictReader(fh)}


def load_merchant_ids(data_dir):
    with open(os.path.join(data_dir, "reference_merchant_ids.csv")) as fh:
        return {r["merchant_id"]: (r["location_id"], r["processor"])
                for r in csv.DictReader(fh)}


def load_bank(data_dir, terms, mids):
    with open(os.path.join(data_dir, "bank_transactions.csv")) as fh:
        txns = [BankTxn(bank_txn_id=r["bank_txn_id"], posted_date=parse_date(r["posted_date"]),
                        description=r["description"], amount=parse_cents(r["amount"]),
                        balance=parse_cents(r["balance"])) for r in csv.DictReader(fh)]
    for t in txns:
        classify(t, terms, mids)
    return txns


def classify(txn, terms, mids):
    """Decode one statement line.

    The descriptor prefix is the only reliable signal, and it is a trap: a
    'TOAST INC SOFTWARE FEE' debit is not a Toast deposit. Anchoring on the exact
    contracted descriptor ('TST*TOAST INC') rather than on the brand name is what
    keeps software fees, ad spend and vendor drafts out of the match pool.
    """
    desc = txn.description
    if desc.startswith("BRANCH DEPOSIT"):
        txn.processor, txn.kind = "CASH", "CASH_DEPOSIT"
        return txn

    for code, term in terms.items():
        if not term.bank_descriptor or not desc.startswith(term.bank_descriptor):
            continue
        txn.processor = code
        m = MID_RE.search(desc)
        if m:
            txn.merchant_id = m.group(1)
            mapped = mids.get(m.group(1))
            if mapped and mapped[1] == code:
                txn.location_id = mapped[0]
        if "CHGBK" in desc:
            txn.kind = "CHARGEBACK"
        elif "DISCOUNT FEE" in desc:
            txn.kind = "FEE"
        elif "PAYOUT" in desc:
            txn.kind = "PAYOUT"
        elif " DEP " in desc:
            txn.kind = "DEPOSIT"
        else:
            txn.kind = "OTHER"
        return txn

    txn.kind = "OTHER"
    return txn


def load_all(data_dir):
    terms = load_terms(data_dir)
    mids = load_merchant_ids(data_dir)
    return {"settlements": load_settlements(data_dir), "terms": terms,
            "merchant_ids": mids, "bank": load_bank(data_dir, terms, mids)}
