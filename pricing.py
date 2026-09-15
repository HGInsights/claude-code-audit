"""Model pricing table and cost math.

Prices are USD per million tokens, from https://claude.com/pricing (API tab).
Cache multipliers are applied to the base input price:
  - 5-minute cache write: 1.25x base input
  - 1-hour cache write:   2.00x base input
  - cache read:           0.10x base input
Update PRICES when Anthropic publishes new rates; everything else derives from it.
"""

# When the table below was last checked against the published pricing page.
# Every report prints this date, so a reader can see how old the figures are
# without reading the source. Bump it when you re-verify, even if nothing moved
# — "verified unchanged" is the useful signal, not "edited".
PRICES_AS_OF = "2026-09-15"

# How long the table is trusted before reports carry a warning. Anthropic has
# repriced families mid-generation, and a silently stale table makes every
# figure wrong with no signal — which is the failure this guards against.
STALENESS_WARNING_DAYS = 180

CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00
CACHE_READ_MULT = 0.10

# family -> (input $/Mtok, output $/Mtok)
#
# Verified against the published Claude API pricing table. Opus 5 / 4.8 / 4.7 /
# 4.6 are all $5/$25 — NOT the $15/$75 of the Claude 3 Opus era. Getting this
# wrong overstates every figure in the report by 3x, so re-verify before
# editing rather than reasoning from memory.
FAMILY_PRICES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
    "fable": (10.0, 50.0),
}

# Relative capability tier. Used to decide whether a cheaper model could
# plausibly have done the work.
FAMILY_TIER = {"haiku": 1, "fable": 2, "sonnet": 2, "opus": 3}


# Per-family sanity bounds on the INPUT price. These are deliberately tight:
# the realistic failure is a stale figure from an older generation, and a wide
# range would wave it through. Claude 3 Opus was $15/Mtok; current Opus is $5,
# so an upper bound of 20 catches nothing. 8 catches it.
#
# The failure mode is silent — every report still renders, just 3x wrong — so
# this raises at import rather than warning.
_SANE_INPUT_RANGE = {
    "opus": (2.0, 8.0),
    "sonnet": (1.0, 5.0),
    "haiku": (0.2, 2.0),
    "fable": (5.0, 15.0),
}

for _fam, (_in, _out) in FAMILY_PRICES.items():
    _lo, _hi = _SANE_INPUT_RANGE.get(_fam, (0.1, 50.0))
    if not (_lo <= _in <= _hi):
        raise ValueError(
            f"{_fam} input price ${_in}/Mtok is outside the expected "
            f"${_lo}-${_hi} range — verify against published pricing before "
            "changing this, and widen the bound deliberately if a real price "
            "moved outside it."
        )
    if not (3.0 <= _out / _in <= 7.0):
        raise ValueError(
            f"{_fam} output/input ratio is {_out / _in:.1f}x; Claude models "
            "price output at ~5x input, so one of these is likely stale."
        )


def family_of(model: str) -> str:
    """Map a raw model id (claude-opus-4-8, claude-haiku-4-5-2025...) to a family."""
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in m:
            return fam
    return "unknown"


def prices_for(model: str):
    """Return (input_price, output_price) per million tokens for a model id."""
    return FAMILY_PRICES.get(family_of(model), (3.0, 15.0))


def cost_of(model, raw_input, cache_write_5m, cache_write_1h, cache_read, output):
    """Cost in USD for one API call's usage numbers."""
    inp, out = prices_for(model)
    per_tok = inp / 1_000_000
    return (
        raw_input * per_tok
        + cache_write_5m * per_tok * CACHE_WRITE_5M_MULT
        + cache_write_1h * per_tok * CACHE_WRITE_1H_MULT
        + cache_read * per_tok * CACHE_READ_MULT
        + output * (out / 1_000_000)
    )


def cost_without_cache(model, raw_input, cache_write_5m, cache_write_1h, cache_read, output):
    """What the same call would have cost with caching disabled entirely.

    Every token that was written-to or read-from cache would instead be billed
    as ordinary input at 1.0x.
    """
    inp, out = prices_for(model)
    total_input = raw_input + cache_write_5m + cache_write_1h + cache_read
    return total_input * (inp / 1_000_000) + output * (out / 1_000_000)


def pricing_age_days(today=None):
    """Days since the pricing table was last verified."""
    from datetime import date

    asof = date.fromisoformat(PRICES_AS_OF)
    return ((today or date.today()) - asof).days


def is_stale(today=None):
    """True once the table is older than the warning threshold."""
    return pricing_age_days(today) > STALENESS_WARNING_DAYS


def staleness_note(today=None):
    """A line for the report describing how old the pricing is.

    Always returns a stamp so the figures are self-dating; adds a warning only
    once the table is past its threshold. Returned as text rather than printed
    so both the markdown report and the JSON can carry it.
    """
    age = pricing_age_days(today)
    stamp = f"Pricing as of {PRICES_AS_OF} ({age} days ago)."
    if age > STALENESS_WARNING_DAYS:
        return (
            f"{stamp} This is older than {STALENESS_WARNING_DAYS} days — "
            "re-verify against https://claude.com/pricing before acting on "
            "any figure below, as published rates may have changed."
        )
    return stamp
