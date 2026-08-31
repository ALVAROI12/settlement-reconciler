"""Money is integer cents everywhere. Floats never touch an amount."""

from decimal import Decimal, ROUND_HALF_UP


def to_cents(amount):
    """Dollars (float/str/Decimal) -> integer cents, half-up."""
    return int(Decimal(str(amount)).quantize(Decimal("0.01"), ROUND_HALF_UP) * 100)


def apply_rate(amount_cents, rate):
    """Percentage of a cent amount, rounded half-up the way processors do."""
    q = Decimal(amount_cents) * Decimal(str(rate))
    return int(q.quantize(Decimal("1"), ROUND_HALF_UP))


def fmt(amount_cents):
    """Integer cents -> '1234.56' for CSV output."""
    sign = "-" if amount_cents < 0 else ""
    a = abs(int(amount_cents))
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def jitter(rng, base, spread):
    """base scaled by a symmetric multiplicative wobble of +/- spread."""
    return base * (1.0 + rng.uniform(-spread, spread))


def lognormal_cents(rng, median_dollars, sigma, floor_dollars=3.0):
    """Ticket sizes are right-skewed: many small checks, a long tail of big ones."""
    v = rng.lognormvariate(0.0, sigma) * float(median_dollars)
    return to_cents(max(float(floor_dollars), round(v, 2)))
