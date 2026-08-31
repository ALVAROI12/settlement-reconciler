"""Optional review stage: hand the residual to Claude.

The deterministic stages are good at what they can prove and correctly refuse
everything else. What is left is genuinely ambiguous - a deposit that is close
but not exact, a descriptor that reads like a settlement but matches no schedule,
a batch that may have been funded inside a credit the rules could not decompose.
That is judgement, and it is what a model is for.

Two rules govern this stage, both of them about not trusting the model:

  1. It may only choose among candidate lines it is given. It cannot invent a
     bank_txn_id, and one that is not in the candidate list is discarded.
  2. Arithmetic has the final say. Every proposal is re-checked against the
     amounts before it becomes a link, and a proposal that does not add up is
     recorded as rejected rather than quietly accepted.

So the worst case for a hallucinated answer is a rejected proposal in the report,
never a wrong number in the reconciliation.
"""

import hashlib
import json
import os
import time

MODEL = "claude-opus-5"
MAX_CANDIDATES = 25

SYSTEM_PROMPT = """You reconcile card-processor settlements against a bank statement \
for a multi-location restaurant and retail operator.

A deterministic rules engine has already matched everything it could prove. You are \
seeing only what it refused to decide. Your job is to judge each remaining case.

How money reaches this bank account:
- Card batches fund T+1 to T+3 in banking days, and processors sometimes fund several \
stores, or several days, inside a single ACH credit.
- Amex funds gross and bills its discount rate once a month as a separate debit; other \
processors withhold fees from each deposit.
- Marketplace payouts (DoorDash, Uber Eats) remit weekly, net of roughly 30% commission.
- Cash is banked in aggregate: one teller deposit can cover one to four days of drawers \
from a single store, and the envelope is sometimes short by tens of dollars.
- The account also carries payroll, rent, vendor drafts, loans, insurance, ads and owner \
draws. These are NOT settlements and must never be matched to one, even when the \
descriptor names the same brand - a "TOAST INC SOFTWARE FEE" debit is not a Toast deposit.

Rules for your answer:
- Only ever cite bank_txn_id values from the candidates given to you. Never invent one.
- For a match, the candidate amounts you cite must add up to the settlement's net amount. \
Cash may differ by a small shortage; nothing else may differ at all.
- Answer MISSING only if you believe the money never arrived, not merely that you cannot \
find it.
- Answer UNRESOLVED when the evidence genuinely does not settle the question. That is a \
useful answer here, not a failure. A wrong match costs an operator far more than an \
honest referral to a human.
- Keep the reasoning to one or two sentences, and make it something an operator could \
check against the statement."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["MATCH", "MISSING", "UNRESOLVED"],
            "description": "MATCH if the cited candidates fund this settlement, "
                           "MISSING if the money never arrived, UNRESOLVED otherwise.",
        },
        "bank_txn_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Candidate ids funding this settlement. Empty unless MATCH.",
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1. Below 0.7 the proposal is treated as unresolved.",
        },
        "reasoning": {"type": "string", "description": "One or two sentences."},
    },
    "required": ["verdict", "bank_txn_ids", "confidence", "reasoning"],
    "additionalProperties": False,
}


def _cents(value):
    return "%.2f" % (value / 100.0)


def build_cases(engine, window=8):
    """Describe each unresolved settlement and the lines that could explain it."""
    cases = []
    for settlement, reason in engine.unresolved:
        candidates = []
        for txn in sorted(engine.open_bank.values(),
                          key=lambda t: (t.posted_date, t.bank_txn_id)):
            drift = (txn.posted_date - settlement.expected_deposit_date).days
            if not -2 <= drift <= window:
                continue
            if (txn.amount > 0) != (settlement.net_amount > 0):
                continue
            candidates.append({
                "bank_txn_id": txn.bank_txn_id,
                "posted_date": txn.posted_date.isoformat(),
                "days_from_expected": drift,
                "description": txn.description.strip(),
                "amount": _cents(txn.amount),
            })
        cases.append({
            "settlement": {
                "settlement_id": settlement.settlement_id,
                "type": settlement.settlement_type,
                "processor": settlement.processor,
                "location_id": settlement.location_id,
                "covers": "%s to %s" % (settlement.period_start, settlement.period_end),
                "gross_amount": _cents(settlement.gross_amount),
                "fees": _cents(settlement.total_fees),
                "refunds": _cents(settlement.refund_amount),
                "net_amount": _cents(settlement.net_amount),
                "expected_deposit_date": settlement.expected_deposit_date.isoformat(),
            },
            "why_the_rules_stopped": reason,
            "candidates": candidates[:MAX_CANDIDATES],
        })
    return cases


class ResponseCache:
    """Keyed by the exact question asked, so a rerun costs nothing and an eval is
    reproducible without a network or a key."""

    def __init__(self, path):
        self.path = path
        self.entries = {}
        if path and os.path.exists(path):
            with open(path) as fh:
                self.entries = json.load(fh)

    @staticmethod
    def key(model, case):
        payload = json.dumps({"model": model, "case": case}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def get(self, key):
        return self.entries.get(key)

    def put(self, key, value):
        self.entries[key] = value

    def save(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(self.entries, fh, indent=2, sort_keys=True)


class ResidualReviewer:
    """Asks Claude about each residual case. Offline unless it has to ask."""

    def __init__(self, model=MODEL, cache_path=None, effort="medium", client=None,
                 max_cases=None, offline=False):
        self.model = model
        self.effort = effort
        self.cache = ResponseCache(cache_path)
        self.max_cases = max_cases
        self.offline = offline
        self._client = client
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise RuntimeError(
                    "the review stage needs the Anthropic SDK: pip install anthropic")
            self._client = anthropic.Anthropic()
        return self._client

    def review(self, cases):
        if self.max_cases:
            cases = cases[:self.max_cases]
        verdicts = []
        for case in cases:
            key = ResponseCache.key(self.model, case)
            answer = self.cache.get(key)
            if answer is None:
                if self.offline:
                    continue
                answer = self._ask(case)
                self.cache.put(key, answer)
            answer = dict(answer)
            answer["settlement_id"] = case["settlement"]["settlement_id"]
            verdicts.append(answer)
        self.cache.save()
        return verdicts

    def _ask(self, case):
        response = self.client().messages.create(
            model=self.model,
            max_tokens=2000,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
            messages=[{"role": "user", "content": json.dumps(case, indent=2)}],
        )
        self.calls += 1
        for field in self.usage:
            self.usage[field] += getattr(response.usage, field, 0) or 0
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def review_batched(self, cases, poll_seconds=20, timeout_seconds=3600):
        """Same questions through the Batches API at half the token price.

        Nothing here is latency-sensitive - a month-end reconciliation is run once
        and read later - so the batch endpoint is the right default for a large
        residual. Results come back in any order and are keyed by custom_id.
        """
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        if self.max_cases:
            cases = cases[:self.max_cases]
        pending, verdicts = [], []
        for index, case in enumerate(cases):
            key = ResponseCache.key(self.model, case)
            cached = self.cache.get(key)
            if cached is not None:
                answer = dict(cached)
                answer["settlement_id"] = case["settlement"]["settlement_id"]
                verdicts.append(answer)
            elif not self.offline:
                pending.append(("case-%04d" % index, key, case))

        if pending:
            batch = self.client().messages.batches.create(requests=[
                Request(custom_id=custom_id, params=MessageCreateParamsNonStreaming(
                    model=self.model, max_tokens=2000,
                    system=[{"type": "text", "text": SYSTEM_PROMPT,
                             "cache_control": {"type": "ephemeral"}}],
                    output_config={"effort": self.effort,
                                   "format": {"type": "json_schema",
                                              "schema": VERDICT_SCHEMA}},
                    messages=[{"role": "user", "content": json.dumps(case, indent=2)}],
                )) for custom_id, _, case in pending])

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                status = self.client().messages.batches.retrieve(batch.id)
                if status.processing_status == "ended":
                    break
                time.sleep(poll_seconds)
            else:
                raise RuntimeError("batch %s did not finish in %ds"
                                   % (batch.id, timeout_seconds))

            by_custom_id = dict((custom_id, (key, case))
                                for custom_id, key, case in pending)
            for entry in self.client().messages.batches.results(batch.id):
                if entry.result.type != "succeeded":
                    continue
                key, case = by_custom_id[entry.custom_id]
                message = entry.result.message
                self.calls += 1
                for field in self.usage:
                    self.usage[field] += getattr(message.usage, field, 0) or 0
                text = next(b.text for b in message.content if b.type == "text")
                answer = json.loads(text)
                self.cache.put(key, answer)
                answer = dict(answer)
                answer["settlement_id"] = case["settlement"]["settlement_id"]
                verdicts.append(answer)

        self.cache.save()
        return verdicts


def apply_verdicts(engine, verdicts, cases, min_confidence=0.7):
    """Check every proposal against the money before it becomes a link.

    A proposal survives only if it cites candidates that were actually offered,
    that are still unexplained, and whose amounts add up to the settlement. Anything
    else is recorded as rejected - visible in the report, absent from the numbers.
    """
    from .engine import _cash_tolerance

    candidates_for = dict((c["settlement"]["settlement_id"],
                           set(x["bank_txn_id"] for x in c["candidates"]))
                          for c in cases)
    settlements = dict((s.settlement_id, s) for s, _ in engine.unresolved)
    accepted, rejected = [], []

    for verdict in verdicts:
        settlement_id = verdict["settlement_id"]
        settlement = settlements.get(settlement_id)
        if settlement is None or settlement_id not in engine.open_settlements:
            continue

        confidence = float(verdict.get("confidence") or 0.0)
        reasoning = (verdict.get("reasoning") or "").strip()
        proposed = list(dict.fromkeys(verdict.get("bank_txn_ids") or []))

        def reject(why):
            rejected.append({"settlement_id": settlement_id, "reason": why,
                             "proposed": proposed, "confidence": confidence,
                             "model_reasoning": reasoning})

        if verdict["verdict"] == "MISSING":
            if confidence >= min_confidence and not proposed:
                engine.declared_missing.append(settlement)
                engine.open_settlements.pop(settlement_id, None)
                engine.stage_counts["llm_declared_missing"] += 1
                accepted.append(verdict)
            else:
                reject("missing claim below confidence, or cited lines anyway")
            continue

        if verdict["verdict"] != "MATCH" or not proposed:
            continue  # UNRESOLVED is a legitimate answer, not a rejection

        if confidence < min_confidence:
            reject("confidence %.2f below the %.2f floor" % (confidence, min_confidence))
            continue
        offered = candidates_for.get(settlement_id, set())
        if not set(proposed).issubset(offered):
            reject("cited a bank line that was not among the candidates")
            continue
        if any(bank_txn_id not in engine.open_bank for bank_txn_id in proposed):
            reject("cited a bank line already explained by another settlement")
            continue

        txns = [engine.open_bank[bank_txn_id] for bank_txn_id in proposed]
        total = sum(t.amount for t in txns)
        tolerance = (_cash_tolerance(settlement.net_amount)
                     if settlement.settlement_type == "CASH_DRAWER" else 0)
        if abs(total - settlement.net_amount) > tolerance:
            reject("cited lines total %s against a net of %s"
                   % (_cents(total), _cents(settlement.net_amount)))
            continue

        for txn in txns:
            engine._link(settlement, txn, txn.amount if len(txns) > 1
                         else settlement.net_amount, "llm_review",
                         min(confidence, 0.8), reasoning or "reviewed by model")
        engine._consume([settlement], txns)
        accepted.append(verdict)

    engine.unresolved = [(s, r) for s, r in engine.unresolved
                         if s.settlement_id in engine.open_settlements]
    engine.stage_counts["unresolved"] = len(engine.unresolved)
    return {"accepted": accepted, "rejected": rejected}
