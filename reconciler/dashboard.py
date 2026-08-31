"""Render the reconciliation as a self-contained HTML page.

A month-end reconciliation is scanned, not read: what tied out, what did not, and
what money is at stake, in that order. The page carries no external assets beyond
its two typefaces, so it can be mailed to a bookkeeper or opened off a USB stick
and still look like itself.
"""

import datetime as dt
import html
import os

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

KIND_LABELS = {
    "missing_deposit": "Never funded",
    "duplicate_credit": "Posted twice",
    "fee_overcharge": "Billed over contract",
    "cash_variance": "Cash came up short",
    "unexplained_bank_line": "Nothing accounts for it",
}

STAGE_LABELS = {
    "exact_on_time": "Amount, processor and date agree",
    "exact_late": "Same, funded a few days behind",
    "combined_deposit": "Several batches in one credit",
    "split_deposit": "One batch across several credits",
    "cash_rollup": "Drawers matched to a teller deposit",
    "debit_match": "Chargebacks and monthly fee bills",
    "llm_review": "Referred to model review",
}

DISPOSITION_LABELS = {
    "declared_missing": "Proven absent from the statement",
    "llm_declared_missing": "Proven absent, after review",
    "unresolved": "Left for a human to decide",
    "not_yet_due": "Not yet due at the statement cut-off",
}

CSS = """
:root {
  --ground: #f2f5f3; --surface: #fbfcfb; --surface-2: #eef2f0;
  --ink: #16211e; --ink-2: #4c5c57; --ink-3: #74837e;
  --line: #dce3df; --line-strong: #c3cdc8;
  --accent: #0f5f6e; --accent-soft: #d9e8ea;
  --good: #1f7a4d; --warn: #c07a05; --crit: #bc2a1e;
  --good-soft: #e0efe6; --warn-soft: #f6ecd8; --crit-soft: #f7e2df;
  --radius: 4px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0d1211; --surface: #141b19; --surface-2: #1b2321;
    --ink: #e7eeea; --ink-2: #a7b4af; --ink-3: #7d8b86;
    --line: #232d2a; --line-strong: #33403c;
    --accent: #4aa9bb; --accent-soft: #16333a;
    --good: #34926a; --warn: #b5811c; --crit: #d1584a;
    --good-soft: #14291f; --warn-soft: #2a2314; --crit-soft: #2e1c19;
  }
}
:root[data-theme="dark"] {
  --ground: #0d1211; --surface: #141b19; --surface-2: #1b2321;
  --ink: #e7eeea; --ink-2: #a7b4af; --ink-3: #7d8b86;
  --line: #232d2a; --line-strong: #33403c;
  --accent: #4aa9bb; --accent-soft: #16333a;
  --good: #34926a; --warn: #b5811c; --crit: #d1584a;
  --good-soft: #14291f; --warn-soft: #2a2314; --crit-soft: #2e1c19;
}

* { box-sizing: border-box; }
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: Archivo, "Helvetica Neue", Arial, sans-serif;
  font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1140px; margin: 0 auto; padding: 0 24px 72px; }

.masthead {
  border-bottom: 1px solid var(--line); background: var(--surface);
  padding: 30px 0 22px; margin-bottom: 28px;
}
.masthead .wrap { padding-bottom: 0; }
.masthead h1 {
  font-family: Newsreader, Georgia, "Times New Roman", serif;
  font-weight: 500; font-size: 34px; line-height: 1.15; margin: 0 0 6px;
  letter-spacing: -0.01em; text-wrap: balance;
}
.masthead p { margin: 0; color: var(--ink-2); max-width: 62ch; }
.meta {
  display: flex; flex-wrap: wrap; gap: 8px 26px; margin-top: 18px;
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  font-size: 12px; color: var(--ink-3);
}
.meta b { color: var(--ink-2); font-weight: 500; }

.tiles { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
@media (max-width: 900px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
.tile {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 16px 16px 14px;
  display: flex; flex-direction: column; gap: 2px;
}
.tile .label {
  font-size: 11px; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 600;
}
.tile .value {
  font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  font-size: 27px; font-weight: 500; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums; line-height: 1.2;
}
.tile .sub { font-size: 12.5px; color: var(--ink-2); }
.tile.flag .value { color: var(--crit); }

h2 {
  font-family: Newsreader, Georgia, serif; font-weight: 500;
  font-size: 20px; margin: 40px 0 14px; letter-spacing: -0.005em;
}
h2 .count { color: var(--ink-3); font-size: 14px; font-family: Archivo, sans-serif; }

.split { display: grid; grid-template-columns: 1.35fr 1fr; gap: 22px; align-items: start; }
@media (max-width: 860px) { .split { grid-template-columns: 1fr; } }
.panel {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 18px 20px 20px;
}
.panel h3 {
  margin: 0 0 4px; font-size: 13px; font-weight: 600;
  letter-spacing: 0.05em; text-transform: uppercase; color: var(--ink-2);
}
.panel .note { margin: 0 0 16px; font-size: 13px; color: var(--ink-3); }

.bars { display: flex; flex-direction: column; gap: 11px; }
.bar-row { display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; }
.bar-row .name { font-size: 13.5px; color: var(--ink); }
.bar-row .num {
  font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 13px;
  font-variant-numeric: tabular-nums; color: var(--ink-2); align-self: end;
}
.bar-track { grid-column: 1 / -1; height: 8px; background: var(--surface-2); border-radius: 2px; }
.bar-fill {
  height: 8px; background: var(--accent);
  border-radius: 0 4px 4px 0; min-width: 3px;
}

.dispositions { display: flex; flex-direction: column; gap: 0; }
.disposition {
  display: flex; justify-content: space-between; align-items: baseline; gap: 16px;
  padding: 11px 0; border-bottom: 1px solid var(--line);
}
.disposition:last-child { border-bottom: 0; }
.disposition .name { font-size: 13.5px; }
.disposition .num {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; font-size: 15px; font-weight: 500;
}

.filters { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.chip {
  font: inherit; font-size: 12.5px; cursor: pointer;
  background: var(--surface); color: var(--ink-2);
  border: 1px solid var(--line-strong); border-radius: 999px;
  padding: 5px 13px;
}
.chip:hover { border-color: var(--accent); color: var(--ink); }
.chip[aria-pressed="true"] {
  background: var(--accent); border-color: var(--accent); color: var(--surface);
}
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.chip .n {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; opacity: 0.75; margin-left: 5px;
}

.ledger {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); overflow-x: auto;
}
table { border-collapse: collapse; width: 100%; min-width: 720px; }
thead th {
  text-align: left; font-size: 11px; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 600; padding: 12px 16px;
  border-bottom: 1px solid var(--line-strong); white-space: nowrap;
}
tbody td { padding: 12px 16px; border-bottom: 1px solid var(--line); vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--surface-2); }
td.amount {
  text-align: right; white-space: nowrap;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; font-size: 13.5px;
}
td.what { font-size: 13.5px; color: var(--ink); }
td.what .ids {
  display: block; margin-top: 3px; font-size: 11.5px; color: var(--ink-3);
  font-family: "IBM Plex Mono", ui-monospace, monospace;
}
.sev {
  display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
  font-size: 11px; font-weight: 600; letter-spacing: 0.05em;
  border-radius: 3px; padding: 3px 8px;
}
.sev::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.sev.HIGH { color: var(--crit); background: var(--crit-soft); }
.sev.MEDIUM { color: var(--warn); background: var(--warn-soft); }
.sev.LOW { color: var(--good); background: var(--good-soft); }
.kind { font-size: 12.5px; color: var(--ink-2); white-space: nowrap; }

.empty { padding: 28px 16px; color: var(--ink-3); font-size: 14px; }
footer {
  margin-top: 46px; padding-top: 18px; border-top: 1px solid var(--line);
  color: var(--ink-3); font-size: 12.5px;
}
footer code {
  font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px;
  background: var(--surface-2); padding: 1px 5px; border-radius: 3px;
}
"""

SCRIPT = """
(function () {
  var chips = document.querySelectorAll('.chip');
  var rows = document.querySelectorAll('tbody tr[data-kind]');
  var empty = document.getElementById('no-rows');
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      var kind = chip.dataset.kind;
      chips.forEach(function (c) { c.setAttribute('aria-pressed', String(c === chip)); });
      var shown = 0;
      rows.forEach(function (row) {
        var on = kind === 'all' || row.dataset.kind === kind;
        row.hidden = !on;
        if (on) { shown += 1; }
      });
      empty.hidden = shown > 0;
    });
  });
})();
"""


def _money(cents):
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    whole, frac = divmod(value, 100)
    return "%s$%s.%02d" % (sign, "{:,}".format(whole), frac)


def _esc(text):
    return html.escape(str(text))


def _tile(label, value, sub, flag=False):
    return ('<div class="tile%s"><span class="label">%s</span>'
            '<span class="value">%s</span><span class="sub">%s</span></div>'
            % (" flag" if flag else "", _esc(label), _esc(value), _esc(sub)))


def render(result, title="Reconciliation"):
    summary = result["summary"]
    findings = result["findings"]
    data = result["data"]
    engine = result["engine"]
    stages = summary["by_stage"]

    dates = [t.posted_date for t in data["bank"]]
    period = "%s to %s" % (min(dates), max(dates)) if dates else "no activity"
    settled = summary["settlements"]
    matched = summary["settlements_matched"]
    pct = (100.0 * matched / settled) if settled else 0.0
    at_stake = sum(abs(f.amount) for f in findings if f.kind != "unexplained_bank_line")
    open_items = summary["settlements_declared_missing"] + summary["settlements_unresolved"]

    parts = ['<div class="masthead"><div class="wrap">',
             "<h1>%s</h1>" % _esc(title),
             "<p>Processor settlements tied out against the bank statement, with every "
             "exception ranked by the money behind it.</p>",
             '<div class="meta">',
             "<span><b>Period</b> %s</span>" % _esc(period),
             "<span><b>Settlements</b> %d</span>" % settled,
             "<span><b>Bank lines</b> %d</span>" % summary["bank_transactions"],
             "<span><b>Processors</b> %s</span>"
             % _esc(", ".join(sorted(data["terms"]))),
             "</div></div></div>", '<div class="wrap">']

    parts.append('<div class="tiles">')
    parts.append(_tile("Tied out", "%.1f%%" % pct, "%d of %d settlements" % (matched, settled)))
    parts.append(_tile("Links", "{:,}".format(summary["links_proposed"]),
                       "settlement to bank line"))
    parts.append(_tile("Exceptions", str(len(findings)),
                       "across %d kinds" % len(summary["findings"])))
    parts.append(_tile("At stake", _money(at_stake), "money behind the exceptions",
                       flag=at_stake > 0))
    parts.append(_tile("Open items", str(open_items), "unfunded or awaiting a decision",
                       flag=open_items > 0))
    parts.append("</div>")

    link_stages = [(k, v) for k, v in sorted(stages.items(), key=lambda kv: -kv[1])
                   if k in STAGE_LABELS]
    top = max([v for _, v in link_stages] or [1])
    parts.append('<div class="split">')
    parts.append('<div class="panel"><h3>How each match was made</h3>'
                 '<p class="note">Stages run most-certain first; each one only sees '
                 'what the stage above it could not explain.</p><div class="bars">')
    for stage, count in link_stages:
        parts.append(
            '<div class="bar-row"><span class="name">%s</span>'
            '<span class="num">%s</span>'
            '<div class="bar-track"><div class="bar-fill" style="width:%.1f%%"></div></div>'
            "</div>" % (_esc(STAGE_LABELS[stage]), "{:,}".format(count),
                        100.0 * count / top))
    parts.append("</div></div>")

    parts.append('<div class="panel"><h3>What was not matched</h3>'
                 '<p class="note">A settlement the rules could not prove absent is '
                 'referred, not asserted.</p><div class="dispositions">')
    for key, label in DISPOSITION_LABELS.items():
        if stages.get(key):
            parts.append('<div class="disposition"><span class="name">%s</span>'
                         '<span class="num">%d</span></div>'
                         % (_esc(label), stages[key]))
    parts.append('<div class="disposition"><span class="name">Bank lines with no '
                 'settlement behind them</span><span class="num">%d</span></div>'
                 % summary["bank_lines_unexplained"])
    parts.append("</div></div></div>")

    if result.get("review"):
        review = result["review"]
        parts.append('<h2>Model review <span class="count">of the residual</span></h2>')
        parts.append(
            '<div class="panel"><p class="note" style="margin:0">%d case(s) referred, '
            "%d accepted after the amounts were re-checked, %d proposal(s) rejected. "
            "A proposal that does not add up is recorded here, never in the numbers."
            "</p></div>" % (review["cases"], len(review["accepted"]),
                            len(review["rejected"])))

    ordered = sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], -abs(f.amount)))
    counts = {}
    for finding in ordered:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1

    parts.append('<h2>Exceptions <span class="count">%d, worst first</span></h2>'
                 % len(ordered))
    parts.append('<div class="filters"><button class="chip" data-kind="all" '
                 'aria-pressed="true">Everything<span class="n">%d</span></button>'
                 % len(ordered))
    for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        parts.append('<button class="chip" data-kind="%s" aria-pressed="false">%s'
                     '<span class="n">%d</span></button>'
                     % (_esc(kind), _esc(KIND_LABELS.get(kind, kind)), count))
    parts.append("</div>")

    parts.append('<div class="ledger"><table><thead><tr>'
                 "<th>Severity</th><th>Kind</th><th>What happened</th>"
                 '<th style="text-align:right">Amount</th></tr></thead><tbody>')
    for finding in ordered[:400]:
        ids = finding.settlement_ids + finding.bank_txn_ids
        id_line = ('<span class="ids">%s</span>' % _esc(" · ".join(ids[:4]))) if ids else ""
        parts.append(
            '<tr data-kind="%s"><td><span class="sev %s">%s</span></td>'
            '<td class="kind">%s</td><td class="what">%s%s</td>'
            '<td class="amount">%s</td></tr>'
            % (_esc(finding.kind), finding.severity, finding.severity,
               _esc(KIND_LABELS.get(finding.kind, finding.kind)),
               _esc(finding.summary), id_line, _money(abs(finding.amount))))
    parts.append("</tbody></table>")
    parts.append('<div class="empty" id="no-rows" hidden>Nothing of that kind.</div>')
    parts.append("</div>")

    parts.append("<footer>Generated %s by <code>python3 -m reconciler --html</code>. "
                 "Every figure traces to <code>matches_detailed.csv</code>, which "
                 "carries the stage, confidence and reason behind each link."
                 "</footer>" % dt.datetime.now().strftime("%d %b %Y, %H:%M"))
    parts.append("</div>")

    return (
        "<title>%s</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        "family=Archivo:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&"
        'family=Newsreader:opsz,wght@6..72,400;6..72,500&display=swap">\n'
        "<style>%s</style>\n%s\n<script>%s</script>\n"
        % (_esc(title), CSS, "\n".join(parts), SCRIPT))


def write(result, path, title="Reconciliation"):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(render(result, title))
    return path
