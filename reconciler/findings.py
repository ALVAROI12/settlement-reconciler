"""Exceptions worth an operator's attention. A reconciliation that only produces
matches is half a product -- the money is in what did not match, and in what
matched but should not have."""

from collections import defaultdict

from .model import Finding


def _dollars(cents):
    return "$%s%.2f" % ("-" if cents < 0 else "", abs(cents) / 100.0)


def expected_fee(settlement, terms):
    term = terms.get(settlement.processor)
    if not term or settlement.gross_amount <= 0:
        return None
    rate = term.contract_discount_rate + (
        term.marketing_rate if settlement.settlement_type == "MARKETPLACE_PAYOUT" else 0.0)
    return int(round(settlement.gross_amount * rate)) + \
        int(round(settlement.txn_count * term.contract_per_txn_fee * 100))


def find_fee_overcharges(settlements, terms, min_gross=2000, rate_tolerance=0.0015):
    """Compare the rate, not the dollars.

    A batch billed 80 basis points over contract is the same breach whether the
    day took $49 or $4,900, and a dollar threshold only ever catches the big days.
    Two floors keep rounding out of the results: a batch has to be worth at least
    $20, and the overage has to clear a quarter -- a single cent of rounding on a
    small batch is a large percentage and no money at all.
    """
    out = []
    for s in settlements:
        expected = expected_fee(s, terms)
        if expected is None or s.gross_amount < min_gross:
            continue
        delta = s.total_fees - expected
        overage_rate = float(delta) / s.gross_amount
        if overage_rate > rate_tolerance and delta > 25:
            out.append(Finding(
                kind="fee_overcharge", severity="HIGH",
                summary="%s billed %s on %s of gross -- %.2f%% over the contracted "
                        "schedule, %s more than agreed" % (
                            s.processor, _dollars(s.total_fees), _dollars(s.gross_amount),
                            100.0 * overage_rate, _dollars(delta)),
                amount=delta, settlement_ids=[s.settlement_id]))
    return out


def find_duplicate_credits(links, bank, open_bank):
    """A leftover credit that mirrors one already explained, within a few days."""
    linked = {}
    by_id = {t.bank_txn_id: t for t in bank}
    for l in links:
        t = by_id[l.bank_txn_id]
        linked.setdefault((t.processor, t.amount), []).append(t)

    out = []
    for t in sorted(open_bank.values(), key=lambda t: (t.posted_date, t.bank_txn_id)):
        if t.amount <= 0 or not t.processor:
            continue
        twins = [o for o in linked.get((t.processor, t.amount), [])
                 if abs((o.posted_date - t.posted_date).days) <= 3]
        if twins:
            out.append(Finding(
                kind="duplicate_credit", severity="HIGH",
                summary="%s credit of %s on %s repeats %s, already matched -- "
                        "likely a double post" % (t.processor, _dollars(t.amount),
                                                  t.posted_date, twins[0].bank_txn_id),
                amount=t.amount, bank_txn_ids=[t.bank_txn_id, twins[0].bank_txn_id]))
    return out


def find_cash_variances(links, bank):
    by_id = {t.bank_txn_id: t for t in bank}
    grouped = defaultdict(list)
    for l in links:
        if l.stage == "cash_rollup":
            grouped[l.bank_txn_id].append(l)

    out = []
    for bid, group in sorted(grouped.items()):
        counted = sum(l.amount for l in group)
        variance = by_id[bid].amount - counted
        if variance:
            out.append(Finding(
                kind="cash_variance", severity="MEDIUM" if abs(variance) < 5000 else "HIGH",
                summary="drawer counted %s, bank took %s on %s -- %s unaccounted"
                        % (_dollars(counted), _dollars(by_id[bid].amount),
                           by_id[bid].posted_date, _dollars(abs(variance))),
                amount=variance, bank_txn_ids=[bid],
                settlement_ids=[l.settlement_id for l in group]))
    return out


def find_missing_deposits(declared_missing):
    return [Finding(
        kind="missing_deposit", severity="HIGH",
        summary="%s of %s was due %s and never funded" % (
            s.processor, _dollars(s.net_amount), s.expected_deposit_date),
        amount=s.net_amount, settlement_ids=[s.settlement_id])
        for s in declared_missing]


def find_unexplained_settlement_lines(open_bank, duplicates):
    """Statement lines that look like processor activity but nothing claims them."""
    dup_ids = set(i for f in duplicates for i in f.bank_txn_ids)
    out = []
    for t in sorted(open_bank.values(), key=lambda t: (t.posted_date, t.bank_txn_id)):
        if not t.processor or t.bank_txn_id in dup_ids:
            continue
        out.append(Finding(
            kind="unexplained_bank_line", severity="MEDIUM",
            summary="%s on %s: %s -- no settlement accounts for it"
                    % (_dollars(t.amount), t.posted_date, t.description.strip()),
            amount=t.amount, bank_txn_ids=[t.bank_txn_id]))
    return out


def collect(engine, settlements, terms, bank):
    duplicates = find_duplicate_credits(engine.links, bank, engine.open_bank)
    findings = []
    findings += find_missing_deposits(engine.declared_missing)
    findings += duplicates
    findings += find_fee_overcharges(settlements, terms)
    findings += find_cash_variances(engine.links, bank)
    findings += find_unexplained_settlement_lines(engine.open_bank, duplicates)
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    findings.sort(key=lambda f: (order[f.severity], f.kind, -abs(f.amount)))
    return findings
