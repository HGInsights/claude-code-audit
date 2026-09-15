#!/usr/bin/env python3
"""cc-audit — audit a Claude Code setup for cost efficiency.

Reads local transcripts from ~/.claude/projects, works out where the money went,
and writes a ranked, actionable markdown report.

    python3 cc_audit.py                    # last 30 days -> CC-AUDIT.md
    python3 cc_audit.py --days 7
    python3 cc_audit.py --out report.md
    python3 cc_audit.py --json             # machine-readable, for aggregating

Standard library only. Nothing leaves the machine.
"""

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents  # noqa: E402
import critic  # noqa: E402
import evidence  # noqa: E402
import grade as grade_mod  # noqa: E402
import store  # noqa: E402
from checks import _money, _tokens, build_findings  # noqa: E402
from parse import find_transcripts, load_sessions, parse_session, repo_of  # noqa: E402
from pricing import PRICES_AS_OF, is_stale, staleness_note  # noqa: E402

# Exclusions can live here so they don't have to be retyped on every run.
CONFIG_PATH = os.path.expanduser("~/.claude/cc-audit.json")

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "ok": 3}
SEVERITY_LABEL = {
    "high": "🔴 High",
    "medium": "🟠 Medium",
    "low": "🟡 Low",
    "ok": "🟢 Healthy",
}


class Context:
    """Everything the checks need, computed once."""

    def __init__(self, sessions, settings):
        self.sessions = sessions
        self.calls = [c for s in sessions for c in s.calls]
        self.settings = settings
        self.total_cost = sum(c.cost for c in self.calls)
        self.compactions = sum(s.compactions for s in sessions)
        self.mcp_tool_count = settings.get("mcp_tool_count", 0)
        self.exclude_patterns = []
        self.excluded_files = 0

        # Subagent definitions, read at BOTH user and project level, plus how
        # agents were actually spawned. Declared config and real behaviour can
        # disagree, and the report should reflect what actually ran.
        self.agent_defs = agents.discover({s.cwd for s in sessions})
        self.spawn_stats = agents.spawn_stats(sessions)

        # Waste as a share of this setup's own spend (see grade.py).
        self.grade = grade_mod.compute(self)

    @property
    def has_cheap_explore(self):
        """Is there a retrieval agent pinned to a cheap model, and used?

        Either a definition named like an explorer pinned to haiku/sonnet, or
        Explore spawns that pass a cheap `model` override.
        """
        for a in self.agent_defs:
            if a.is_cheap and any(
                w in a.name.lower() for w in ("explore", "search", "retriev", "read")
            ):
                return True
        for kind, info in self.spawn_stats.items():
            if "explore" not in kind.lower():
                continue
            for model, n in info["overrides"].items():
                if n and ("haiku" in model or "sonnet" in model):
                    return True
        return False

    def active_days(self):
        days = {s.start.date() for s in self.sessions if s.start}
        return len(days) or 1


def load_exclude_config():
    """Exclusion patterns from ~/.claude/cc-audit.json, if present.

    Format: {"exclude": ["side-project", "/Users/me/personal/*"]}
    """
    if not os.path.exists(CONFIG_PATH):
        return []
    try:
        with open(CONFIG_PATH) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {CONFIG_PATH}: {exc}", file=sys.stderr)
        return []
    patterns = data.get("exclude") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    return [str(p) for p in patterns]


def list_repos(root, since_days):
    """Print every repo found, so the user can decide what to exclude."""
    totals = {}
    for path in find_transcripts(root, since_days):
        sess = parse_session(path)
        if not sess:
            continue
        repo = repo_of(sess.cwd)
        entry = totals.setdefault(repo, {"sessions": 0, "cwds": set()})
        entry["sessions"] += 1
        if sess.cwd:
            entry["cwds"].add(sess.cwd)

    if not totals:
        print("No transcripts found.", file=sys.stderr)
        return 1

    print(f"\nRepos found in the last {since_days} days "
          f"(pass any of these to --exclude):\n")
    width = max(len(r) for r in totals)
    for repo, info in sorted(totals.items(), key=lambda kv: -kv[1]["sessions"]):
        wt = len(info["cwds"])
        suffix = f"  ({wt} working dirs)" if wt > 1 else ""
        print(f"  {repo:<{width}}  {info['sessions']:>4} transcript files{suffix}")
    print(f"\nExclude permanently by creating {CONFIG_PATH}:")
    print('  {"exclude": ["personal-repo", "/Users/you/side/*"]}\n')
    return 0


def show_trend(root, uid):
    """Print stored reports over time — the payoff for keeping a history."""
    rows = store.history(root=root, uid=uid)
    if not rows:
        print(f"No stored reports yet under {root or store.DEFAULT_ROOT}.",
              file=sys.stderr)
        print("Run an audit first; each run saves one report per user per day.",
              file=sys.stderr)
        return 1

    multi_user = len({r.get("user_id") for r in rows}) > 1
    print(f"\nStored reports ({len(rows)}):\n")
    header = f"  {'Date':<12} {'User':<18} {'Spend':>10} {'Waste':>10} {'Grade':>6}"
    if not multi_user:
        header = f"  {'Date':<12} {'Spend':>10} {'Waste':>10} {'Grade':>6}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for r in rows:
        m = r.get("metrics") or {}
        per_day = m.get("cost_per_active_day_usd")
        hit = m.get("cache_hit_rate_pct")
        waste = r.get("waste_usd")
        pct = r.get("waste_pct")
        cells = [
            f"{r.get('date', '?'):<12}",
            f"{_money(r.get('total_cost_usd') or 0):>10}",
            (f"{_money(waste)} ({pct:.0f}%)".rjust(10) if waste else f"{'—':>10}"),
            f"{(r.get('grade') or '—'):>6}",
        ]
        if multi_user:
            cells.insert(1, f"{r.get('user_id', '?'):<18}")
        print("  " + " ".join(cells))

    # Which findings recur? That is the cross-run learning this store exists for.
    tally = {}
    for r in rows:
        for f in r.get("findings", []):
            key = f.get("title") or f.get("key")
            entry = tally.setdefault(key, {"n": 0, "savings": 0.0})
            entry["n"] += 1
            entry["savings"] += f.get("savings_usd") or 0
    if tally:
        print("\n  Most common findings across these reports:\n")
        for title, info in sorted(tally.items(), key=lambda kv: -kv[1]["savings"])[:8]:
            print(f"    {info['n']:>3}x  {_money(info['savings']):>10}  {title}")
    print()
    return 0


def load_settings():
    """Read local config for signals the transcripts don't carry."""
    out = {}
    home = os.path.expanduser("~/.claude")

    for name in ("settings.json", "settings.local.json"):
        path = os.path.join(home, name)
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    out[name] = json.load(fh)
            except (OSError, json.JSONDecodeError):
                pass

    # MCP servers configured for this user.
    mcp_path = os.path.join(home, ".claude.json")
    servers = {}
    for candidate in (mcp_path, os.path.expanduser("~/.claude.json")):
        if os.path.exists(candidate):
            try:
                with open(candidate) as fh:
                    data = json.load(fh)
                servers.update(data.get("mcpServers") or {})
            except (OSError, json.JSONDecodeError):
                pass
            break
    out["mcp_servers"] = sorted(servers)
    # Custom subagent definitions are the only way to pin a subagent to a cheap
    # model; without them, subagents inherit the main (expensive) model.
    agents_dir = os.path.join(home, "agents")
    out["has_agents_dir"] = os.path.isdir(agents_dir) and bool(os.listdir(agents_dir))
    return out


def count_mcp_tools(sessions):
    """Count distinct MCP tools that appeared in these sessions.

    MCP tool schemas sit in the prompt prefix, so their count is a proxy for how
    much of the baseline context they occupy.
    """
    names = set()
    for s in sessions:
        for c in s.calls:
            for t in c.tools:
                if t and t.startswith("mcp__"):
                    names.add(t)
    return len(names)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def md_table(rows):
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(str(c) for c in head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for r in body:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def spend_overview(ctx):
    calls = ctx.calls
    by_family = Counter()
    for c in calls:
        by_family[c.family] += c.cost

    rows = [("Model", "Calls", "Input tokens", "Output tokens", "Cost", "Share")]
    fam_calls = Counter(c.family for c in calls)
    for fam, cost in by_family.most_common():
        fam_list = [c for c in calls if c.family == fam]
        rows.append((
            fam,
            f"{fam_calls[fam]:,}",
            _tokens(sum(c.total_input for c in fam_list)),
            _tokens(sum(c.output for c in fam_list)),
            _money(cost),
            f"{100 * cost / ctx.total_cost:.1f}%" if ctx.total_cost else "—",
        ))
    return rows


def render(ctx, findings, days, verdicts=None):
    sessions = ctx.sessions
    calls = ctx.calls
    starts = [s.start for s in sessions if s.start]
    window = ""
    if starts:
        window = f"{min(starts):%Y-%m-%d} → {max(starts):%Y-%m-%d}"

    total_savings = sum(f.savings for f in findings)
    actionable = [f for f in findings if f.severity != "ok"]
    actionable.sort(key=lambda f: (-f.savings, SEVERITY_ORDER[f.severity]))
    healthy = [f for f in findings if f.severity == "ok"]

    out = []
    out.append("# Claude Code cost audit\n")
    out.append(
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} · "
        f"last {days} days · {window}_\n"
    )
    # Every figure below derives from the pricing table, so date it here where
    # the reader cannot miss it rather than in a footnote.
    if is_stale():
        out.append(f"> ⚠️ {staleness_note()}\n")
    else:
        out.append(f"_{staleness_note()}_\n")

    # --- Headline -------------------------------------------------------
    out.append("## Bottom line\n")
    pct = 100 * total_savings / ctx.total_cost if ctx.total_cost else 0
    out.append(
        f"Analysed **{len(sessions):,} sessions** / **{len(calls):,} API calls** "
        f"costing **{_money(ctx.total_cost)}**.\n"
    )
    if total_savings > 0.01:
        out.append(
            f"Estimated recoverable: **{_money(total_savings)} (~{pct:.0f}%)** "
            f"across {len(actionable)} finding(s), ranked below by impact.\n"
        )
    else:
        out.append("No material savings identified — this setup looks efficient.\n")

    # The grade measures waste as a share of THIS setup's own spend. Spend per
    # day is reported as context, never as a benchmark: a developer doing more
    # valuable work spends more, and that is not a defect to correct.
    active = ctx.active_days()
    per_day = ctx.total_cost / active
    g = ctx.grade
    if g:
        out.append(f"## Efficiency grade: {g['grade']}\n")
        out.append(f"**{g['verdict']}**\n")
        out.append(
            f"Of the {_money(g['total_usd'])} spent, about **{_money(g['waste_usd'])} "
            f"({g['waste_pct']:.0f}%)** bought nothing — it paid for context re-read "
            "without adding information, work run on a pricier model than it needed, "
            f"or calls that failed. The other {_money(g['efficient_usd'])} is the cost "
            "of the work itself.\n"
        )
        rows = [("Wasted on", "Cost", "Share of spend")]
        for comp in g["components"]:
            rows.append((
                comp["label"],
                _money(comp["cost"]),
                f"{100 * comp['cost'] / g['total_usd']:.1f}%",
            ))
        out.append(md_table(rows) + "\n")
        out.append(
            "_This grade is scored against your own spend, not against other "
            "developers. Spending more because you are doing more is not waste; "
            "these components are._\n"
        )

    out.append(md_table([
        ("Metric", "Value"),
        ("Sessions", f"{len(sessions):,}"),
        ("Active days", f"{active:,}"),
        ("Cost per active day", _money(per_day)),
        ("API calls", f"{len(calls):,}"),
        ("Total input tokens", _tokens(sum(c.total_input for c in calls))),
        ("Total output tokens", _tokens(sum(c.output for c in calls))),
        ("Total spend", _money(ctx.total_cost)),
        ("Estimated recoverable", f"{_money(total_savings)} ({pct:.0f}%)"),
    ]) + "\n")

    # --- Priority list --------------------------------------------------
    if actionable:
        out.append("## Roadmap — highest impact first\n")
        rows = [("#", "Change", "Severity", "Est. saving")]
        for i, f in enumerate(actionable, 1):
            rows.append((i, f.title, SEVERITY_LABEL[f.severity], _money(f.savings)))
        out.append(md_table(rows) + "\n")

    # --- Spend breakdown ------------------------------------------------
    if ctx.exclude_patterns:
        out.append(
            f"> **Scope:** {ctx.excluded_files} transcript file(s) excluded by "
            f"`{'`, `'.join(ctx.exclude_patterns)}`. Every figure below is net of "
            "those exclusions.\n"
        )

    out.append("## Where the money goes\n")
    out.append(md_table(spend_overview(ctx)) + "\n")

    by_repo = {}
    for s in sessions:
        entry = by_repo.setdefault(s.repo, {"cost": 0.0, "sessions": 0})
        entry["cost"] += s.cost
        entry["sessions"] += 1
    if len(by_repo) > 1:
        out.append("### By repo\n")
        rows = [("Repo", "Sessions", "Cost", "Share")]
        for repo, info in sorted(by_repo.items(), key=lambda kv: -kv[1]["cost"])[:12]:
            rows.append((
                repo,
                f"{info['sessions']:,}",
                _money(info["cost"]),
                f"{100 * info['cost'] / ctx.total_cost:.1f}%" if ctx.total_cost else "—",
            ))
        out.append(md_table(rows) + "\n")

    top = sorted(sessions, key=lambda s: -s.cost)[:10]
    if top:
        out.append("### Most expensive sessions\n")
        rows = [("Session", "Date", "Directory", "Turns", "Peak context", "Cost")]
        for s in top:
            d = (s.cwd or "?").split("/")[-1]
            rows.append((
                (s.session_id or "?")[:8],
                f"{s.start:%Y-%m-%d}" if s.start else "?",
                d,
                f"{len(s.main_calls):,}",
                _tokens(s.peak_context()),
                _money(s.cost),
            ))
        out.append(md_table(rows) + "\n")

    # --- Critic verdicts ------------------------------------------------
    by_key = {}
    if verdicts:
        for v in verdicts.get("verdicts", []):
            by_key[v.get("finding_key")] = v

        # Only surface what changes the reader's decision: which findings are
        # the same lever (so their savings can't be added up). The per-finding
        # verdicts are applied silently above, not narrated here.
        shared = verdicts.get("shared_root_causes") or []
        if shared:
            out.append("## Don't double-count these\n")
            out.append(
                "Some findings describe one underlying problem at different "
                "granularities. Fixing the root cause captures all of them — "
                "adding their savings together overstates the total.\n"
            )
            title_of = {f.key: f.title for f in findings}
            for group in shared:
                keys = group.get("finding_keys", [])
                names = ", ".join(f"**{title_of.get(k, k)}**" for k in keys)
                out.append(f"- {names} — {group.get('why', '')}")
            out.append("")

    # --- Findings -------------------------------------------------------
    out.append("## Findings\n")
    for i, f in enumerate(actionable, 1):
        out.append(f"### {i}. {f.title} — {SEVERITY_LABEL[f.severity]}\n")
        out.append(f"**{f.summary}**\n")
        if f.savings > 0.01:
            out.append(f"> Estimated saving: **{_money(f.savings)}**\n")
        if f.detail:
            out.append(f.detail + "\n")
        if f.table:
            out.append(md_table(f.table) + "\n")
        # When the critic overturned a recommendation, show only the corrected
        # one. Printing the original next to a "revised" version doubles the
        # reading and makes the reader adjudicate a disagreement they have no
        # way to settle.
        v = by_key.get(f.key)
        revised = (v or {}).get("revised_recommendation")
        if revised and v.get("verdict") != "supported":
            out.append(f"**Fix:** {revised}\n")
        elif f.fix:
            out.append(f"**Fix:** {f.fix}\n")

    if healthy:
        out.append("## Already healthy\n")
        for f in healthy:
            out.append(f"- **{f.title}** — {f.summary}")
        out.append("")

    out.append("## Method\n")
    out.append(
        "Parsed from local `~/.claude/projects/**/*.jsonl` transcripts. Each `assistant` "
        "record is one billed API call; costs come from its `usage` block priced at "
        "published per-model rates (cache write 1.25x/2.0x, cache read 0.1x base input).\n\n"
        "Savings estimates are deliberately conservative and each finding shows its "
        "arithmetic. They are directional, not invoices — use them to rank what to change "
        "first, then verify against Console billing.\n"
    )
    return "\n".join(out)


def to_json(ctx, findings, days):
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "user_id": store.user_id(),
        "grade": (ctx.grade or {}).get("grade"),
        "waste_usd": round((ctx.grade or {}).get("waste_usd", 0.0), 2),
        "waste_pct": round((ctx.grade or {}).get("waste_pct", 0.0), 1),
        "window_days": days,
        "pricing_as_of": PRICES_AS_OF,
        "pricing_stale": is_stale(),
        "excluded_patterns": ctx.exclude_patterns,
        "excluded_files": ctx.excluded_files,
        "sessions": len(ctx.sessions),
        "api_calls": len(ctx.calls),
        "total_cost_usd": round(ctx.total_cost, 2),
        "estimated_savings_usd": round(sum(f.savings for f in findings), 2),
        "input_tokens": sum(c.total_input for c in ctx.calls),
        "output_tokens": sum(c.output for c in ctx.calls),
        "findings": [
            {
                "key": f.key,
                "title": f.title,
                "severity": f.severity,
                "savings_usd": round(f.savings, 2),
                "summary": f.summary,
                "fix": f.fix,
            }
            for f in sorted(findings, key=lambda f: -f.savings)
        ],
    }


def main():
    ap = argparse.ArgumentParser(
        prog="cc-audit",
        description="Audit a Claude Code setup for cost efficiency.",
    )
    ap.add_argument("--days", type=int, default=30,
                    help="how far back to analyse (default: 30)")
    ap.add_argument("--out", default="CC-AUDIT.md",
                    help="markdown report path (default: CC-AUDIT.md)")
    ap.add_argument("--json", action="store_true",
                    help="also print machine-readable JSON to stdout")
    ap.add_argument("--root", default=None,
                    help="transcript root (default: ~/.claude/projects)")
    ap.add_argument("--limit", type=int, default=None,
                    help="max sessions to analyse (for a quick look)")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="repo name, path glob, or path segment to leave out "
                         "(repeatable). Worktrees follow their parent repo.")
    ap.add_argument("--no-config", action="store_true",
                    help=f"ignore exclusions in {CONFIG_PATH}")
    ap.add_argument("--list-repos", action="store_true",
                    help="list the repos found, then exit (use to pick exclusions)")
    ap.add_argument("--report-dir", default=None, metavar="DIR",
                    help=f"where to store dated reports (default: {store.DEFAULT_ROOT})")
    ap.add_argument("--no-store", action="store_true",
                    help="don't save a dated copy of this report")
    ap.add_argument("--challenge", action="store_true",
                    help="ask an LLM to challenge the recommendations against your "
                         "actual config (sends config + counters, never transcripts)")
    ap.add_argument("--challenge-model", default="sonnet", metavar="MODEL",
                    help="model for the critic pass (default: sonnet)")
    ap.add_argument("--show-packet", action="store_true",
                    help="print exactly what --challenge would send, then exit")
    ap.add_argument("--trend", action="store_true",
                    help="show stored reports over time, then exit")
    ap.add_argument("--all-users", action="store_true",
                    help="with --trend, include every user under the report dir")
    args = ap.parse_args()

    if args.trend:
        return show_trend(args.report_dir,
                          None if args.all_users else store.user_id())

    if args.list_repos:
        return list_repos(args.root, args.days)

    # All repos are analysed by default; exclusions come from the config file
    # and the command line combined.
    exclude = list(args.exclude)
    if not args.no_config:
        exclude = load_exclude_config() + exclude

    print(f"Reading transcripts (last {args.days} days)…", file=sys.stderr)
    if exclude:
        print(f"Excluding: {', '.join(exclude)}", file=sys.stderr)
    sessions, excluded_count = load_sessions(
        root=args.root, since_days=args.days, limit=args.limit, exclude=exclude,
    )
    if not sessions:
        if excluded_count:
            print(f"Everything was excluded: {excluded_count} transcript file(s) "
                  f"matched {', '.join(exclude)}. Loosen the exclusions "
                  f"(or use --no-config) to get a report.", file=sys.stderr)
        else:
            print("No transcripts found. Is this the machine you run Claude Code on?",
                  file=sys.stderr)
        return 1

    settings = load_settings()
    settings["mcp_tool_count"] = count_mcp_tools(sessions)
    ctx = Context(sessions, settings)
    ctx.exclude_patterns = exclude
    ctx.excluded_files = excluded_count

    print(f"Analysing {len(sessions):,} sessions / {len(ctx.calls):,} API calls…",
          file=sys.stderr)
    findings = build_findings(ctx)

    payload = to_json(ctx, findings, args.days)

    verdicts = None
    if args.challenge or args.show_packet:
        packet = evidence.build(ctx, findings, store.sanitize(payload, ctx))

        # Hard gate: never send anything that looks like conversation content,
        # even if a future change to the packet builder lets one through.
        leaks = evidence.find_leaks(packet)
        if leaks:
            print(f"error: refusing to send — packet contains {len(leaks)} value(s) "
                  f"that look like file paths or commands: {leaks[:3]}",
                  file=sys.stderr)
            return 2

        if args.show_packet:
            print(json.dumps(packet, indent=2, sort_keys=True))
            return 0

        print(f"Challenging {len(findings)} recommendations "
              f"({len(json.dumps(packet)) // 4:,} tokens)…", file=sys.stderr)
        try:
            verdicts = critic.review(packet, model=args.challenge_model)
        except critic.CriticUnavailable as exc:
            print(f"warning: {exc}", file=sys.stderr)
            print("         the deterministic report below is unaffected.",
                  file=sys.stderr)

    report = render(ctx, findings, args.days, verdicts)
    with open(args.out, "w") as fh:
        fh.write(report)

    stored = None
    if not args.no_store:
        try:
            stored = store.save(report, payload, ctx, root=args.report_dir)
        except OSError as exc:
            print(f"warning: could not store report: {exc}", file=sys.stderr)

    savings = sum(f.savings for f in findings)
    print(f"\n  Spend analysed:  {_money(ctx.total_cost)}", file=sys.stderr)
    print(f"  Recoverable:     {_money(savings)}", file=sys.stderr)
    print(f"  Report:          {args.out}", file=sys.stderr)
    if stored:
        md_path, json_path = stored
        print(f"  Stored:          {md_path}", file=sys.stderr)
        print(f"                   {os.path.basename(json_path)} (sanitized, "
              f"safe to share)", file=sys.stderr)
    print("", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
