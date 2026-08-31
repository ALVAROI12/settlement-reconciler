"""Invariants that must hold for the dataset to be trustworthy as ground truth."""

import datetime as dt
import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recongen import bizcal
from recongen.config import AnomalyRates, Scenario
from recongen.generate import generate

SMALL = dict(days=45, start_date=dt.date(2024, 6, 1))


def digest(path):
    h = hashlib.sha256()
    for name in sorted(os.listdir(path)):
        if name.endswith(".csv"):
            with open(os.path.join(path, name), "rb") as fh:
                h.update(name.encode())
                h.update(fh.read())
    return h.hexdigest()


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="recongen-test-")
        cls.out = os.path.join(cls.tmp, "run")
        cls.result = generate(Scenario(seed=11, **SMALL), AnomalyRates(), cls.out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_same_seed_is_byte_identical(self):
        other = os.path.join(self.tmp, "again")
        generate(Scenario(seed=11, **SMALL), AnomalyRates(), other)
        self.assertEqual(digest(self.out), digest(other))

    def test_different_seed_differs(self):
        other = os.path.join(self.tmp, "seed12")
        generate(Scenario(seed=12, **SMALL), AnomalyRates(), other)
        self.assertNotEqual(digest(self.out), digest(other))

    def test_order_totals_add_up(self):
        for o in self.result["orders"]:
            self.assertEqual(o.grand_total, o.subtotal - o.discount + o.tax + o.tip)

    def test_payment_totals_add_up(self):
        for p in self.result["payments"]:
            self.assertEqual(p.total_charged, p.amount + p.tip)

    def test_payments_reconcile_to_their_orders(self):
        by_order = {}
        for p in self.result["payments"]:
            by_order.setdefault(p.order_id, []).append(p)
        for o in self.result["orders"]:
            self.assertEqual(sum(p.total_charged for p in by_order[o.order_id]),
                             o.grand_total, o.order_id)

    def test_settlement_net_is_gross_less_refunds_and_fees(self):
        for s in self.result["settlements"]:
            fees = s.discount_fee + s.per_txn_fee + s.marketing_fee
            expected = s.gross_amount - s.refund_amount + s.adjustment_amount
            if s.fee_billing == "DAILY_NET":
                expected -= fees
            self.assertEqual(s.net_amount, expected, s.settlement_id)

    def test_links_conserve_settlement_amounts(self):
        totals = {}
        for l in self.result["links"]:
            totals[l.settlement_id] = totals.get(l.settlement_id, 0) + l.amount
        nets = {s.settlement_id: s.net_amount for s in self.result["settlements"]}
        for sid, total in totals.items():
            self.assertEqual(total, nets[sid], sid)

    def test_settlement_credits_equal_the_sum_of_their_links(self):
        """A cash deposit may be short; an ACH credit never invents money."""
        by_txn = {}
        for l in self.result["links"]:
            by_txn.setdefault(l.txn.bank_txn_id, []).append(l)
        for t in self.result["bank_transactions"]:
            if t.category == "SETTLEMENT" and t.bank_txn_id in by_txn:
                self.assertEqual(t.amount, sum(l.amount for l in by_txn[t.bank_txn_id]),
                                 t.bank_txn_id)

    def test_running_balance_is_continuous(self):
        txns = self.result["bank_transactions"]
        balance = txns[0].balance - txns[0].amount
        for t in txns:
            balance += t.amount
            self.assertEqual(t.balance, balance, t.bank_txn_id)

    def test_statement_is_ordered_and_uniquely_keyed(self):
        txns = self.result["bank_transactions"]
        ids = [t.bank_txn_id for t in txns]
        self.assertEqual(len(ids), len(set(ids)))
        dates = [t.posted_date for t in txns]
        self.assertEqual(dates, sorted(dates))

    def test_deposits_only_land_on_banking_days(self):
        for s in self.result["settlements"]:
            self.assertTrue(bizcal.is_business_day(s.expected_deposit_date),
                            "%s -> %s" % (s.settlement_id, s.expected_deposit_date))
        for t in self.result["bank_transactions"]:
            if t.category in ("SETTLEMENT", "CASH_DEPOSIT", "DUPLICATE_CREDIT"):
                self.assertTrue(bizcal.is_business_day(t.posted_date), t.bank_txn_id)

    def test_every_link_points_at_a_real_settlement_and_line(self):
        sids = set(s.settlement_id for s in self.result["settlements"])
        bids = set(t.bank_txn_id for t in self.result["bank_transactions"])
        for l in self.result["links"]:
            self.assertIn(l.settlement_id, sids)
            self.assertIn(l.txn.bank_txn_id, bids)

    def test_missing_deposits_have_no_links(self):
        linked = set(l.settlement_id for l in self.result["links"])
        for sid, s in self.result["status"].items():
            if s["status"] == "MISSING_DEPOSIT":
                self.assertNotIn(sid, linked, sid)

    def test_expected_files_are_written(self):
        for name in ("pos_orders.csv", "pos_payments.csv", "processor_settlements.csv",
                     "bank_transactions.csv", "reference_processors.csv",
                     "reference_merchant_ids.csv", "ground_truth_links.csv",
                     "ground_truth.json", "run_manifest.json"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)

    def test_bank_statement_never_leaks_the_answer(self):
        with open(os.path.join(self.out, "bank_transactions.csv")) as fh:
            text = fh.read()
        for token in ("settlement_id", "category", "TOAST-L001", "CASH-L001"):
            self.assertNotIn(token, text)


class CleanRunTests(unittest.TestCase):
    def test_clean_scenario_injects_nothing(self):
        tmp = tempfile.mkdtemp(prefix="recongen-clean-")
        try:
            zeros = AnomalyRates(**{k: 0.0 for k in vars(AnomalyRates())})
            result = generate(Scenario(seed=3, **SMALL), zeros, os.path.join(tmp, "clean"))
            self.assertEqual(result["anomalies"], [])
            self.assertFalse([s for s in result["status"].values()
                              if s["status"] == "MISSING_DEPOSIT"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CalendarTests(unittest.TestCase):
    def test_t_plus_two_skips_july_fourth_and_the_weekend(self):
        self.assertEqual(bizcal.add_business_days(dt.date(2024, 7, 3), 2),
                         dt.date(2024, 7, 8))

    def test_observed_holidays_shift_off_the_weekend(self):
        self.assertIn(dt.date(2021, 12, 24), bizcal.bank_holidays(2021))  # Christmas on Sat


if __name__ == "__main__":
    unittest.main(verbosity=2)
