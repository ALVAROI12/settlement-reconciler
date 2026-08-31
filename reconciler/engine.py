"""The matcher: ordered stages, most-certain first.

Each stage consumes settlements and bank lines it can explain and hands the rest
down. Nothing is ever matched twice, and every link carries the stage that made it
and why - an operator has to be able to argue with the answer.

No business calendar is needed anywhere: the settlement file states the date the
processor expects to fund, and every stage reasons about drift from that date. A
real bank feed would behave the same way.
"""

import datetime as dt
from collections import defaultdict

from .model import MatchLink

DEPOSIT_KINDS = ("DEPOSIT", "PAYOUT")


class Engine:
    def __init__(self, settlements, bank, terms, late_window=5, cash_window=4):
        self.settlements = settlements
        self.bank = bank
        self.terms = terms
        self.late_window = late_window
        self.cash_window = cash_window

        self.links = []
        self.open_settlements = {s.settlement_id: s for s in settlements}
        self.open_bank = {t.bank_txn_id: t for t in bank}
        self.declared_missing = []
        self.unresolved = []
        self.stage_counts = defaultdict(int)
        self.last_statement_date = max(t.posted_date for t in bank) if bank else None

    # - bookkeeping ------------------------------------------------------
    def _link(self, settlement, txn, amount, stage, confidence, rationale):
        self.links.append(MatchLink(settlement.settlement_id, txn.bank_txn_id, amount,
                                    stage, confidence, rationale))
        self.stage_counts[stage] += 1

    def _consume(self, settlements, txns):
        for s in settlements:
            self.open_settlements.pop(s.settlement_id, None)
        for t in txns:
            self.open_bank.pop(t.bank_txn_id, None)

    def _open_settlements(self, types=None, positive=True):
        out = []
        for s in self.open_settlements.values():
            if types and s.settlement_type not in types:
                continue
            if positive and s.net_amount <= 0:
                continue
            out.append(s)
        return sorted(out, key=lambda s: (s.expected_deposit_date, s.settlement_id))

    def _open_bank(self, kinds=None, processor=None, credits_only=True):
        out = []
        for t in self.open_bank.values():
            if kinds and t.kind not in kinds:
                continue
            if processor and t.processor != processor:
                continue
            if credits_only and t.amount <= 0:
                continue
            out.append(t)
        return sorted(out, key=lambda t: (t.posted_date, t.bank_txn_id))

    # - stages -----------------------------------------------------------
    def run(self):
        self.stage_exact_on_time()
        self.stage_exact_late()
        self.stage_combined_deposits()
        self.stage_split_deposits()
        self.stage_cash_rollups()
        self.stage_debits()
        self.stage_declare_missing()
        return self.links

    def stage_exact_on_time(self):
        """Amount, processor and date all agree. Most of the book clears here."""
        self._exact(0, "exact_on_time", 0.99)

    def stage_exact_late(self):
        """Same certainty on amount and processor, funded a few days behind."""
        self._exact(self.late_window, "exact_late", 0.93)

    def _exact(self, window, stage, confidence):
        buckets = defaultdict(list)
        for t in self._open_bank(kinds=DEPOSIT_KINDS):
            buckets[(t.processor, t.amount)].append(t)

        for s in self._open_settlements(types=("BATCH", "MARKETPLACE_PAYOUT")):
            pool = [t for t in buckets.get((s.processor, s.net_amount), [])
                    if t.bank_txn_id in self.open_bank
                    and 0 <= (t.posted_date - s.expected_deposit_date).days <= window]
            if not pool:
                continue
            # A same-amount collision is broken by the MID on the descriptor.
            same_loc = [t for t in pool if t.location_id == s.location_id]
            txn = (same_loc or pool)[0]
            drift = (txn.posted_date - s.expected_deposit_date).days
            self._link(s, txn, s.net_amount, stage,
                       confidence if same_loc else confidence - 0.05,
                       "amount and processor match%s" % (
                           "" if drift == 0 else "; funded %d day(s) late" % drift))
            self._consume([s], [txn])

    def stage_combined_deposits(self):
        """One ACH credit funding several stores, and often several days, at once.

        These get large: Amex funds T+3, so Friday, Saturday and Sunday all land on
        the same Monday, and across three stores that is nine batches inside one
        credit. The first pass looks only at batches due exactly on the credit's
        date - by this point the ones that funded on their own are already gone,
        so what remains due that day is usually the whole group.
        """
        for window in (0, self.late_window):
            for txn in self._open_bank(kinds=DEPOSIT_KINDS):
                pool = [s for s in self._open_settlements(types=("BATCH", "MARKETPLACE_PAYOUT"))
                        if s.processor == txn.processor
                        and 0 <= (txn.posted_date - s.expected_deposit_date).days <= window]
                subset = _subset_summing_to(pool, txn.amount, max_size=12)
                if not subset:
                    continue
                dates = sorted(set(s.period_end for s in subset))
                span = "" if len(dates) == 1 else " covering %s to %s" % (dates[0], dates[-1])
                for s in subset:
                    self._link(s, txn, s.net_amount, "combined_deposit", 0.90,
                               "one of %d batches funded in a single %s credit%s"
                               % (len(subset), txn.processor, span))
                self._consume(subset, [txn])

    def stage_split_deposits(self):
        """The mirror case: one batch paid as two or three credits."""
        for s in self._open_settlements(types=("BATCH", "MARKETPLACE_PAYOUT")):
            pool = [t for t in self._open_bank(kinds=DEPOSIT_KINDS, processor=s.processor)
                    if 0 <= (t.posted_date - s.expected_deposit_date).days <= self.late_window]
            subset = _subset_summing_to(pool, s.net_amount, max_size=3, key=lambda t: t.amount)
            if not subset:
                continue
            for t in subset:
                self._link(s, t, t.amount, "split_deposit", 0.88,
                           "batch funded across %d credits" % len(subset))
            self._consume([s], subset)

    def stage_cash_rollups(self):
        """Branch deposits name no store and no day, and three stores bank on the
        same Monday. Matching them one deposit at a time by closest amount is
        exactly how a person gets this wrong: the nearest-looking run usually
        belongs to another store, and one bad pick strands that store's drawers
        for the rest of the month.

        So each store is solved over the whole period at once. A store's drawers
        can only be banked front to back, which makes its history a partition into
        consecutive runs - a shortest-path problem, not a series of local guesses.
        Stores are then reconciled against each other: when two claim the same
        teller deposit, the closer count keeps it and the other store re-plans
        without it.
        """
        by_loc = defaultdict(list)
        for s in self._open_settlements(types=("CASH_DRAWER",), positive=False):
            by_loc[s.location_id].append(s)
        for rows in by_loc.values():
            rows.sort(key=lambda s: (s.period_end, s.settlement_id))

        by_date = defaultdict(list)
        for t in self._open_bank(kinds=("CASH_DEPOSIT",)):
            by_date[t.posted_date].append(t)

        forbidden = dict((loc, set()) for loc in by_loc)
        plans = {}
        for _ in range(12):
            for loc, rows in by_loc.items():
                plans[loc] = _plan_cash_runs(rows, by_date, forbidden[loc], self.cash_window)
            claimed = defaultdict(list)
            for loc, plan in plans.items():
                for txn, _, _, variance in plan:
                    claimed[txn.bank_txn_id].append((abs(variance), loc))
            contested = [(bid, c) for bid, c in claimed.items() if len(c) > 1]
            if not contested:
                break
            for bid, claimants in contested:
                claimants.sort()
                for _, loser in claimants[1:]:
                    forbidden[loser].add(bid)

        for loc in sorted(plans):
            for txn, i, j, variance in plans[loc]:
                run = by_loc[loc][i:j]
                for s in run:
                    note = "banked with %d day(s) of takings from %s" % (len(run), loc)
                    if variance:
                        note += "; deposit short by %s" % _dollars(-variance)
                    self._link(s, txn, s.net_amount, "cash_rollup",
                               0.85 if not variance else 0.75, note)
                self._consume(run, [txn])

    def stage_debits(self):
        """Chargebacks and the monthly Amex discount bill are debits, and they are
        settlements too - the book does not balance without them."""
        for s in self._open_settlements(types=("CHARGEBACK", "MONTHLY_FEE"), positive=False):
            if s.net_amount >= 0:
                continue
            kind = "CHARGEBACK" if s.settlement_type == "CHARGEBACK" else "FEE"
            for t in sorted(self.open_bank.values(),
                            key=lambda t: (t.posted_date, t.bank_txn_id)):
                if t.kind != kind or t.processor != s.processor or t.amount != s.net_amount:
                    continue
                if not 0 <= (t.posted_date - s.expected_deposit_date).days <= self.late_window:
                    continue
                if kind == "CHARGEBACK" and t.location_id and s.location_id \
                        and t.location_id != s.location_id:
                    continue
                self._link(s, t, s.net_amount, "debit_match", 0.95,
                           "%s debited by %s" % (s.settlement_type.lower().replace("_", " "),
                                                 s.processor))
                self._consume([s], [t])
                break

    def stage_declare_missing(self):
        """Assert a deposit was never funded only when the statement can be shown
        not to contain it. "I could not match this" and "you were never paid" are
        different claims, and only one of them starts an argument with a processor,
        so everything short of proof is left unresolved for review.

        Three things block the claim: cash, which is banked in aggregate and so is
        never proven absent by a missing amount; a settlement too close to the
        statement cut-off to be overdue yet; and the existence of any unexplained
        credit large enough to have carried it.
        """
        cutoff = None
        if self.last_statement_date:
            cutoff = self.last_statement_date - dt.timedelta(days=self.late_window)

        for s in sorted(self.open_settlements.values(),
                        key=lambda s: (s.expected_deposit_date, s.settlement_id)):
            if s.net_amount == 0:
                continue
            if s.settlement_type == "CASH_DRAWER":
                self._defer(s, "cash is banked in aggregate; absence proves nothing")
            elif cutoff and s.expected_deposit_date > cutoff:
                self.stage_counts["not_yet_due"] += 1
            elif self._could_still_be_funded(s):
                self._defer(s, "an unexplained credit is large enough to contain it")
            else:
                self.declared_missing.append(s)
                self.stage_counts["declared_missing"] += 1

    def _defer(self, settlement, reason):
        self.unresolved.append((settlement, reason))
        self.stage_counts["unresolved"] += 1

    def _could_still_be_funded(self, s):
        for t in self.open_bank.values():
            if t.processor != s.processor or (t.amount > 0) != (s.net_amount > 0):
                continue
            if abs(t.amount) < abs(s.net_amount):
                continue
            drift = (t.posted_date - s.expected_deposit_date).days
            if -1 <= drift <= self.late_window:
                return True
        return False


def _cash_tolerance(total):
    """How far a teller deposit may sit from the counted drawer before it stops
    being a rounding-and-skimming problem and becomes an investigation."""
    return max(2500, min(10000, int(total * 0.07)))


def _dollars(cents):
    return "$%.2f" % (cents / 100.0)


def _subset_summing_to(items, target, max_size, key=lambda s: s.net_amount):
    """Which of these amounts add up to exactly this credit?

    Exact, never approximate: money either explains the credit or it does not.
    Solved as a reachability table over attainable totals rather than by trying
    every combination, because a nine-way Amex credit has 512 of them. If two
    different subsets both hit the target the answer is ambiguous, and an
    ambiguous match is worse than none - it is left for a human instead.
    """
    items = [i for i in items if key(i) > 0][:20]
    if len(items) < 2 or target <= 0:
        return None
    if sum(map(key, items)) == target:
        return list(items)  # the whole day's batches funded as one credit

    reach = {0: (0, 1)}
    for idx, item in enumerate(items):
        value = key(item)
        additions = []
        for subtotal, (mask, count) in reach.items():
            combined = subtotal + value
            if combined <= target:
                additions.append((combined, mask | (1 << idx), count))
        for combined, mask, count in additions:
            if combined in reach:
                seen_mask, seen_count = reach[combined]
                reach[combined] = (seen_mask, min(seen_count + count, 2))
            else:
                reach[combined] = (mask, count)

    hit = reach.get(target)
    if not hit or hit[1] > 1:
        return None
    chosen = [items[i] for i in range(len(items)) if hit[0] >> i & 1]
    return chosen if 2 <= len(chosen) <= max_size else None


def _plan_cash_runs(rows, credits_by_date, forbidden, window, max_run=4):
    """Bank one store's drawers across the whole period.

    Every drawer belongs to exactly one trip to the bank and trips happen in order,
    so the only question is where to cut the sequence. Solved backwards: the best
    plan for the drawers from i onward is the best run starting at i plus the best
    plan for whatever follows it. A drawer may also go unbanked, which leaves an
    exception behind instead of forcing a wrong match.

    Returns (bank_txn, start, end, variance) for each run that found its deposit.
    """
    n = len(rows)
    best = [None] * (n + 1)
    best[n] = ((0, 0), None)

    for i in range(n - 1, -1, -1):
        tail_score, _ = best[i + 1]
        choice = (tail_score, None)          # leave this drawer unexplained
        for size in range(1, max_run + 1):
            j = i + size
            if j > n:
                break
            total = sum(s.net_amount for s in rows[i:j])
            tolerance = _cash_tolerance(total)
            due = rows[j - 1].expected_deposit_date
            for drift in range(0, window + 1):
                for txn in credits_by_date.get(due + dt.timedelta(days=drift), ()):
                    if txn.bank_txn_id in forbidden:
                        continue
                    variance = txn.amount - total
                    if not -tolerance <= variance <= 100:
                        continue
                    tail, _ = best[j]
                    candidate = (tail[0] - 1, tail[1] + abs(variance) + drift)
                    if candidate < choice[0]:
                        choice = (candidate, (txn, i, j, variance))
        best[i] = choice

    plan, i = [], 0
    while i < n:
        _, step = best[i]
        if step is None:
            i += 1
            continue
        plan.append(step)
        i = step[2]
    return plan
