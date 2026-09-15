"""Grade a setup on efficiency: what fraction of its own spend is waste.

Deliberately NOT benchmarked against other developers. Spend per day says
nothing about whether a setup is efficient — someone doing more valuable work
will spend more, and that is not a problem to fix. The only question this
module asks is: **of the money this setup spent, how much bought nothing?**

Waste is measured as four independent components, each computed from disjoint
token pools so they can be summed without double-counting:

  1. Context above the window ceiling   — re-read on every turn, avoidable
  2. Retrieval on a premium model       — same tokens, cheaper tier
  3. Failed tool calls                  — billed a turn, produced nothing
  4. Cache rebuilt instead of reused    — paid 1.25x where 0.1x was available

The findings in checks.py deliberately overlap (they describe one problem from
several angles, which is useful for deciding what to *do*). This module is the
opposite: it partitions spend so the total is defensible.
"""

from pricing import CACHE_READ_MULT, CACHE_WRITE_5M_MULT, FAMILY_TIER, prices_for

# Context beyond this is re-read every turn for no benefit. Chosen as the
# standard window: work that genuinely needs more is rare, and this is the
# threshold above which per-turn re-reading dominates.
CONTEXT_CEILING = 200_000

# A call ingesting a lot and emitting almost nothing is retrieval, not
# reasoning. These thresholds match the model_tiering check.
RETRIEVAL_MAX_OUTPUT = 300
RETRIEVAL_MIN_INPUT = 20_000

GRADES = [
    (5, "A", "Efficient — little recoverable waste."),
    (12, "B", "Healthy — minor waste, worth a look but not urgent."),
    (22, "C", "Noticeable waste — a few targeted changes pay for themselves."),
    (35, "D", "Substantial waste — a meaningful share of spend buys nothing."),
    (101, "F", "Most spend is avoidable — the setup, not the workload, is the problem."),
]


def _grade_for(pct):
    for threshold, letter, blurb in GRADES:
        if pct < threshold:
            return letter, blurb
    return "F", GRADES[-1][2]


def compute(ctx):
    """Return a dict describing waste as a share of this setup's own spend."""
    calls = ctx.calls
    total = ctx.total_cost
    if not calls or total <= 0:
        return None

    components = []

    # 1. Context above the ceiling. Charged as cache reads, because that is how
    #    an oversized transcript actually bills on every subsequent turn.
    over_ceiling = 0.0
    for s in ctx.sessions:
        for c in s.main_calls:
            excess = c.total_input - CONTEXT_CEILING
            if excess > 0:
                over_ceiling += excess * prices_for(c.model)[0] / 1e6 * CACHE_READ_MULT
    if over_ceiling > 0:
        components.append({
            "key": "oversized_context",
            "label": "Context carried above 200K",
            "cost": over_ceiling,
            "why": "Re-read on every turn of the session without adding information.",
        })

    # 2. Retrieval on a premium model, priced against the cheapest tier that
    #    could plausibly do it. Only the DELTA is waste, not the whole call —
    #    the work still had to happen somewhere.
    #
    #    Tokens above the ceiling are excluded here: component 1 already
    #    counted them, and charging them twice is exactly the double-count the
    #    per-finding estimates suffer from.
    retrieval_delta = 0.0
    retrieval_calls = 0
    haiku_in, haiku_out = prices_for("haiku")
    for c in calls:
        if FAMILY_TIER.get(c.family, 3) <= 2:
            continue
        if c.output >= RETRIEVAL_MAX_OUTPUT or c.total_input <= RETRIEVAL_MIN_INPUT:
            continue
        billable = min(c.total_input, CONTEXT_CEILING)
        premium_in = prices_for(c.model)[0] / 1e6
        delta_in = (premium_in - haiku_in / 1e6) * billable * CACHE_READ_MULT
        delta_out = (prices_for(c.model)[1] - haiku_out) / 1e6 * c.output
        retrieval_delta += max(0.0, delta_in + delta_out)
        retrieval_calls += 1
    # Not all retrieval-shaped work is safely delegable; discount accordingly.
    retrieval_delta *= 0.6
    if retrieval_delta > 0:
        components.append({
            "key": "premium_retrieval",
            "label": "Retrieval running on a premium model",
            "cost": retrieval_delta,
            "why": (
                f"{retrieval_calls:,} calls read >20K and wrote <300 tokens; the "
                "price gap to a cheap tier is pure overhead."
            ),
        })

    # 3. Failed tool calls: a turn billed for nothing. Priced at the average
    #    main-thread turn, since that is what a failure consumes.
    results = [r for s in ctx.sessions for r in s.tool_results]
    errors = [r for r in results if r.is_error]
    main = [c for c in calls if not c.is_sidechain]
    if errors and main:
        avg_turn = sum(c.cost for c in main) / len(main)
        failed_cost = len(errors) * avg_turn * 0.6  # some probing is legitimate
        components.append({
            "key": "failed_calls",
            "label": "Failed tool calls",
            "cost": failed_cost,
            "why": f"{len(errors):,} calls errored; each billed a full turn and returned nothing.",
        })

    # 4. Cache rebuilt rather than reused. Only the writes beyond a healthy
    #    baseline count — some writing is unavoidable on any first turn.
    read = sum(c.cache_read for c in calls)
    write = sum(c.cache_write for c in calls)
    if read + write > 0:
        healthy_write = (read + write) * 0.10
        excess_write = write - healthy_write
        if excess_write > 0:
            per_tok = sum(
                c.cache_write * prices_for(c.model)[0] / 1e6 for c in calls
            ) / max(write, 1)
            rebuild = excess_write * per_tok * (CACHE_WRITE_5M_MULT - CACHE_READ_MULT)
            if rebuild > 0:
                components.append({
                    "key": "cache_churn",
                    "label": "Cache rebuilt instead of reused",
                    "cost": rebuild,
                    "why": "Prefix re-written at 1.25x where it could have been read at 0.1x.",
                })

    waste = sum(c["cost"] for c in components)
    # The components are disjoint by construction, but clamp anyway — a future
    # component that overlaps shouldn't be able to report >100% waste.
    waste = min(waste, total)
    pct = 100.0 * waste / total
    letter, blurb = _grade_for(pct)

    components.sort(key=lambda c: -c["cost"])
    return {
        "grade": letter,
        "verdict": blurb,
        "waste_usd": waste,
        "waste_pct": pct,
        "efficient_usd": total - waste,
        "total_usd": total,
        "components": components,
    }
