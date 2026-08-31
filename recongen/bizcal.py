"""Banking calendar. Deposits do not land on weekends or federal holidays, and
that single fact generates a large share of real reconciliation breaks.
"""

import datetime as dt

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def _nth_weekday(year, month, weekday, n):
    d = dt.date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    d = nxt - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def federal_holidays(year):
    """Actual (unobserved) US federal holiday dates."""
    return {
        dt.date(year, 1, 1): "New Year's Day",
        _nth_weekday(year, 1, MON, 3): "MLK Day",
        _nth_weekday(year, 2, MON, 3): "Presidents Day",
        _last_weekday(year, 5, MON): "Memorial Day",
        dt.date(year, 6, 19): "Juneteenth",
        dt.date(year, 7, 4): "Independence Day",
        _nth_weekday(year, 9, MON, 1): "Labor Day",
        _nth_weekday(year, 10, MON, 2): "Columbus Day",
        dt.date(year, 11, 11): "Veterans Day",
        _nth_weekday(year, 11, THU, 4): "Thanksgiving",
        dt.date(year, 12, 25): "Christmas Day",
    }


def bank_holidays(year):
    """Observed closures: a Saturday holiday is taken the Friday before, a
    Sunday holiday the Monday after."""
    observed = {}
    for d, name in federal_holidays(year).items():
        if d.weekday() == SAT:
            observed[d - dt.timedelta(days=1)] = name + " (observed)"
        elif d.weekday() == SUN:
            observed[d + dt.timedelta(days=1)] = name + " (observed)"
        else:
            observed[d] = name
    return observed


_CACHE = {}


def _holidays_for(year):
    if year not in _CACHE:
        _CACHE[year] = bank_holidays(year)
    return _CACHE[year]


def holiday_name(d):
    return _holidays_for(d.year).get(d)


def is_business_day(d):
    return d.weekday() < SAT and d not in _holidays_for(d.year)


def next_business_day(d):
    while not is_business_day(d):
        d += dt.timedelta(days=1)
    return d


def add_business_days(d, n):
    """Settlement lag: T+n counted in banking days from the batch date."""
    cur = d
    for _ in range(max(0, n)):
        cur += dt.timedelta(days=1)
        cur = next_business_day(cur)
    return next_business_day(cur)


def date_range(start, days):
    return [start + dt.timedelta(days=i) for i in range(days)]
