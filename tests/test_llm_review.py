"""The review stage is only safe if a wrong answer cannot become a wrong number.

These tests hand the validator deliberately bad proposals -- invented ids, sums
that do not add up, borrowed lines, hedged confidence -- and assert that none of
them reach the reconciliation.
"""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reconciler.engine import Engine
from reconciler.llm import ResidualReviewer, apply_verdicts, build_cases
from reconciler.model import BankTxn, Settlement

DAY = dt.date(2024, 6, 12)


def settlement(sid, net, kind="BATCH"):
    return Settlement(settlement_id=sid, settlement_type=kind, processor="TOAST",
                      location_id="L001", period_start=DAY, period_end=DAY, txn_count=10,
                      gross_amount=net, refund_amount=0, discount_fee=0, per_txn_fee=0,
                      marketing_fee=0, total_fees=0, net_amount=net, fee_billing="DAILY_NET",
                      expected_deposit_date=DAY, effective_rate=0.0)


def credit(bid, amount, day=DAY):
    return BankTxn(bank_txn_id=bid, posted_date=day, description="TST*TOAST INC DEP",
                   amount=amount, balance=0, processor="TOAST", kind="DEPOSIT")


def verdict(sid, kind, ids, confidence=0.9):
    return {"settlement_id": sid, "verdict": kind, "bank_txn_ids": ids,
            "confidence": confidence, "reasoning": "test"}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.s1 = settlement("S-1", 10000)
        self.s2 = settlement("S-2", 20000)
        self.bank = [credit("B-1", 10000), credit("B-2", 6000), credit("B-3", 4000),
                     credit("B-4", 9999)]
        self.engine = Engine([self.s1, self.s2], self.bank, {})
        self.engine.unresolved = [(self.s1, "test"), (self.s2, "test")]
        self.cases = build_cases(self.engine)

    def apply(self, verdicts):
        return apply_verdicts(self.engine, verdicts, self.cases)

    def test_a_correct_single_match_is_accepted(self):
        result = self.apply([verdict("S-1", "MATCH", ["B-1"])])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual([(l.settlement_id, l.bank_txn_id, l.stage)
                          for l in self.engine.links],
                         [("S-1", "B-1", "llm_review")])
        self.assertNotIn("B-1", self.engine.open_bank)

    def test_a_correct_split_match_is_accepted(self):
        result = self.apply([verdict("S-1", "MATCH", ["B-2", "B-3"])])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(sum(l.amount for l in self.engine.links), 10000)

    def test_an_invented_bank_line_is_rejected(self):
        result = self.apply([verdict("S-1", "MATCH", ["B-999"])])
        self.assertEqual(self.engine.links, [])
        self.assertIn("not among the candidates", result["rejected"][0]["reason"])

    def test_amounts_that_do_not_add_up_are_rejected(self):
        result = self.apply([verdict("S-1", "MATCH", ["B-4"])])  # 99.99 against 100.00
        self.assertEqual(self.engine.links, [])
        self.assertIn("total", result["rejected"][0]["reason"])

    def test_a_line_already_spent_cannot_be_borrowed(self):
        self.apply([verdict("S-1", "MATCH", ["B-2", "B-3"])])
        result = self.apply([verdict("S-2", "MATCH", ["B-2", "B-3"])])
        self.assertEqual(len(self.engine.links), 2)
        self.assertIn("already explained", result["rejected"][0]["reason"])

    def test_low_confidence_is_rejected(self):
        result = self.apply([verdict("S-1", "MATCH", ["B-1"], confidence=0.4)])
        self.assertEqual(self.engine.links, [])
        self.assertIn("confidence", result["rejected"][0]["reason"])

    def test_unresolved_is_a_legitimate_answer_not_a_rejection(self):
        result = self.apply([verdict("S-1", "UNRESOLVED", [])])
        self.assertEqual(self.engine.links, [])
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["accepted"], [])

    def test_a_confident_missing_claim_is_recorded(self):
        result = self.apply([verdict("S-1", "MISSING", [])])
        self.assertEqual([s.settlement_id for s in self.engine.declared_missing], ["S-1"])
        self.assertEqual(len(result["accepted"]), 1)

    def test_a_missing_claim_that_also_cites_lines_is_rejected(self):
        result = self.apply([verdict("S-1", "MISSING", ["B-1"])])
        self.assertEqual(self.engine.declared_missing, [])
        self.assertEqual(len(result["rejected"]), 1)

    def test_cash_may_come_up_short_but_a_card_batch_may_not(self):
        short = settlement("S-CASH", 10000, kind="CASH_DRAWER")
        engine = Engine([short], [credit("B-9", 9950)], {})
        engine.unresolved = [(short, "test")]
        cases = build_cases(engine)
        apply_verdicts(engine, [verdict("S-CASH", "MATCH", ["B-9"])], cases)
        self.assertEqual(len(engine.links), 1)

    def test_settlements_it_resolves_leave_the_unresolved_list(self):
        self.apply([verdict("S-1", "MATCH", ["B-1"])])
        self.assertEqual([s.settlement_id for s, _ in self.engine.unresolved], ["S-2"])


class StubMessages:
    def __init__(self, answers, log):
        self.answers = answers
        self.log = log

    def create(self, **kwargs):
        self.log.append(kwargs)
        payload = self.answers.pop(0)

        class Block:
            type = "text"
            text = json.dumps(payload)

        class Usage:
            input_tokens = 10
            output_tokens = 5
            cache_read_input_tokens = 0

        class Response:
            content = [Block()]
            usage = Usage()

        return Response()


class StubClient:
    def __init__(self, answers, log):
        self.messages = StubMessages(answers, log)


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.s1 = settlement("S-1", 10000)
        self.engine = Engine([self.s1], [credit("B-1", 10000)], {})
        self.engine.unresolved = [(self.s1, "test")]
        self.cases = build_cases(self.engine)
        self.answer = {"verdict": "MATCH", "bank_txn_ids": ["B-1"],
                       "confidence": 0.9, "reasoning": "amount and date agree"}

    def test_it_asks_once_and_replays_from_cache_afterwards(self):
        tmp = tempfile.mkdtemp()
        cache = os.path.join(tmp, "cache.json")
        log = []
        first = ResidualReviewer(cache_path=cache, client=StubClient([self.answer], log))
        self.assertEqual(len(first.review(self.cases)), 1)
        self.assertEqual(first.calls, 1)

        replay = ResidualReviewer(cache_path=cache, client=StubClient([], log),
                                  offline=True)
        self.assertEqual(len(replay.review(self.cases)), 1)
        self.assertEqual(replay.calls, 0, "a cached case must never be re-asked")

    def test_offline_with_no_cache_asks_nothing_and_returns_nothing(self):
        reviewer = ResidualReviewer(cache_path=None, client=StubClient([], []),
                                    offline=True)
        self.assertEqual(reviewer.review(self.cases), [])
        self.assertEqual(reviewer.calls, 0)

    def test_the_request_pins_the_model_schema_and_a_cached_system_prompt(self):
        log = []
        ResidualReviewer(cache_path=None,
                         client=StubClient([self.answer], log)).review(self.cases)
        sent = log[0]
        self.assertEqual(sent["model"], "claude-opus-5")
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("effort", sent["output_config"])

    def test_cases_never_leak_a_settlement_id_into_the_candidate_list(self):
        for case in self.cases:
            for candidate in case["candidates"]:
                self.assertNotIn("settlement", candidate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
