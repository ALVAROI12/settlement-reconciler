"""The bank statement: what actually hit the account, described the way a bank
describes it - a truncated merchant descriptor, a trace number that matches
nothing in the POS, and no settlement id anywhere.

Everything that makes reconciliation hard lives here: timing, aggregation,
gross-vs-net, and a stream of operating debits that must NOT be matched.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional

from . import bizcal
from .util import apply_rate, to_cents


@dataclass
class BankTxn:
    posted_date: dt.date
    description: str
    amount: int                    # signed cents
    category: str                  # ground-truth only, never written to the statement
    seq: int = 0
    bank_txn_id: str = ""
    balance: int = 0


@dataclass
class Link:
    settlement_id: str
    txn: BankTxn
    relation: str                  # ONE_TO_ONE | ONE_TO_MANY | MANY_TO_ONE
    amount: int


def merchant_id(location_id, processor):
    """Stable pseudo-MID so descriptors can be joined only via reference data."""
    h = (sum(ord(c) for c in location_id) * 37 + sum(ord(c) for c in processor) * 11)
    return "%06d" % (400000 + h % 99999)


class BankBuilder:
    def __init__(self, scenario, rng, cfg, anomaly_log):
        self.scenario = scenario
        self.rng = rng
        self.cfg = cfg
        self.log = anomaly_log
        self.txns: List[BankTxn] = []
        self.links: List[Link] = []
        self.status = {}
        self._seq = 0
        self.start = scenario.start_date
        self.end = scenario.start_date + dt.timedelta(days=scenario.days - 1)

    # - primitives -------------------------------------------------------
    def add(self, date, description, amount, category):
        self._seq += 1
        t = BankTxn(posted_date=date, description=description, amount=amount,
                    category=category, seq=self._seq)
        self.txns.append(t)
        return t

    def _in_window(self, d):
        return self.start <= d <= self.end

    def descriptor(self, s):
        rng = self.rng
        proc = {p.code: p for p in self.scenario.processors}.get(s.processor)
        desc = proc.bank_descriptor if proc else s.processor
        mid = merchant_id(s.location_id, s.processor) if s.location_id else "000000"
        if s.settlement_type == "MARKETPLACE_PAYOUT":
            return "%-18s PAYOUT %s ST-%06d" % (desc, s.period_end.strftime("%y%m%d"),
                                                rng.randrange(1000000))
        if s.settlement_type == "CHARGEBACK":
            return "%-18s CHGBK ADJ MID %s" % (desc, mid)
        if s.settlement_type == "MONTHLY_FEE":
            return "%-18s DISCOUNT FEE %s" % (desc, s.period_end.strftime("%y%m"))
        return "%-18s DEP MID %s TRN%08d" % (desc, mid, rng.randrange(100000000))

    # - settlement realization -------------------------------------------
    def realize(self, settlements):
        cash = [s for s in settlements if s.settlement_type == "CASH_DRAWER"]
        electronic = [s for s in settlements if s.settlement_type != "CASH_DRAWER"]

        plans = [self._plan(s) for s in electronic]
        plans = self._combine(plans)
        for plan in plans:
            self._emit(plan)
        self._emit_cash(cash)

    def _plan(self, s):
        rng, cfg = self.rng, self.cfg
        date = s.expected_deposit_date
        mode, labels = "NORMAL", list(s.labels)

        if s.net_amount == 0:
            return {"settlements": [s], "mode": "ZERO", "date": date, "labels": labels}

        r = rng.random()
        if s.settlement_type in ("BATCH", "MARKETPLACE_PAYOUT"):
            if r < cfg.missing_deposit:
                mode = "MISSING"
            elif r < cfg.missing_deposit + cfg.duplicate_deposit:
                mode = "DUPLICATE"
            elif r < cfg.missing_deposit + cfg.duplicate_deposit + cfg.split_deposit:
                mode = "SPLIT"
            elif r < (cfg.missing_deposit + cfg.duplicate_deposit + cfg.split_deposit
                      + cfg.late_deposit):
                mode = "LATE"
                date = bizcal.add_business_days(date, rng.randint(1, 3))
                labels.append("late_deposit")
        return {"settlements": [s], "mode": mode, "date": date, "labels": labels}

    def _combine(self, plans):
        """Processors routinely fund several locations in one ACH credit."""
        groups = defaultdict(list)
        passthrough = []
        for p in plans:
            s = p["settlements"][0]
            if p["mode"] == "NORMAL" and s.settlement_type == "BATCH" and s.net_amount > 0:
                groups[(s.processor, p["date"])].append(p)
            else:
                passthrough.append(p)

        out = list(passthrough)
        for _, members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            if len(members) > 1 and self.rng.random() < self.cfg.combined_deposit:
                merged = {"settlements": [m["settlements"][0] for m in members],
                          "mode": "COMBINED", "date": members[0]["date"],
                          "labels": ["combined_deposit"]}
                out.append(merged)
            else:
                out.extend(members)
        return out

    def _emit(self, plan):
        mode, date = plan["mode"], plan["date"]
        members = plan["settlements"]
        s = members[0]

        if mode == "ZERO":
            self.status[s.settlement_id] = {"status": "NO_DEPOSIT_EXPECTED",
                                            "labels": plan["labels"]}
            return
        if not self._in_window(date):
            self.status[s.settlement_id] = {"status": "OUT_OF_WINDOW",
                                            "labels": plan["labels"] + ["after_period_end"]}
            return

        if mode == "MISSING":
            self.status[s.settlement_id] = {"status": "MISSING_DEPOSIT",
                                            "labels": plan["labels"] + ["missing_deposit"]}
            self.log.append({"type": "missing_deposit", "settlement_id": s.settlement_id,
                             "amount": s.net_amount, "expected_date": date.isoformat()})
            return

        if mode == "COMBINED":
            total = sum(m.net_amount for m in members)
            t = self.add(date, self.descriptor(s), total, "SETTLEMENT")
            for m in members:
                self.links.append(Link(m.settlement_id, t, "MANY_TO_ONE", m.net_amount))
                self.status[m.settlement_id] = {"status": "MATCHED",
                                                "labels": ["combined_deposit"]}
            self.log.append({"type": "combined_deposit", "processor": s.processor,
                             "date": date.isoformat(),
                             "settlement_ids": [m.settlement_id for m in members],
                             "amount": total})
            return

        if mode == "SPLIT":
            first = apply_rate(s.net_amount, round(self.rng.uniform(0.35, 0.65), 4))
            for part in (first, s.net_amount - first):
                t = self.add(date, self.descriptor(s), part, "SETTLEMENT")
                self.links.append(Link(s.settlement_id, t, "ONE_TO_MANY", part))
            self.status[s.settlement_id] = {"status": "MATCHED",
                                            "labels": plan["labels"] + ["split_deposit"]}
            self.log.append({"type": "split_deposit", "settlement_id": s.settlement_id,
                             "amount": s.net_amount, "date": date.isoformat()})
            return

        t = self.add(date, self.descriptor(s), s.net_amount, "SETTLEMENT")
        self.links.append(Link(s.settlement_id, t, "ONE_TO_ONE", s.net_amount))
        self.status[s.settlement_id] = {"status": "MATCHED", "labels": plan["labels"]}

        if mode == "DUPLICATE":
            dup_date = date if self.rng.random() < 0.5 else bizcal.add_business_days(date, 1)
            if self._in_window(dup_date):
                self.add(dup_date, self.descriptor(s), s.net_amount, "DUPLICATE_CREDIT")
                self.status[s.settlement_id]["labels"].append("duplicate_deposit")
                self.log.append({"type": "duplicate_deposit", "settlement_id": s.settlement_id,
                                 "amount": s.net_amount, "date": dup_date.isoformat()})

    def _emit_cash(self, cash_settlements):
        """The manager banks the drawer every day or three, and the envelope does
        not always agree with the POS."""
        by_loc = defaultdict(list)
        for s in cash_settlements:
            by_loc[s.location_id].append(s)

        for loc, rows in sorted(by_loc.items()):
            rows.sort(key=lambda s: s.period_end)
            i = 0
            while i < len(rows):
                span = self.rng.choice([1, 1, 2, 2, 3])
                group = rows[i:i + span]
                i += span
                total = sum(g.net_amount for g in group)
                if total <= 0:
                    for g in group:
                        self.status[g.settlement_id] = {"status": "NO_DEPOSIT_EXPECTED",
                                                        "labels": []}
                    continue
                date = bizcal.add_business_days(group[-1].period_end, 1)
                if not self._in_window(date):
                    for g in group:
                        self.status[g.settlement_id] = {"status": "OUT_OF_WINDOW",
                                                        "labels": ["after_period_end"]}
                    continue

                variance = 0
                labels = []
                if self.rng.random() < self.cfg.short_cash_deposit:
                    # A drawer is short by tens of dollars, not by half.
                    variance = -min(to_cents(round(self.rng.uniform(2, 90), 2)),
                                    apply_rate(total, 0.06))
                    labels.append("short_cash_deposit")
                deposited = max(0, total + variance)
                desc = "BRANCH DEPOSIT #%04d TELLER %02d" % (
                    self.rng.randrange(10000), self.rng.randrange(1, 30))
                t = self.add(date, desc, deposited, "CASH_DEPOSIT")
                for g in group:
                    self.links.append(Link(g.settlement_id, t, "MANY_TO_ONE", g.net_amount))
                    self.status[g.settlement_id] = {"status": "MATCHED", "labels": list(labels)}
                if variance:
                    self.log.append({"type": "short_cash_deposit", "date": date.isoformat(),
                                     "location_id": loc, "counted": total,
                                     "deposited": deposited, "variance": variance,
                                     "settlement_ids": [g.settlement_id for g in group]})

    # - non-settlement activity ------------------------------------------
    def operating_activity(self):
        """Debits that share the account and sometimes the descriptor. A good
        agent must leave every one of these unmatched."""
        rng = self.rng
        locs = self.scenario.locations
        d = self.start
        payroll_anchor = self.start + dt.timedelta(days=(4 - self.start.weekday()) % 7)

        while d <= self.end:
            if d == payroll_anchor:
                for loc in locs:
                    amt = -to_cents(round(rng.uniform(9_800, 21_500) * (loc.base_daily_orders / 120.0), 2))
                    self.add(bizcal.next_business_day(d),
                             "GUSTO PAYROLL ACH  PPD ID %s" % merchant_id(loc.location_id, "PAY"),
                             amt, "PAYROLL")
                payroll_anchor += dt.timedelta(days=14)

            if d.day == 1:
                for loc in locs:
                    self.add(bizcal.next_business_day(d),
                             "RENT ACH %s PROPERTIES LLC" % loc.name.split()[0].upper(),
                             -to_cents(round(rng.uniform(6_500, 14_000), 2)), "RENT")
                self.add(bizcal.add_business_days(d, 4),
                         "TOAST INC SOFTWARE FEE MONTHLY", -to_cents(round(rng.uniform(320, 690), 2)),
                         "SOFTWARE_FEE")
                self.add(bizcal.add_business_days(d, 6),
                         "ANALYSIS SERVICE CHARGE", -to_cents(round(rng.uniform(38, 145), 2)),
                         "BANK_FEE")
                self.add(bizcal.add_business_days(d, 8),
                         "CITY UTIL AUTOPAY 8812", -to_cents(round(rng.uniform(1_900, 4_400), 2)),
                         "UTILITIES")
                self.add(bizcal.add_business_days(d, 9),
                         "HARTFORD INS PREM ACH", -to_cents(round(rng.uniform(1_100, 1_900), 2)),
                         "INSURANCE")
                self.add(bizcal.add_business_days(d, 2),
                         "EQUIP LOAN PMT 000441", -to_cents(round(rng.uniform(2_200, 2_260), 2)),
                         "LOAN")
                self.add(bizcal.add_business_days(d, 3),
                         "META PLATFORMS ADS 4471", -to_cents(round(rng.uniform(1_400, 5_200), 2)),
                         "MARKETING")

            if d.day == 15:
                self.add(bizcal.next_business_day(d), "OWNER DISTRIBUTION ACH *2210",
                         -to_cents(round(rng.uniform(18_000, 46_000), 2)), "DISTRIBUTION")

            if bizcal.is_business_day(d):
                # Food and supply cost tracks roughly a third of revenue.
                for _ in range(rng.choice([1, 1, 2, 2, 3])):
                    vendor = rng.choice(["SYSCO 8412 INVOICE", "US FOODS ACH PMT",
                                         "BEN E KEITH CO DRAFT", "AMZN MKTPLACE SUPPLIES",
                                         "RESTAURANT DEPOT #22", "COCA-COLA SW BEV ACH"])
                    self.add(d, vendor, -to_cents(round(rng.uniform(900, 9_400), 2)), "VENDOR")

            if bizcal.is_business_day(d) and rng.random() < 0.06:
                self.add(d, "ONLINE TRANSFER TO SAV *4471",
                         -to_cents(round(rng.uniform(2_000, 15_000), 2)), "TRANSFER")

            d += dt.timedelta(days=1)

    # - finalize ---------------------------------------------------------
    def finalize(self):
        self.txns.sort(key=lambda t: (t.posted_date, t.seq))
        balance = to_cents(self.scenario.opening_balance)
        for i, t in enumerate(self.txns, start=1):
            t.bank_txn_id = "B-%06d" % i
            balance += t.amount
            t.balance = balance
        return self.txns, self.links, self.status


def build_bank_statement(scenario, settlements, rng, cfg, anomaly_log):
    b = BankBuilder(scenario, rng, cfg, anomaly_log)
    b.realize(settlements)
    b.operating_activity()
    return b.finalize()
