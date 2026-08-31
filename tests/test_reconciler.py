"""Behavioural gates for the reconciler.

The integration tests score against ground truth on freshly generated data, so a
regression in matching quality fails the build rather than quietly shipping.
"""

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import score as scorer
from recongen.config import AnomalyRates, Scenario
from recongen.generate import generate
from reconciler.loader import classify, load_all, load_merchant_ids, load_terms
from reconciler.model import BankTxn, parse_cents
from reconciler.pipeline import reconcile

SMALL = dict(days=60, start_date=dt.date(2024, 6, 1))
OPERATOR_FILES = ("processor_settlements.csv", "bank_transactions.csv",
                  "reference_processors.csv", "reference_merchant_ids.csv")


class MoneyTests(unittest.TestCase):
    def test_amounts_parse_without_float_drift(self):
        self.assertEqual(parse_cents("1234.56"), 123456)
        self.assertEqual(parse_cents("-0.07"), -7)
        self.assertEqual(parse_cents("0.10"), 10)
        self.assertEqual(parse_cents(""), 0)


class DescriptorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="recon-desc-")
        cls.data = os.path.join(cls.tmp, "data")
        generate(Scenario(seed=5, **SMALL), AnomalyRates(), cls.data)
        cls.terms = load_terms(cls.data)
        cls.mids = load_merchant_ids(cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _classify(self, description, amount=100000):
        return classify(BankTxn("B-1", dt.date(2024, 6, 3), description, amount, 0),
                        self.terms, self.mids)

    def test_software_fee_is_not_a_toast_deposit(self):
        """The trap: same brand, opposite direction, not a settlement."""
        t = self._classify("TOAST INC SOFTWARE FEE MONTHLY", -50000)
        self.assertIsNone(t.processor)
        self.assertEqual(t.kind, "OTHER")

    def test_deposit_descriptor_resolves_to_a_location(self):
        mid = next(m for m, (loc, proc) in self.mids.items() if proc == "TOAST")
        t = self._classify("TST*TOAST INC      DEP MID %s TRN00099111" % mid)
        self.assertEqual(t.processor, "TOAST")
        self.assertEqual(t.location_id, self.mids[mid][0])
        self.assertEqual(t.kind, "DEPOSIT")

    def test_operating_debits_stay_unclassified(self):
        for desc in ("GUSTO PAYROLL ACH  PPD ID 400123", "SYSCO 8412 INVOICE",
                     "RENT ACH RIVERSIDE PROPERTIES LLC", "ANALYSIS SERVICE CHARGE"):
            self.assertIsNone(self._classify(desc, -1000).processor, desc)

    def test_cash_and_chargeback_lines_are_distinguished(self):
        self.assertEqual(self._classify("BRANCH DEPOSIT #0421 TELLER 07").kind, "CASH_DEPOSIT")
        self.assertEqual(self._classify("TST*TOAST INC      CHGBK ADJ MID 400123",
                                        -5000).kind, "CHARGEBACK")


class ReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="recon-run-")
        cls.data = os.path.join(cls.tmp, "data")
        generate(Scenario(seed=5, **SMALL), AnomalyRates(), cls.data)
        cls.out = os.path.join(cls.tmp, "out")
        cls.result = reconcile(cls.data, cls.out)
        cls.report = scorer.score(cls.data, os.path.join(cls.out, "predictions.csv"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_it_beats_the_naive_floor(self):
        self.assertGreater(self.report["links"]["f1"], 0.95, self.report["links"])

    def test_it_almost_never_proposes_a_wrong_link(self):
        self.assertGreater(self.report["links"]["precision"], 0.99, self.report["links"])

    def test_it_never_matches_a_payroll_or_rent_line(self):
        self.assertEqual(self.report["false_links_to_non_settlement_lines"]["count"], 0)

    def test_every_match_shape_is_handled(self):
        for shape, v in self.report["by_match_shape"].items():
            self.assertGreater(v["recall"], 0.80, "%s: %s" % (shape, v))

    def test_a_bank_line_is_never_spent_twice(self):
        """Two settlements may share a combined credit, but only if the same
        processor funded them -- otherwise the money has been counted twice."""
        by_txn = {}
        for l in self.result["engine"].links:
            by_txn.setdefault(l.bank_txn_id, []).append(l.settlement_id)
        settlements = {s.settlement_id: s for s in self.result["data"]["settlements"]}
        for bid, sids in by_txn.items():
            processors = set(settlements[s].processor for s in sids)
            self.assertEqual(len(processors), 1, "%s: %s" % (bid, sids))

    def test_links_never_exceed_what_the_credit_paid(self):
        by_id = {t.bank_txn_id: t for t in self.result["data"]["bank"]}
        totals = {}
        for l in self.result["engine"].links:
            totals[l.bank_txn_id] = totals.get(l.bank_txn_id, 0) + l.amount
        for bid, total in totals.items():
            if by_id[bid].kind == "CASH_DEPOSIT":
                continue  # a short envelope is the finding, not an error
            self.assertEqual(total, by_id[bid].amount, bid)

    def test_it_finds_every_injected_fee_overcharge(self):
        with open(os.path.join(self.data, "ground_truth.json")) as fh:
            truth = json.load(fh)
        expected = set(a["settlement_id"] for a in truth["anomalies"]
                       if a["type"] == "fee_overcharge")
        found = set(f.settlement_ids[0] for f in self.result["findings"]
                    if f.kind == "fee_overcharge")
        self.assertEqual(expected - found, set())

    def test_it_writes_an_operator_facing_report(self):
        for name in ("predictions.csv", "matches_detailed.csv", "report.md", "summary.json"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        with open(os.path.join(self.out, "report.md")) as fh:
            self.assertIn("Exceptions", fh.read())

    def test_every_link_carries_a_stage_and_a_reason(self):
        for l in self.result["engine"].links:
            self.assertTrue(l.stage)
            self.assertTrue(l.rationale)
            self.assertGreater(l.confidence, 0.0)


class IsolationTests(unittest.TestCase):
    def test_it_runs_with_the_answer_key_deleted(self):
        """The reconciler must only ever touch what an operator actually has."""
        tmp = tempfile.mkdtemp(prefix="recon-isolated-")
        try:
            full = os.path.join(tmp, "full")
            generate(Scenario(seed=9, days=30, start_date=dt.date(2024, 6, 1)),
                     AnomalyRates(), full)
            blind = os.path.join(tmp, "blind")
            os.makedirs(blind)
            for name in OPERATOR_FILES:
                shutil.copy(os.path.join(full, name), os.path.join(blind, name))
            load_all(blind)
            result = reconcile(blind, os.path.join(tmp, "out"))
            self.assertGreater(result["summary"]["settlements_matched"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
