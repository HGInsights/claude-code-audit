"""Cost checks. Each returns a Finding or None.

A Finding carries an estimated dollar saving so the report can rank by impact
rather than by opinion. Estimates are deliberately conservative and every one
states the arithmetic behind it, because a number nobody can reproduce is a
number nobody will act on.
"""

import os
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import agents
import parse
from pricing import (CACHE_READ_MULT, CACHE_WRITE_1H_MULT, CACHE_WRITE_5M_MULT,
                     FAMILY_TIER, prices_for)

CHECKS = []

# Human-readable labels for the failure buckets in `failed_tool_calls`.
ERROR_LABEL = {
    "edit_before_read": "Edit before read",
    "stale_edit": "Stale edit (target text changed)",
    "bad_path": "File or path not found",
    "permission_denied": "Permission denied",
    "missing_command": "Command not found",
    "timeout": "Timed out",
    "shell_failure": "Shell command failed",
    "other": "Other",
}

# Agent types the harness provides. They have no file in .claude/agents/ by
# design, so a missing definition is not a finding for these.
BUILTIN_AGENTS = {
    "Explore", "Plan", "general-purpose", "claude", "fork",
    "statusline-setup", "claude-code-guide", "(default)",
}


def check(fn):
    CHECKS.append(fn)
    return fn


@dataclass
class Finding:
    key: str
    title: str
    severity: str          # "high" | "medium" | "low" | "ok"
    savings: float         # estimated USD saveable over the analysed window
    summary: str           # one-line verdict
    detail: str = ""       # markdown body: the evidence and the math
    fix: str = ""          # what to actually change
    table: list = field(default_factory=list)  # [(col, col, ...)], first row = header


def _pct(n, d):
    return 100.0 * n / d if d else 0.0


def blended_input_price(calls):
    """Input $/token, weighted by tokens actually consumed on each model.

    An unweighted mean over calls prices a Haiku call the same as an Opus call,
    which is wrong by up to 15x when a workload mixes tiers: thousands of small
    cheap subagent calls would drag the effective price far below what the
    expensive calls actually consuming the tokens really cost.
    """
    total_tokens = 0
    total_cost = 0.0
    for c in calls:
        toks = c.total_input
        if toks <= 0:
            continue
        total_tokens += toks
        total_cost += toks * prices_for(c.model)[0] / 1e6
    if not total_tokens:
        return 0.0
    return total_cost / total_tokens


def _money(x):
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    return f"${x:.3f}"


def _tokens(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


# --------------------------------------------------------------------------
# 1. Cache health
# --------------------------------------------------------------------------

@check
def cache_hit_rate(ctx):
    calls = ctx.calls
    read = sum(c.cache_read for c in calls)
    write = sum(c.cache_write for c in calls)
    raw = sum(c.raw_input for c in calls)
    billed_input = read + write + raw
    if not billed_input:
        return None

    hit = _pct(read, billed_input)
    spend = ctx.total_cost
    uncached = sum(c.cost_uncached for c in calls)
    saved = uncached - spend

    # A poor hit rate means the prefix is being rebuilt instead of reused.
    # Model the upside as reaching a healthy 90% hit rate: the tokens that are
    # currently re-written would instead be read at 0.1x.
    savings = 0.0
    if hit < 90 and write > read * 0.15:
        excess = write - (read + write) * 0.10
        if excess > 0:
            per_tok = blended_input_price(calls)
            savings = excess * per_tok * (CACHE_WRITE_5M_MULT - CACHE_READ_MULT)

    if hit >= 90:
        sev, verdict = "ok", f"Cache hit rate {hit:.1f}% — healthy."
    elif hit >= 70:
        sev, verdict = "medium", f"Cache hit rate {hit:.1f}% — some prefix churn."
    else:
        sev, verdict = "high", f"Cache hit rate {hit:.1f}% — the prefix is being rebuilt constantly."

    return Finding(
        key="cache_hit_rate",
        title="Prompt cache hit rate",
        severity=sev,
        savings=savings,
        summary=verdict,
        detail=(
            f"Across {len(calls):,} API calls: **{_tokens(read)}** cache-read vs "
            f"**{_tokens(write)}** cache-write and **{_tokens(raw)}** uncached input.\n\n"
            f"Caching is currently saving {_money(saved)} "
            f"({_pct(saved, uncached):.0f}% off the {_money(uncached)} this would cost "
            f"with caching off).\n\n"
            "A cache read costs 0.1x base input; a 5m write costs 1.25x. So every "
            "avoidable re-write is a 12.5x markup over reusing the prefix."
        ),
        table=[
            ("Metric", "Value"),
            ("Cache hit rate", f"{hit:.1f}%"),
            ("Cache read tokens", _tokens(read)),
            ("Cache write tokens", _tokens(write)),
            ("Uncached input tokens", _tokens(raw)),
            ("Actual spend", _money(spend)),
            ("Spend if caching were off", _money(uncached)),
        ],
        fix=(
            "" if sev == "ok" else
            "Find what changes early in the prompt between turns: a hook injecting a "
            "timestamp or changing content via `UserPromptSubmit`, an MCP server "
            "reconnecting mid-session, or a statusline/CLAUDE.md that mutates. "
            "Anything that edits the prefix invalidates every token after it."
        ),
    )


@check
def cache_ttl(ctx):
    """1h cache writes cost 2x vs 1.25x for 5m. Only worth it if turns are >5min apart."""
    calls = ctx.calls
    w1h = sum(c.cache_write_1h for c in calls)
    w5m = sum(c.cache_write_5m for c in calls)
    total = w1h + w5m
    if not total or w1h == 0:
        return None

    share_1h = _pct(w1h, total)
    if share_1h < 5:
        return None

    # Gap distribution between consecutive main-thread calls, per session.
    gaps = []
    for sess in ctx.sessions:
        stamps = sorted(c.ts for c in sess.main_calls if c.ts)
        for a, b in zip(stamps, stamps[1:]):
            gaps.append((b - a).total_seconds())
    if not gaps:
        return None

    under5 = sum(1 for g in gaps if g < 300)
    mid = sum(1 for g in gaps if 300 <= g <= 3600)
    over60 = sum(1 for g in gaps if g > 3600)

    # The 1h premium only pays off in the 5-60min band. Everywhere else it is
    # pure markup: 2.0x vs 1.25x = 0.75x base input wasted.
    per_tok = blended_input_price(calls)
    wasted_share = _pct(under5 + over60, len(gaps)) / 100
    savings = w1h * per_tok * (CACHE_WRITE_1H_MULT - CACHE_WRITE_5M_MULT) * wasted_share

    useful = _pct(mid, len(gaps))
    sev = "high" if savings > 50 else ("medium" if savings > 5 else "low")

    return Finding(
        key="cache_ttl",
        title="Cache TTL choice (1-hour vs 5-minute)",
        severity=sev,
        savings=savings,
        summary=(
            f"{share_1h:.0f}% of cache writes use the 1h TTL, but only "
            f"{useful:.1f}% of turn gaps fall in the 5–60min window where it pays off."
        ),
        detail=(
            f"The 1-hour extended TTL bills cache writes at **2.0x** base input vs "
            f"**1.25x** for the standard 5-minute TTL. That premium only buys something "
            f"when consecutive turns are more than 5 minutes apart (so the cheap cache "
            f"would have expired) but less than an hour (so the expensive one hasn't).\n\n"
            f"Measured across {len(gaps):,} inter-turn gaps:\n\n"
            f"- **{_pct(under5, len(gaps)):.1f}%** are < 5 min — a 5m cache would have "
            f"survived; the 1h premium bought nothing.\n"
            f"- **{useful:.1f}%** are 5–60 min — the only band where 1h TTL saves a re-write.\n"
            f"- **{_pct(over60, len(gaps)):.1f}%** are > 60 min — the cache expires either way.\n\n"
            f"So the 2x premium is paid on {_tokens(w1h)} tokens to benefit ~{useful:.1f}% of turns."
        ),
        table=[
            ("TTL", "Write tokens", "Share"),
            ("1-hour (2.0x)", _tokens(w1h), f"{share_1h:.1f}%"),
            ("5-minute (1.25x)", _tokens(w5m), f"{100 - share_1h:.1f}%"),
        ],
        fix=(
            "Unset the extended cache TTL so writes bill at 1.25x instead of 2.0x. "
            "Check `CLAUDE_CODE_EXTENDED_CACHE_TTL` / cache settings in your env and "
            "`~/.claude/settings.json`. Worst case is an occasional re-cache when you "
            "step away for more than 5 minutes."
        ),
    )


# --------------------------------------------------------------------------
# 2. Worktrees
# --------------------------------------------------------------------------

@check
def worktree_cache_cost(ctx):
    """Each distinct working dir gets its own cache prefix — more dirs, more cold starts."""
    by_dir = defaultdict(list)
    for sess in ctx.sessions:
        if sess.cwd:
            by_dir[sess.cwd].append(sess)
    if len(by_dir) < 2:
        return None

    # Group sibling worktrees: same parent dir, and at least one path looks like
    # a worktree checkout of another.
    groups = defaultdict(list)
    for d in by_dir:
        groups[os.path.dirname(d.rstrip("/"))].append(d)
    multi = {k: v for k, v in groups.items() if len(v) > 1}

    # A cold start is the first call of a session: cache_read == 0 but a large write.
    cold_tokens = 0
    cold_count = 0
    for sess in ctx.sessions:
        for c in sess.main_calls:
            if c.cache_read == 0 and c.cache_write > 1000:
                cold_tokens += c.cache_write
                cold_count += 1

    if not cold_count:
        return None

    per_tok = blended_input_price(ctx.calls)
    # Cold-start writes are unavoidable in principle, but sessions fragmented
    # across many dirs multiply them. Model the excess as the cold starts beyond
    # one per directory.
    excess = max(0, cold_count - len(by_dir))
    avg_cold = cold_tokens / cold_count
    savings = excess * avg_cold * per_tok * CACHE_WRITE_5M_MULT * 0.5

    rows = [("Working directory", "Sessions", "Cost")]
    ranked = sorted(by_dir.items(), key=lambda kv: -sum(s.cost for s in kv[1]))
    for d, sessions in ranked[:8]:
        rows.append((
            "`" + (d if len(d) < 52 else "…" + d[-49:]) + "`",
            str(len(sessions)),
            _money(sum(s.cost for s in sessions)),
        ))

    sev = "medium" if multi and savings > 5 else "low"
    return Finding(
        key="worktrees",
        title="Worktree / working-directory fragmentation",
        severity=sev,
        savings=savings,
        summary=(
            f"{len(by_dir)} distinct working directories, "
            f"{cold_count:,} cold cache starts ({_tokens(cold_tokens)} written from scratch)."
            + (f" {len(multi)} sibling-worktree group(s) detected." if multi else "")
        ),
        detail=(
            "Every working directory builds its own cache prefix — a different CLAUDE.md, "
            "file tree and git branch means no prefix sharing. Each new session in each "
            "directory pays a **cold write** at 1.25x for the full system prompt + "
            "CLAUDE.md before any reuse begins.\n\n"
            f"Observed {cold_count:,} cold starts averaging {_tokens(avg_cold)} each. "
            f"With {len(by_dir)} directories in play, roughly {excess:,} of those cold "
            "starts are repeat sessions that could have continued an existing warm one.\n\n"
            + ("Sibling worktrees sharing a parent were detected — these duplicate the "
               "same CLAUDE.md and repo context under different paths, so none of that "
               "prefix is shared between them.\n" if multi else "")
        ),
        table=rows,
        fix=(
            "Fewer, longer-lived sessions per worktree beat many short ones. Resume with "
            "`claude --continue` instead of starting cold. If parallel worktrees are a "
            "workflow requirement, keep the shared prefix small — a lean CLAUDE.md pays "
            "off once per worktree per session."
        ),
    )


# --------------------------------------------------------------------------
# 3. Model tiering / subagent delegation
# --------------------------------------------------------------------------

@check
def expensive_model_grunt_work(ctx):
    """High-input / low-output turns on a premium model are retrieval, not reasoning."""
    THRESHOLD_OUT = 300     # tokens of output
    THRESHOLD_IN = 20_000   # tokens of input

    grunt = [
        c for c in ctx.calls
        if c.family == "opus" and c.output < THRESHOLD_OUT and c.total_input > THRESHOLD_IN
    ]
    if not grunt:
        return None

    grunt_cost = sum(c.cost for c in grunt)
    opus_calls = [c for c in ctx.calls if c.family == "opus"]
    if not opus_calls:
        return None

    # Same usage re-priced on Sonnet.
    def repriced(call, fam_in, fam_out):
        return (
            call.raw_input * fam_in / 1e6
            + call.cache_write_5m * fam_in / 1e6 * CACHE_WRITE_5M_MULT
            + call.cache_write_1h * fam_in / 1e6 * CACHE_WRITE_1H_MULT
            + call.cache_read * fam_in / 1e6 * CACHE_READ_MULT
            + call.output * fam_out / 1e6
        )

    s_in, s_out = prices_for("sonnet")
    sonnet_cost = sum(repriced(c, s_in, s_out) for c in grunt)
    savings = (grunt_cost - sonnet_cost) * 0.6  # assume 60% is genuinely delegable

    share = _pct(len(grunt), len(opus_calls))
    sev = "high" if savings > 50 else ("medium" if savings > 5 else "low")

    # Attribute the waste to repos so the fix says where to apply it.
    grunt_ids = {id(c) for c in grunt}
    by_repo = defaultdict(float)
    for s in ctx.sessions:
        for c in s.calls:
            if id(c) in grunt_ids:
                by_repo[s.repo] += c.cost
    top_repos = sorted(by_repo.items(), key=lambda kv: -kv[1])[:3]
    repo_hint = ", ".join(f"`{r}` ({_money(v)})" for r, v in top_repos)

    return Finding(
        key="model_tiering",
        title="Premium model doing retrieval work",
        severity=sev,
        savings=savings,
        summary=(
            f"{len(grunt):,} Opus calls ({share:.0f}% of all Opus calls) consumed "
            f">{THRESHOLD_IN // 1000}K input but produced <{THRESHOLD_OUT} output tokens — "
            f"{_money(grunt_cost)} of read-heavy, low-reasoning work."
        ),
        detail=(
            "A call that ingests a large context and emits almost nothing is doing "
            "**retrieval**: reading files, grepping, gathering context. That is the "
            "cheapest possible cognitive task and it is running on the most expensive "
            "model. Opus input is 5x Sonnet and 15x Haiku.\n\n"
            f"These {len(grunt):,} calls cost {_money(grunt_cost)} on Opus. The identical "
            f"token usage on Sonnet would be {_money(sonnet_cost)}. Assuming ~60% of it is "
            f"genuinely delegable retrieval, that is **{_money(savings)}** recoverable.\n\n"
            f"Median output on these calls: {statistics.median([c.output for c in grunt]):.0f} tokens."
        ),
        table=(
            [("Metric", "Value"),
             ("Opus retrieval-shaped calls", f"{len(grunt):,}"),
             ("Their cost on Opus", _money(grunt_cost)),
             ("Same usage on Sonnet", _money(sonnet_cost)),
             ("Median output tokens",
              f"{statistics.median([c.output for c in grunt]):.0f}")]
            + [("— in `%s`" % r, _money(v)) for r, v in top_repos]
        ),
        fix=_tiering_fix(ctx, repo_hint),
    )


def _tiering_fix(ctx, repo_hint):
    """Remediation text, matched to what is already configured.

    Telling a team that already pins its retrieval agent to Haiku to "create an
    explore agent" is wrong and gets the whole report dismissed. Only recommend
    creating one when there genuinely isn't a cheap retrieval agent.
    """
    if ctx.has_cheap_explore:
        head = (
            "A cheap retrieval agent already exists and is being used, so the gap "
            "is not the agent definition — it is how much reading still happens on "
            "the main thread. Route more of the find/read/gather work through it, "
            "and check whether the *reviewer* subagents are pinned to a premium "
            "model (see the subagent-pinning finding).\n\n"
        )
    else:
        head = (
            "Move the read-heavy phase onto a cheap model.\n\n"
            "**1. Create `~/.claude/agents/explore.md`**:\n\n"
            "```markdown\n"
            "---\n"
            "name: explore\n"
            "description: Read-heavy search and context gathering. Use for locating "
            "code, reading files, and summarising findings.\n"
            "model: haiku\n"
            "tools: Read, Grep, Glob, Bash\n"
            "---\n\n"
            "Gather the requested context and return a concise summary with "
            "`file:line` citations. Return findings, not file contents.\n"
            "Do not read or enumerate `node_modules/`, `.next`, `dist`, `build`, "
            "generated files, or `*.lock`.\n"
            "```\n\n"
            "**2. Route retrieval to it** — delegate the \"find/read/gather\" step "
            "and keep only the decision on the premium model.\n\n"
            "**3. Set the default subagent model** so anything not explicitly "
            "pinned still lands cheap: `CLAUDE_CODE_SUBAGENT_MODEL=haiku` in "
            "`~/.claude/settings.json` under `env`.\n\n"
        )
    return head + (f"**Where it matters most:** {repo_hint}" if repo_hint else "")


@check
def subagent_delegation(ctx):
    """Is read-heavy work delegated at all, and are the subagents on cheap models?"""
    side = [c for c in ctx.calls if c.is_sidechain]
    main = [c for c in ctx.calls if not c.is_sidechain]
    if not main:
        return None

    side_cost = sum(c.cost for c in side)
    main_cost = sum(c.cost for c in main)
    total = side_cost + main_cost

    fam_counts = Counter(c.family for c in side)
    cheap = sum(v for k, v in fam_counts.items() if FAMILY_TIER.get(k, 3) <= 2)
    expensive = sum(v for k, v in fam_counts.items() if FAMILY_TIER.get(k, 3) > 2)

    rows = [("Model", "Subagent calls", "Cost")]
    for fam, n in fam_counts.most_common():
        rows.append((fam, f"{n:,}", _money(sum(c.cost for c in side if c.family == fam))))

    if not side:
        return Finding(
            key="subagents",
            title="Subagent delegation",
            severity="high",
            savings=main_cost * 0.15,
            summary="No subagent usage at all — every token of exploration runs on the main model.",
            detail=(
                "There are no sidechain (subagent) calls in this window. That means all "
                "file reading, searching and context gathering happens on the main "
                "conversation, at the main model's price, and every byte read stays in "
                "the main context window for the rest of the session — inflating every "
                "subsequent turn.\n\n"
                "Delegating exploration to a subagent does two things: it runs at the "
                "subagent's (cheaper) model price, and the bulk output never enters the "
                "main context, so it isn't re-sent on every later turn."
            ),
            table=[],
            fix=(
                "Use the `Explore` agent for broad searches, or define subagents in "
                "`.claude/agents/*.md` with `model: haiku` for retrieval. Rule of thumb: "
                "if a task is high-input and low-judgement, it belongs in a subagent."
            ),
        )

    # Expensive subagents doing subagent work is a partial miss.
    exp_cost = sum(c.cost for c in side if FAMILY_TIER.get(c.family, 3) > 2)
    s_in, s_out = prices_for("sonnet")
    savings = exp_cost * 0.5 if expensive else 0.0
    cheap_share = _pct(cheap, len(side))

    if cheap_share >= 80 and side_cost / total > 0.15:
        sev = "ok"
        summary = (
            f"Good delegation: {_pct(side_cost, total):.0f}% of spend runs in subagents, "
            f"{cheap_share:.0f}% of them on cheaper models."
        )
    elif expensive:
        sev = "medium"
        summary = (
            f"{expensive:,} subagent calls ({_pct(expensive, len(side)):.0f}%) run on a "
            f"premium model, costing {_money(exp_cost)}."
        )
    else:
        sev = "low"
        summary = (
            f"Subagents are used ({_pct(side_cost, total):.0f}% of spend) and mostly on "
            f"cheap models, but delegation is light."
        )

    return Finding(
        key="subagents",
        title="Subagent delegation and model tiering",
        severity=sev,
        savings=savings,
        summary=summary,
        detail=(
            f"Subagent (sidechain) calls: **{len(side):,}** costing {_money(side_cost)}; "
            f"main-thread calls: **{len(main):,}** costing {_money(main_cost)}. "
            f"Subagents are {_pct(side_cost, total):.0f}% of spend.\n\n"
            f"{cheap_share:.0f}% of subagent calls run on Haiku/Sonnet-tier models."
            + (f" The remaining {expensive:,} run on a premium tier — if those are doing "
               f"retrieval rather than judgement, half that {_money(exp_cost)} is recoverable."
               if expensive else "")
        ),
        table=rows,
        fix=(
            "" if sev == "ok" else
            "Pin retrieval subagents to Haiku via `model: haiku` in the agent's frontmatter. "
            "Reserve premium models for the verdict step that reads the subagent's summary."
        ),
    )


# --------------------------------------------------------------------------
# 4. Context bloat
# --------------------------------------------------------------------------

@check
def context_bloat(ctx):
    """A large baseline prefix is paid on every single turn of every session."""
    baselines = []
    for sess in ctx.sessions:
        first = next((c for c in sess.main_calls if c.cache_write > 0), None)
        if first:
            baselines.append(first.cache_write + first.raw_input)
    if not baselines:
        return None

    median = statistics.median(baselines)
    per_tok = blended_input_price(ctx.calls)

    # Everything above a lean ~25K baseline is avoidable. It is paid twice: once
    # per session as a cache write, and again on every turn as a cache read.
    #
    # Only the WRITE half is claimed here. The per-turn read half is already
    # billed by session_hygiene / runaway_context, which charge for total context
    # size above 100K — and the baseline is part of that total. Claiming both
    # would double-count the same tokens across findings, so the reported saving
    # is deliberately the smaller, non-overlapping number.
    LEAN = 25_000
    excess = max(0, median - LEAN)
    turns = sum(len(s.main_calls) for s in ctx.sessions)
    savings = excess * len(ctx.sessions) * per_tok * CACHE_WRITE_5M_MULT
    read_half = excess * turns * per_tok * CACHE_READ_MULT

    if excess <= 0:
        sev = "ok"
    elif median > 60_000:
        sev = "high"
    elif median > 40_000:
        sev = "medium"
    else:
        sev = "low"

    mcp = ctx.mcp_tool_count
    return Finding(
        key="context_bloat",
        title="Baseline context size (system prompt + CLAUDE.md + tool schemas)",
        severity=sev,
        savings=savings,
        summary=(
            f"Median starting context is {_tokens(median)} tokens before any work begins"
            + (f"; {mcp} MCP tools are loaded." if mcp else ".")
        ),
        detail=(
            "The baseline prefix — system prompt, CLAUDE.md, tool definitions, MCP "
            "schemas — is written to cache once per session at 1.25x and then **read on "
            "every single turn** at 0.1x. It is the multiplier on your entire bill: a "
            "session with 200 turns pays for that prefix 200 times.\n\n"
            f"Median baseline here: **{_tokens(median)}** tokens across {len(ctx.sessions)} "
            f"sessions and {turns:,} main-thread turns. A lean setup lands near "
            f"{_tokens(LEAN)}.\n\n"
            f"Trimming it saves {_money(savings)} in cache writes, plus a further "
            f"~{_money(read_half)} in per-turn cache reads — that read saving is "
            "already counted under the session-context findings above, so it is not "
            "added again here.\n\n"
            + (f"MCP tool definitions are a common hidden bulk — **{mcp} tools** are "
               "currently exposed. Every one of their schemas sits in the prefix whether "
               "or not it is ever called.\n" if mcp else "")
        ),
        table=[
            ("Metric", "Value"),
            ("Median baseline context", _tokens(median)),
            ("Largest baseline", _tokens(max(baselines))),
            ("Sessions", f"{len(ctx.sessions):,}"),
            ("Main-thread turns", f"{turns:,}"),
            ("MCP tools exposed", str(mcp) if mcp else "n/a"),
        ],
        fix=(
            "" if sev == "ok" else
            "Trim CLAUDE.md to invariants only — move reference material into skills that "
            "load on demand. Disable MCP servers you don't use in this repo (they cost "
            "prefix bytes even when idle). Prefer deferred/on-demand tool schemas over "
            "always-loaded ones."
        ),
    )


@check
def session_hygiene(ctx):
    """Long sessions re-send an enormous context on every turn."""
    heavy = [s for s in ctx.sessions if s.peak_context() > 150_000]
    if not ctx.sessions:
        return None

    peaks = [s.peak_context() for s in ctx.sessions]
    median_peak = statistics.median(peaks)

    # Cost of the reads between a 100K working ceiling and the 200K window.
    # Anything above 200K is attributed to the runaway_context check instead, so
    # the two findings never bill the same tokens twice.
    CEILING = 100_000
    RUNAWAY = 200_000
    per_tok = blended_input_price(ctx.calls)
    excess_reads = sum(
        max(0, min(c.total_input, RUNAWAY) - CEILING)
        for s in ctx.sessions for c in s.main_calls
    )
    savings = excess_reads * per_tok * CACHE_READ_MULT * 0.5

    # Is a long session one sprawling catch-all, or a single long task? The
    # answer changes the fix entirely: /clear helps the first and is useless
    # advice for the second.
    skill_driven = [s for s in heavy if s.skills_used]
    skill_counts = Counter(k for s in heavy for k in s.skills_used)
    task_shaped = _pct(len(skill_driven), len(heavy)) if heavy else 0

    rows = [("Session", "Turns", "Peak context", "Driven by", "Cost")]
    for s in sorted(ctx.sessions, key=lambda s: -s.cost)[:8]:
        rows.append((
            (s.session_id or "?")[:8],
            f"{len(s.main_calls):,}",
            _tokens(s.peak_context()),
            ", ".join(sorted(s.skills_used)[:2]) or "ad-hoc",
            _money(s.cost),
        ))

    sev = "high" if len(heavy) > len(ctx.sessions) * 0.25 else (
        "medium" if heavy else "low")

    return Finding(
        key="session_hygiene",
        title="Session length and context growth",
        severity=sev,
        savings=savings,
        summary=(
            f"{len(heavy)} of {len(ctx.sessions)} sessions exceeded 150K context; "
            f"median peak is {_tokens(median_peak)}."
        ),
        detail=(
            "Context is re-sent on every turn. A session that grows to 180K tokens is "
            "paying for 180K of cache reads on each subsequent turn, forever — even if "
            "the useful part of the conversation is the last 10K. The reads are cheap "
            "individually (0.1x) but they are paid per turn, so they dominate long sessions.\n\n"
            f"{excess_reads and _tokens(excess_reads) or '0'} tokens were read above a "
            f"{_tokens(CEILING)} working ceiling across this window.\n\n"
            + (f"**{task_shaped:.0f}% of these long sessions run under a skill** — "
               f"{', '.join(f'`{k}` ({n})' for k, n in skill_counts.most_common(3))}. "
               "That means they are single long tasks rather than sessions left open, "
               "so the fix is about keeping a task's context lean, not about clearing "
               "more often.\n\n" if heavy and task_shaped >= 50 else "")
            + f"Compaction events observed: {ctx.compactions}."
            + ("\n\nCompaction itself is not free — it re-reads the whole conversation to "
               "summarise it. Compacting deliberately at a task boundary is cheaper than "
               "letting auto-compact fire at an arbitrary point."
               if ctx.compactions else "")
        ),
        table=rows,
        fix=(
            "" if sev == "low" else
            (
                # A long session that is ONE task can't be /clear'd mid-flight.
                # Recommending it anyway is the kind of advice that gets a report
                # ignored, so give the remediation that actually applies.
                f"These are long *tasks*, not sprawling sessions — {task_shaped:.0f}% of "
                f"them run under a skill ({', '.join(k for k, _ in skill_counts.most_common(3))}), "
                "so `/clear` mid-task is not available. What actually helps:\n\n"
                "1. **Cap what enters the transcript.** The context grows because file "
                "reads and command output accumulate in the main thread. See the "
                "\"What your context is made of\" finding for this setup's actual "
                "split — it says which sources to cap, and whether delegating more "
                "would help or is already saturated.\n"
                "2. **Compact deliberately, not automatically.** Auto-compact fires late "
                "and summarises everything at once. A skill-level checkpoint that writes "
                "state to a dev-doc and compacts at a task boundary keeps the summary "
                "small and relevant.\n"
                "3. **Watch the dev-doc itself.** A dev-doc that accumulates history gets "
                "re-read every session and becomes the bloat it was meant to prevent. "
                "Keep a short 'current state' header at the top and let the agent read "
                "only that; append detail below it.\n"
                "4. **`/rewind` over `/compact`** where it fits — it truncates back to an "
                "already-cached prefix instead of paying to build a new summary."
                if task_shaped >= 50 else
                "`/clear` between unrelated tasks rather than letting one session sprawl. "
                "Start a fresh session per issue/PR. If a session must run long, delegate "
                "the bulky reading to subagents so the transcript stays small."
            )
        ),
    )


@check
def tool_output_waste(ctx):
    """Giant tool results land in context permanently and are re-read every turn."""
    results = [r for s in ctx.sessions for r in s.tool_results]
    if not results:
        return None

    BIG = 20_000  # chars, ~5K tokens
    big = [r for r in results if r.chars > BIG]
    if not big:
        return None

    big_chars = sum(r.chars for r in big)
    est_tokens = big_chars / 4

    # Each big result is re-read on every subsequent turn of its session.
    avg_turns_after = statistics.mean(
        [max(1, len(s.main_calls) / 2) for s in ctx.sessions]
    )
    per_tok = blended_input_price(ctx.calls)
    savings = est_tokens * avg_turns_after * per_tok * CACHE_READ_MULT * 0.5

    # Group by what actually produced the bytes, because the remediation differs
    # completely: an image needs a different fix than a 3000-line source file.
    by_kind = defaultdict(int)
    for r in big:
        by_kind[r.kind] += r.chars

    # Name the specific offenders. "You read too much" is not actionable;
    # "this file, 12 times, 600K chars" is.
    by_target = defaultdict(lambda: {"chars": 0, "n": 0, "repo": "", "kind": ""})
    for r in big:
        if not r.target:
            continue
        key = r.target
        entry = by_target[key]
        entry["chars"] += r.chars
        entry["n"] += 1
        entry["repo"] = r.repo or entry["repo"]
        entry["kind"] = r.kind

    ranked = sorted(by_target.items(), key=lambda kv: -kv[1]["chars"])[:12]
    rows = [("Target", "Repo", "Reads", "Chars", "Type")]
    for target, info in ranked:
        shown = target if len(target) <= 58 else "…" + target[-55:]
        rows.append((
            "`" + shown + "`",
            info["repo"] or "—",
            f"{info['n']:,}",
            f"{info['chars']:,}",
            info["kind"],
        ))

    errors = [r for r in results if r.is_error]

    # Build a remediation list keyed to what was actually found, so each line is
    # a change someone can make rather than general advice.
    steps = []
    if by_kind.get("image"):
        n_img = sum(1 for r in big if r.kind == "image")
        steps.append(
            f"**Images ({n_img} reads, {by_kind['image'] / 1e6:.1f}M chars).** A screenshot "
            "is one of the most expensive things to put in context and it never leaves. "
            "Crop before reading, read it once in a subagent that reports back in text, "
            "or point Playwright at an assertion instead of a screenshot."
        )
    if by_kind.get("instructions"):
        steps.append(
            f"**Skill / dev-doc files ({by_kind['instructions'] / 1e6:.1f}M chars).** These "
            "are being re-read as ordinary files. Split the long ones so only the relevant "
            "section loads, and keep dev-docs append-only with a short 'current state' "
            "header the agent reads instead of the full history."
        )
    if by_kind.get("generated"):
        steps.append(
            f"**Generated / build output ({by_kind['generated'] / 1e6:.1f}M chars).** Add "
            "these paths to the search skip-list in CLAUDE.md and to `.claude/settings.json` "
            "deny rules so they are never read at all."
        )
    if by_kind.get("command"):
        steps.append(
            f"**Shell output ({by_kind['command'] / 1e6:.1f}M chars).** Pipe through "
            "`head -100`, `wc -l`, or `--quiet`. `gh pr diff` and full test output are the "
            "usual culprits — capture to a file and read the slice you need."
        )
    if by_kind.get("file"):
        steps.append(
            f"**Whole-file reads ({by_kind['file'] / 1e6:.1f}M chars).** Use `offset`/`limit`, "
            "or grep for the symbol first and read only around the hit."
        )

    sev = "high" if len(big) > len(results) * 0.05 else "medium"

    return Finding(
        key="tool_output",
        title="Oversized tool output in context",
        severity=sev,
        savings=savings,
        summary=(
            f"{len(big):,} tool results exceeded {BIG // 1000}K characters "
            f"(~{_tokens(est_tokens)} tokens total), and each is re-read on every "
            "later turn of its session."
        ),
        detail=(
            "A tool result is not a one-off cost. Once it lands in the transcript it is "
            "part of the context for every subsequent turn — so a single 600K-char read "
            "keeps billing for the rest of the session.\n\n"
            f"Oversized results: **{len(big):,}** of {len(results):,} total "
            f"({_pct(len(big), len(results)):.1f}%), ~{_tokens(est_tokens)} tokens.\n\n"
            "**This is usually not 'our source files are too big'.** The table below shows "
            "what was actually read; in practice the bulk is images, generated output, and "
            "unbounded shell commands rather than legitimate source. Check the `Type` "
            "column before concluding the codebase is at fault.\n\n"
            f"Failed tool calls (which occupy context while contributing nothing): "
            f"**{len(errors):,}** ({_pct(len(errors), len(results)):.1f}%)."
            + ("\n\nA high error rate usually means retry loops — the same failing edit or "
               "command attempted repeatedly, each attempt paying full context."
               if _pct(len(errors), len(results)) > 10 else "")
        ),
        table=rows,
        fix=" ".join(f"({i}) {s}" for i, s in enumerate(steps, 1)) or (
            "Read with `offset`/`limit`; pipe shell output through `head`."
        ),
    )


@check
def context_composition(ctx):
    """What the main-thread context is actually MADE of.

    Everyone's advice for long sessions is "delegate to subagents". That only
    helps if subagent results are where the context is going. Measuring the
    composition first tells you whether the standard advice applies to this
    setup or targets a rounding error.

    Main thread only: a subagent's own reads never enter the parent context,
    so counting them would credit delegation with bloat it actually prevented.
    """
    results = [
        r for s in ctx.sessions for r in s.tool_results
        if not r.is_error and not r.is_sidechain
    ]
    if len(results) < 100:
        return None

    by_tool = defaultdict(lambda: {"chars": 0, "calls": 0, "sizes": []})
    for r in results:
        name = r.name or "other"
        entry = by_tool[name]
        entry["chars"] += r.chars
        entry["calls"] += 1
        entry["sizes"].append(r.chars)

    total = sum(e["chars"] for e in by_tool.values())
    if not total:
        return None

    ranked = sorted(by_tool.items(), key=lambda kv: -kv[1]["chars"])

    rows = [("Source", "Share", "Chars", "Calls", "Median", "Largest")]
    for name, e in ranked[:8]:
        sizes = sorted(e["sizes"])
        rows.append((
            f"`{name}`",
            f"{_pct(e['chars'], total):.1f}%",
            f"{e['chars'] / 1e6:.1f}M",
            f"{e['calls']:,}",
            f"{sizes[len(sizes) // 2]:,}",
            f"{sizes[-1]:,}",
        ))

    # Growth rate: how fast does a long session approach the ceiling?
    rates = []
    for s in ctx.sessions:
        mains = sorted((c for c in s.main_calls if c.ts), key=lambda c: c.ts)
        if len(mains) < 20:
            continue
        rates.append((mains[-1].total_input - mains[0].total_input) / len(mains))
    growth = statistics.median(rates) if rates else 0
    turns_to_ceiling = int((200_000 - 30_000) / growth) if growth > 0 else 0

    # Is delegation already saturated? Agent results are the only part of the
    # main-thread context that delegating MORE can remove.
    agent_share = _pct(by_tool.get("Agent", {}).get("chars", 0), total)

    # Per-source remediation, keyed to the shape of each source's distribution.
    # A concentrated source (few huge results) and a diffuse one (thousands of
    # small ones) need different fixes, so classify before advising.
    steps = []
    for name, e in ranked[:4]:
        share = _pct(e["chars"], total)
        if share < 8:
            continue
        sizes = sorted(e["sizes"])
        median = sizes[len(sizes) // 2]
        top1 = sum(sizes[-max(1, len(sizes) // 100):])
        concentrated = _pct(top1, e["chars"]) > 40

        if name == "Bash":
            steps.append(
                f"**`Bash` — {share:.0f}%** ({e['calls']:,} calls, median "
                f"{median:,} chars). "
                + ("A few huge outputs dominate; cap those specific commands."
                   if concentrated else
                   "No dominant offender — this is thousands of ordinary "
                   "results accumulating. Capping output has to become a habit "
                   "in your skills (`| head -100`, `--quiet`, `wc -l`, or "
                   "redirect to a file and read back a slice), not a one-time "
                   "cleanup of outliers.")
            )
        elif name == "Read":
            steps.append(
                f"**`Read` — {share:.0f}%** ({e['calls']:,} calls, median "
                f"{median:,} chars, largest {sizes[-1]:,}). Use `offset`/`limit`, "
                "or grep for the symbol first and read only around the hit."
                + ("  The top 1% of reads are most of this — those files are "
                   "the place to start." if concentrated else "")
            )
        elif name == "Agent":
            steps.append(
                f"**`Agent` — {share:.0f}%** ({e['calls']:,} spawns, median "
                f"{median:,} chars returned). Subagent summaries are already "
                "compact; this is not where the context is going."
            )
        else:
            steps.append(
                f"**`{name}` — {share:.0f}%** ({e['calls']:,} calls, median "
                f"{median:,} chars). Worth a look if this is unexpected."
            )

    detail = [
        "Long sessions are usually treated as a discipline problem. They are "
        "really an accumulation problem, and accumulation has a specific "
        "composition worth measuring before acting on generic advice.\n",
        f"Main-thread tool results total **{total / 1e6:.0f}M characters** "
        f"(~{total / 4 / 1e6:.1f}M tokens) across {len(results):,} calls. "
        "Subagent-internal results are excluded: they never enter the parent "
        "context, which is the point of delegating.\n",
    ]
    if growth:
        detail.append(
            f"Median context growth: **{growth:,.0f} tokens per turn** — from a "
            f"typical baseline that reaches the 200K window at roughly "
            f"**turn {turns_to_ceiling}**.\n"
        )

    if agent_share < 15:
        detail.append(
            f"**Delegating more will not fix this.** Subagent returns are only "
            f"**{agent_share:.1f}%** of main-thread context, so even perfect "
            "delegation leaves the rest untouched. The standard 'use subagents' "
            "advice is already saturated here — the top sources below are what "
            "actually need capping.\n"
        )

    top_two = _pct(sum(e["chars"] for _, e in ranked[:2]), total)
    return Finding(
        key="context_composition",
        title="What your context is made of",
        severity="medium",
        savings=0.0,  # diagnostic: the cost is already counted by other checks
        summary=(
            f"`{ranked[0][0]}` and `{ranked[1][0]}` are {top_two:.0f}% of "
            f"everything accumulating in the main thread"
            + (f"; context grows {growth:,.0f} tokens/turn." if growth else ".")
        ),
        detail="\n".join(detail),
        table=rows,
        fix=" ".join(f"({i}) {s}" for i, s in enumerate(steps, 1)),
    )


@check
def runaway_context(ctx):
    """Sessions that blow past the standard 200K window into long-context territory.

    Above ~200K the whole conversation is re-read on every turn at a size no
    amount of caching can make cheap: 800K cache-read tokens on Opus is ~$1.20
    per turn before the model has done anything. These sessions are almost
    always one task that should have been several.
    """
    RUNAWAY = 200_000
    offenders = [s for s in ctx.sessions if s.peak_context() > RUNAWAY]
    if not offenders:
        return None

    # Cost of the cache reads above the window, which is the part that a /clear
    # at a task boundary would simply not have happened.
    per_tok_by_fam = {}
    excess_cost = 0.0
    for s in offenders:
        for c in s.main_calls:
            over = c.total_input - RUNAWAY
            if over > 0:
                inp = prices_for(c.model)[0] / 1e6
                excess_cost += over * inp * CACHE_READ_MULT

    savings = excess_cost * 0.7  # some long-context work is genuinely necessary
    off_cost = sum(s.cost for s in offenders)

    runaway_skills = Counter(k for s in offenders for k in s.skills_used)
    skill_share = _pct(len([s for s in offenders if s.skills_used]), len(offenders))

    rows = [("Session", "Repo", "Turns", "Peak context", "Driven by", "Cost")]
    for s in sorted(offenders, key=lambda s: -s.cost)[:10]:
        rows.append((
            (s.session_id or "?")[:8],
            s.repo,
            f"{len(s.main_calls):,}",
            _tokens(s.peak_context()),
            ", ".join(sorted(s.skills_used)[:2]) or "ad-hoc",
            _money(s.cost),
        ))

    worst = max(offenders, key=lambda s: s.peak_context())
    return Finding(
        key="runaway_context",
        title="Runaway sessions (context beyond 200K)",
        severity="high",
        savings=savings,
        summary=(
            f"{len(offenders)} sessions grew past 200K context — the largest reached "
            f"{_tokens(worst.peak_context())} over {len(worst.main_calls):,} turns. "
            f"These sessions account for {_money(off_cost)} "
            f"({_pct(off_cost, ctx.total_cost):.0f}% of all spend)."
        ),
        detail=(
            "This is the single biggest cost multiplier in most expensive setups.\n\n"
            "Context is re-sent on **every** turn. Once a session reaches 800K tokens, "
            "each additional turn re-reads 800K tokens before the model does any work — "
            "on Opus that is roughly **$1.20 per turn in pure re-reading**, whether the "
            "turn is a one-line edit or a deep refactor. Multiply by several hundred "
            "turns and the session costs more than a month of disciplined usage.\n\n"
            f"Worst offender: `{(worst.session_id or '?')[:8]}` in "
            f"`{(worst.cwd or '?').split('/')[-1]}` — {len(worst.main_calls):,} turns, "
            f"peak {_tokens(worst.peak_context())}, {_money(worst.cost)}.\n\n"
            f"Across all {len(offenders)} runaway sessions, "
            f"{_money(excess_cost)} was spent purely on re-reading context above the "
            "200K mark."
        ),
        table=rows,
        fix=(
            (
                f"{skill_share:.0f}% of these are skill-driven single tasks "
                f"({', '.join(k for k, _ in runaway_skills.most_common(3))}), so "
                "'start a new session' is not the fix. Attack the growth rate instead:\n\n"
                "1. **Cap the biggest sources.** Every byte a tool returns stays in "
                "context for all remaining turns. The \"What your context is made of\" "
                "finding measures which tools those actually are here — delegation only "
                "helps if subagent returns are a large share, which is often not the "
                "case.\n"
                "2. **Checkpoint to a dev-doc and compact at task boundaries** (after "
                "planning, after implementation, before review) rather than letting "
                "auto-compact fire at an arbitrary point.\n"
                "3. **Cap what enters context**: `head` on shell output, `offset`/`limit` "
                "on reads, and never read a screenshot into the main thread.\n"
                "4. If a task genuinely needs 500+ turns, split it — the dev-doc carries "
                "state across a fresh session far more cheaply than the transcript does."
                if skill_share >= 50 else
                "Treat 200K as a hard ceiling. `/clear` at every task boundary — a new "
                "issue, a new PR, a new bug — instead of continuing one session all day. "
                "Push file reading into subagents so their output never enters the main "
                "transcript."
            )
        ),
    )


@check
def failed_tool_calls(ctx):
    """Failed tool calls cost a full turn and teach the model nothing.

    Every failure bills the whole context to produce an error, then bills it
    again for the retry. Unlike most waste this one is fixable at the source:
    the common failures have specific, addressable causes.
    """
    results = [r for s in ctx.sessions for r in s.tool_results]
    if not results:
        return None

    errors = [r for r in results if r.is_error]
    if len(errors) < 10:
        return None

    rate = _pct(len(errors), len(results))

    # A failed call costs roughly one main-thread turn: the context is sent,
    # an error comes back, and the work still has to be redone.
    main = [c for s in ctx.sessions for c in s.main_calls]
    avg_turn = statistics.mean([c.cost for c in main]) if main else 0
    savings = len(errors) * avg_turn * 0.6  # some failures are unavoidable probing

    buckets = Counter(r.error_kind for r in errors)

    rows = [("Failure", "Count", "Share")]
    for kind, n in buckets.most_common(8):
        rows.append((ERROR_LABEL.get(kind, kind), f"{n:,}", f"{_pct(n, len(errors)):.0f}%"))

    # Sessions where failures are concentrated are the ones worth looking at.
    worst = []
    for s in ctx.sessions:
        if len(s.tool_results) < 40:
            continue
        errs = sum(1 for r in s.tool_results if r.is_error)
        if errs:
            worst.append((_pct(errs, len(s.tool_results)), errs, s))
    worst.sort(key=lambda t: -t[0])

    steps = []
    if buckets.get("edit_before_read"):
        steps.append(
            f"**Edit-before-read ({buckets['edit_before_read']}).** The model tried to "
            "edit a file it hadn't opened. Usually means a skill or prompt tells it to "
            "edit a path directly — have that step read first, or pass the content."
        )
    if buckets.get("stale_edit"):
        steps.append(
            f"**Stale edit ({buckets['stale_edit']}).** The target string had already "
            "changed. Common after a formatter or a parallel edit; re-read before "
            "editing rather than reusing a remembered snippet."
        )
    if buckets.get("bad_path"):
        steps.append(
            f"**Bad path ({buckets['bad_path']}).** Files referenced that don't exist — "
            "usually a wrong working directory in a worktree, or a path from CLAUDE.md "
            "that has moved. Check the paths your instructions name still resolve."
        )
    if buckets.get("shell_failure"):
        steps.append(
            f"**Shell failures ({buckets['shell_failure']}).** Commands exiting non-zero. "
            "Worth checking whether a recurring one (a test runner, a lint step) is "
            "failing predictably and could be fixed or removed from the loop."
        )
    if buckets.get("timeout"):
        steps.append(
            f"**Timeouts ({buckets['timeout']}).** These bill the full context and return "
            "nothing. Raise the timeout for known-slow commands or run them in the "
            "background."
        )
    if buckets.get("permission_denied"):
        steps.append(
            f"**Permission denials ({buckets['permission_denied']}).** Each denial costs a "
            "turn. Add the safe, frequent commands to the allow-list in "
            "`.claude/settings.json`."
        )

    sev = "high" if rate > 5 else ("medium" if rate > 2 else "low")
    return Finding(
        key="failed_tools",
        title="Failed tool calls and retry loops",
        severity=sev,
        savings=savings,
        summary=(
            f"{len(errors):,} of {len(results):,} tool calls failed ({rate:.1f}%), "
            f"costing roughly {_money(savings)} in turns that produced nothing."
        ),
        detail=(
            "A failed tool call is billed like any other turn: the whole context goes "
            "up, an error comes back, and the work still has to be done. The retry "
            "then pays for the same context again.\n\n"
            f"At an average of {_money(avg_turn)} per main-thread turn, "
            f"{len(errors):,} failures is about {_money(len(errors) * avg_turn)} of "
            "spend before accounting for the retries. The estimate above discounts "
            "that, since some failures are legitimate probing.\n\n"
            "Unlike most waste in this report, these have specific causes you can fix "
            "at the source rather than habits to change."
        ),
        table=rows,
        fix=" ".join(f"({i}) {s}" for i, s in enumerate(steps, 1)) or (
            "Review the most common failures above and address their causes."
        ),
    )


@check
def subagent_model_pinning(ctx):
    """Which subagents run on a premium model, and what that costs.

    Reads agent definitions at BOTH the user and project level. A check that
    only looks at ~/.claude/agents/ will tell a team with a well-configured repo
    to "create an agents directory", which is wrong and gets the report ignored.
    """
    defs = ctx.agent_defs
    stats = ctx.spawn_stats
    if not defs and not stats:
        return None

    # Cost of sidechain work, split by model tier.
    side = [c for c in ctx.calls if c.is_sidechain]
    premium_calls = [c for c in side if FAMILY_TIER.get(c.family, 3) > 2]
    premium_cost = sum(c.cost for c in premium_calls)
    if not side:
        return None

    # A premium call that emits almost nothing is a wrapper turn — orchestration
    # around an external tool, not generation. Downgrading the model does not
    # remove that cost, because the cost is the re-read context, not the output.
    WRAPPER_OUT = 5
    wrapper = [c for c in premium_calls if c.output <= WRAPPER_OUT]
    substantive = [c for c in premium_calls if c.output > WRAPPER_OUT]
    wrapper_cost = sum(c.cost for c in wrapper)
    substantive_cost = sum(c.cost for c in substantive)

    grouped = agents.by_name(defs)

    # Premium-pinned agents that are spawned often enough to matter. Agents that
    # delegate to an external CLI are tracked separately: their pin sets the
    # fallback tier, so "use a cheaper model" is the wrong recommendation.
    premium = []
    external = []
    for name, group in grouped.items():
        models = {a.model for a in group if a.model}
        if not any("opus" in m for m in models):
            continue
        spawns = stats.get(name, {}).get("spawns", 0)
        row = (name, sorted(models), spawns, group)
        if any(a.delegates_externally for a in group):
            external.append(row)
        else:
            premium.append(row)
    premium.sort(key=lambda t: -t[2])
    external.sort(key=lambda t: -t[2])

    # The same agent pinned differently across repos: one repo has already
    # decided the cheaper model is adequate for this exact job.
    diverging = []
    for name, group in grouped.items():
        # An external-delegating agent's model is its fallback tier. Two repos
        # choosing different fallbacks is not evidence that the cheaper one is
        # adequate for the primary path.
        if any(a.delegates_externally for a in group):
            continue
        models = {a.model or "inherit" for a in group}
        if len(models) > 1:
            cheap = sorted(m for m in models if "haiku" in m or "sonnet" in m)
            pricey = sorted(m for m in models if "opus" in m)
            if cheap and pricey:
                repos = {}
                for a in group:
                    # a.path is <repo>/.claude/agents/<name>.md — climb past
                    # `.claude/agents` before deriving the repo name.
                    root = os.path.dirname(os.path.dirname(os.path.dirname(a.path)))
                    repos.setdefault(a.model or "inherit", set()).add(
                        parse.repo_of(root))
                diverging.append((name, cheap, pricey, repos))

    # Spawned types with no definition anywhere -> they inherit the main model.
    # Built-ins ship with the harness and have no user-authored file, so their
    # absence from .claude/agents/ is normal, not a misconfiguration. What
    # matters for those is whether spawns pass a cheap `model` override.
    defined_names = set(grouped)
    inheriting = []
    for t, info in stats.items():
        if t in defined_names or t in BUILTIN_AGENTS or info["spawns"] < 5:
            continue
        if "(no override)" not in info["overrides"]:
            continue
        inheriting.append((t, info["spawns"]))
    inheriting.sort(key=lambda t: -t[1])

    # Built-ins spawned on the main model with no override.
    unpinned_builtins = []
    for t, info in stats.items():
        if t not in BUILTIN_AGENTS or t == "fork":
            continue
        bare = info["overrides"].get("(no override)", 0)
        if bare >= 5:
            unpinned_builtins.append((t, bare, info["spawns"]))
    unpinned_builtins.sort(key=lambda t: -t[1])

    if not premium and not diverging and not inheriting and not unpinned_builtins:
        return None

    # Savings come only from SUBSTANTIVE premium generation re-priced one tier
    # down. Wrapper turns are excluded: their cost is re-read context, which a
    # cheaper model would still pay. Discounted further because reviewing is
    # judgement work where a downgrade carries real quality risk.
    savings = substantive_cost * 0.8 * 0.5

    rows = [("Subagent", "Pinned model", "Spawns", "Note")]
    for name, models, spawns, group in external[:6]:
        rows.append((
            f"`{name}`",
            "/".join(models),
            f"{spawns:,}" if spawns else "—",
            "external-primary (pin = fallback)",
        ))
    for name, models, spawns, group in premium[:8]:
        rows.append((
            f"`{name}`",
            "/".join(models),
            f"{spawns:,}" if spawns else "—",
            group[0].scope,
        ))
    for name, spawns in inheriting[:5]:
        rows.append((f"`{name}`", "_inherits main model_", f"{spawns:,}", "undefined"))
    for name, bare, total in unpinned_builtins[:5]:
        rows.append((
            f"`{name}`",
            f"_no override on {bare} of {total}_",
            f"{total:,}",
            "built-in",
        ))

    detail = [
        f"Subagent calls cost **{_money(sum(c.cost for c in side))}**, of which "
        f"**{_money(premium_cost)}** ran on a premium model.\n",
        f"Agent definitions found: **{len(defs)}** "
        f"({sum(1 for a in defs if a.scope == 'project')} project-level, "
        f"{sum(1 for a in defs if a.scope == 'user')} user-level).\n",
        "**Most premium subagent spend is not generation.** Splitting those calls "
        f"by what they actually emitted:\n\n"
        f"- **{len(wrapper):,} wrapper turns** (≤{WRAPPER_OUT} output tokens) — "
        f"{_money(wrapper_cost)}. These are orchestration steps: deciding what to "
        "run, shelling out, reading a result. The cost is the context re-read on "
        "each turn, **not** the model's output — so moving them to a cheaper model "
        "saves proportionally less than the headline suggests, and fewer/larger "
        "turns saves more than a downgrade would.\n"
        f"- **{len(substantive):,} substantive turns** (>{WRAPPER_OUT} output "
        f"tokens) — {_money(substantive_cost)}. This is the only part where the "
        "model tier is really the lever, and it is what the saving below is "
        "based on.\n",
    ]

    if external:
        detail.append(
            "**Externally-delegating agents are excluded from the recommendation.** "
            + ", ".join(f"`{n}`" for n, _, _, _ in external[:4])
            + " shell out to an external CLI on the primary path and only use the "
            "pinned model as a fallback. Their `model:` line sets the *fallback* "
            "tier, so changing it does not change what normally runs — and would "
            "quietly downgrade the safety net.\n"
        )

    if diverging:
        detail.append(
            "**The same agent is pinned differently across repos.** One repo has "
            "already decided a cheaper model does this exact job well enough:\n"
        )
        for name, cheap, pricey, repos in diverging[:4]:
            cheap_repos = ", ".join(sorted(r for m in cheap for r in repos.get(m, [])))
            pricey_repos = ", ".join(sorted(r for m in pricey for r in repos.get(m, [])))
            detail.append(
                f"- `{name}`: **{'/'.join(pricey)}** in {pricey_repos} — but "
                f"**{'/'.join(cheap)}** in {cheap_repos}."
            )
        detail.append("")

    if inheriting:
        detail.append(
            "**Spawned but never defined**, so these inherit the main "
            "conversation's model: "
            + ", ".join(f"`{n}` ({c} spawns)" for n, c in inheriting[:5]) + "\n"
        )

    if unpinned_builtins:
        detail.append(
            "**Built-in agents spawned without a `model` override**, so they run on "
            "the main model: "
            + ", ".join(f"`{n}` ({b} of {t} spawns)"
                        for n, b, t in unpinned_builtins[:5])
            + ". These need no definition file — pass `model` at spawn time, or set "
              "a cheap default.\n"
        )

    # A reviewer that reads a large diff and emits a short verdict is mostly
    # retrieval; the ratio makes that measurable rather than assumed.
    # The overall input:output ratio is dominated by wrapper turns, so quoting it
    # as evidence of "retrieval on an expensive model" would overstate the case.
    # Report the ratio for substantive turns only.
    if substantive:
        s_in = sum(c.total_input for c in substantive)
        s_out = sum(c.output for c in substantive)
        if s_out:
            detail.append(
                f"Across the substantive turns alone the ratio is "
                f"**{s_in / s_out:.0f}:1** ({_tokens(s_in)} read vs "
                f"{_tokens(s_out)} written). A high ratio here means the model is "
                "still ingesting a lot to produce a little — worth splitting into a "
                "cheap gather stage and a premium verdict stage.\n"
            )

    fixes = []
    if wrapper_cost > substantive_cost:
        fixes.append(
            f"Most of this ({_money(wrapper_cost)}) is wrapper turns, so the lever is "
            "**turn count, not model tier**: each orchestration step re-reads the "
            "whole subagent context to emit a few tokens. Batch the shell steps "
            "(probe, write prompt, invoke, read result) into fewer calls and the "
            "cost falls without touching the model."
        )
    if diverging:
        fixes.append(
            "Align the divergent agents on the cheaper model that another repo is "
            "already using in production, then compare review quality on the next "
            "few PRs before deciding it was wrong."
        )
    if premium and substantive_cost > 20:
        fixes.append(
            "For the agents that genuinely generate on a premium model, split the "
            "work rather than downgrading the verdict: a cheap first stage gathers "
            "context, and the premium model judges only that summary."
        )
    if inheriting:
        fixes.append(
            "Define the undefined agents (or pass `model:` at spawn) so they stop "
            "inheriting the main model by default."
        )
    if unpinned_builtins:
        fixes.append(
            "Set `CLAUDE_CODE_SUBAGENT_MODEL=haiku` under `env` in "
            "`~/.claude/settings.json` so built-in agents spawned without an "
            "explicit override stop defaulting to the main model."
        )

    return Finding(
        key="subagent_pinning",
        title="Subagent model pinning",
        severity="medium" if savings > 5 else "low",
        savings=savings,
        summary=(
            f"{_money(premium_cost)} of subagent work runs on a premium model, but "
            f"only {_money(substantive_cost)} of it is actual generation — "
            f"{_money(wrapper_cost)} is wrapper turns whose cost is re-read context"
            + (f"; {len(diverging)} agent(s) are pinned cheaper in another repo"
               if diverging else "")
            + "."
        ),
        detail="\n".join(detail),
        table=rows,
        fix=" ".join(fixes),
    )


@check
def config_levers(ctx):
    """Settings-level switches that change cost but leave no trace in usage numbers."""
    issues = []
    fixes = []
    settings = ctx.settings
    merged_env = {}
    for name in ("settings.json", "settings.local.json"):
        merged_env.update((settings.get(name) or {}).get("env") or {})

    # 1. No subagent definitions anywhere — user level OR any project. Only
    #    report this when both are genuinely absent: a repo with its own
    #    .claude/agents/ is already configured, and telling that team to create
    #    a user-level directory is wrong.
    if not ctx.agent_defs:
        issues.append(
            "**No subagent definitions found** at `~/.claude/agents/` or in any "
            "repo's `.claude/agents/`. Subagents inherit the main model, so "
            "delegated retrieval costs the same as doing it inline."
        )
        fixes.append(
            "Create `~/.claude/agents/explore.md` with `model: haiku` in the frontmatter "
            "so read-heavy delegation actually lands on a cheap model."
        )

    # 2. MAX_THINKING_TOKENS is ignored by adaptive-reasoning models. Setting it
    #    and assuming thinking is capped is false confidence, not a cap.
    mtt = merged_env.get("MAX_THINKING_TOKENS")
    if mtt and str(mtt) != "0":
        issues.append(
            f"**`MAX_THINKING_TOKENS={mtt}` is not doing what it looks like.** "
            "Adaptive-reasoning models ignore nonzero thinking budgets — only `0` "
            "reliably disables thinking. This setting is very likely a no-op."
        )
        fixes.append(
            "Use effort levels to control reasoning cost on adaptive models; set "
            "`MAX_THINKING_TOKENS=0` only if you genuinely want thinking off."
        )

    # 3. A fallback chain can silently land on a pricier model.
    fb = None
    for name in ("settings.json", "settings.local.json"):
        fb = fb or (settings.get(name) or {}).get("fallbackModel")
    if fb:
        issues.append(
            f"**`fallbackModel` is set to `{fb}`.** Fallbacks fire on overload without "
            "announcing themselves; if the chain lands on a pricier model the extra "
            "spend is invisible."
        )
        fixes.append("Confirm every model in the fallback chain is one you want to pay for.")

    # 4. MCP output cap. Default is 25,000 tokens per tool result, which is a lot
    #    of context for one call to inject.
    cap = merged_env.get("MAX_MCP_OUTPUT_TOKENS")
    if ctx.mcp_tool_count and not cap:
        issues.append(
            f"**{ctx.mcp_tool_count} MCP tools in use with no `MAX_MCP_OUTPUT_TOKENS` cap.** "
            "The default allows up to 25,000 tokens per tool result, and whatever lands "
            "there stays in context for the rest of the session."
        )
        fixes.append(
            "Set `MAX_MCP_OUTPUT_TOKENS` to something like 5000 unless a server genuinely "
            "needs to return more."
        )

    if not issues:
        return None

    return Finding(
        key="config_levers",
        title="Configuration switches worth changing",
        severity="medium",
        savings=0.0,  # real but not separately attributable; avoid double-counting
        summary=f"{len(issues)} configuration issue(s) that affect cost but don't show up in token counts.",
        detail=(
            "These are settings-level findings. They have no separate dollar estimate "
            "because their cost is already counted in the findings above — but they are "
            "often the *mechanism* behind those numbers, and the cheapest things to change.\n\n"
            + "\n\n".join(f"- {i}" for i in issues)
        ),
        table=[],
        fix=" ".join(fixes),
    )


def build_findings(ctx):
    out = []
    for fn in CHECKS:
        try:
            f = fn(ctx)
        except Exception as exc:  # a broken check must not kill the report
            # Surface it loudly. A crashed check previously rendered as a
            # low-severity row at the bottom of the roadmap, which reads like a
            # finding worth ignoring rather than a bug — one shipped that way.
            print(f"warning: check `{fn.__name__}` crashed: {exc}", file=sys.stderr)
            f = Finding(
                key=fn.__name__,
                title=f"⚠️ Check `{fn.__name__}` failed to run",
                severity="high", savings=0.0,
                summary=(
                    f"This check crashed (`{exc}`), so its findings are missing "
                    "from this report. The other findings are unaffected."
                ),
            )
        if f:
            out.append(f)
    return out
