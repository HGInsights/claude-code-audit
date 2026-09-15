# cc-audit

Audits a Claude Code setup for cost efficiency by reading local transcripts, and
writes a ranked markdown report of what to change first.

Built for a specific question: *given an engineer spending thousands a month,
what is the shortest list of changes that actually reduces the bill?*

## Usage

```bash
python3 cc_audit.py                 # ALL repos, last 30 days -> CC-AUDIT.md
python3 cc_audit.py --days 7
python3 cc_audit.py --out me.md
python3 cc_audit.py --json          # machine-readable, for aggregating across a team
```

## Challenging the recommendations (`--challenge`)

The analysis is deterministic: fixed thresholds, no model, no network. That makes
the numbers auditable, but it also means a recommendation can assert something
about your setup that is simply false — "create an explore agent" when you have
forty, or "pin this reviewer to Sonnet" when that pin is a Codex fallback tier.

`--challenge` sends the findings to an LLM whose only job is to **attack** them:

```bash
python3 cc_audit.py --days 30 --challenge
python3 cc_audit.py --show-packet          # see exactly what would be sent
```

When a recommendation is overturned, the corrected one **silently replaces** it.
The report never shows both — a finding that says "do X" and then "actually do Y"
just makes the reader adjudicate a disagreement they have no way to settle. You
get one instruction per finding, either way.

The only thing the pass adds to the report is a **"Don't double-count these"**
section listing findings that are one problem seen at different granularities,
so their savings aren't summed.

**The critic never computes or adjusts a number.** The deterministic pass owns
all arithmetic; the critic only rules on whether a recommendation is supported.
That boundary is what keeps the figures reproducible.

### What gets sent — config, never conversation

The packet is **~4K tokens of configuration and counters**. It is built from
structured fields only, so transcripts cannot leak into it by construction:

| Sent | Never sent |
|---|---|
| Agent names, scopes, pinned models, descriptions | Prompts, replies, thinking |
| How agents were spawned, and with what override | Tool results, file contents, diffs |
| Settings/env var **names** (values redacted) | File paths, shell commands, URLs |
| Token counts, costs, ratios, severities | Repo names, session IDs |

Agent *descriptions* are included deliberately — that is where "this reviewer
uses Codex first" is written, and without it the critic cannot tell a pricing
choice from a fallback tier.

A regex guard scans the machine-derived fields before anything is sent and
**aborts the run** if a path, source filename, URL, or shell command appears.
Use `--show-packet` to inspect the payload yourself; it prints and exits without
contacting anything.

Runs through the already-authenticated `claude` CLI, so there is no API key to
manage. If the CLI is missing or fails, the run warns and the deterministic
report is written unchanged.

## Stored reports

Every run saves a dated copy under `~/.claude/cc-audit-reports/<user-id>/`, so
learnings accumulate as more people run it:

```text
~/.claude/cc-audit-reports/
  alice-b73b/
    2026-01-15.md      full report — paths, commands, session IDs
    2026-01-15.json    sanitized metrics — safe to pool
    latest.md -> 2026-01-15.md
```

The user ID is `<os-username>-<4-char machine hash>`; the hash distinguishes two
laptops without embedding a hostname. One report per user per day — re-running
overwrites that day.

**The two files exist for different audiences.** The markdown is for the person
who ran it and keeps every detail that made a finding actionable. The JSON is
what you collect centrally, and it carries **no file paths, no shell commands,
no repo names, and no free text** — summaries and fixes are dropped precisely
because they quote real paths. Pooling the JSON cannot leak anyone's work.

```bash
python3 cc_audit.py --trend              # your reports over time
python3 cc_audit.py --trend --all-users  # everyone under the report dir
python3 cc_audit.py --no-store           # don't save this run
python3 cc_audit.py --report-dir /shared/audits   # collect somewhere central
```

`--trend` also tallies which findings recur across reports, which is the point
of keeping a history: it shows whether a fix actually moved the number, and
which problems are common enough to be worth solving org-wide rather than
one engineer at a time.

To gather reports centrally, have people point `--report-dir` at a synced
folder, or collect the `*.json` files — they are designed to be concatenated:

```python
import store
rows = store.history(root="/shared/audits")   # every user, oldest first
```

## Excluding personal repos

Every repo is analysed by default. To leave personal work out of a report you're
going to share, first see what's there:

```bash
python3 cc_audit.py --list-repos
```

Then exclude by repo name, a repo-name glob, or a path:

```bash
python3 cc_audit.py --exclude side-project --exclude 'hobby-*'
python3 cc_audit.py --exclude '/Users/me/personal/*'
```

To set it once instead of retyping, create `~/.claude/cc-audit.json`:

```json
{ "exclude": ["side-project", "/Users/me/personal/*"] }
```

Config and `--exclude` flags combine; `--no-config` ignores the file for one run.

Matching rules, which are deliberately narrow to avoid dropping work by accident:

- A **bare name** matches the repo only — never an arbitrary path segment. A
  worktree can share a name with an unrelated repo (`work-repo-worktrees/website`
  vs a standalone `website`), and excluding the latter must not silently drop the
  former.
- **Worktrees follow their parent repo**, so `--exclude myrepo` also excludes
  every `myrepo-worktrees/*` checkout.
- A pattern containing a **slash** matches the working directory tree.

Exclusions are disclosed in the report and in the JSON output, so a reader can
tell the figures are partial rather than assuming they cover everything.

Python 3.9+, standard library only. No install and no dependencies — it reads
`~/.claude/projects/**/*.jsonl` and writes one file.

By default nothing leaves the machine, which matters because transcripts contain
source code. The one exception is the opt-in `--challenge` pass, which sends a
config-only evidence packet to an LLM; see [above](#challenging-the-recommendations---challenge)
for exactly what that packet may contain and the gate that enforces it.

The Python floor is verified in CI, not assumed: the suite runs on 3.9 and 3.13,
on Linux and macOS.

## Rolling it out across a team

The tool is self-serve by design: transcripts live on each engineer's laptop, so
each person runs it themselves.

```bash
# each engineer runs:
python3 cc_audit.py --days 30 --json > "$USER-audit.json"
```

The JSON output carries totals and per-finding savings without any prompt or
code content, so it is safe to share for aggregation. The markdown report
contains directory names and session IDs — review before circulating widely.

## What it checks

| Check | What it catches |
|---|---|
| **Runaway sessions** | Sessions grown past 200K context, where every turn re-reads the entire history |
| **Premium model on retrieval** | High-input/low-output Opus calls — reading and grepping on the most expensive model |
| **Subagent delegation** | Whether read-heavy work is delegated, and whether those subagents run on cheap models |
| **Session hygiene** | Context growth between 100K and 200K, compaction events |
| **Cache hit rate** | Prefix churn — the cache being rebuilt rather than reused |
| **Cache TTL** | Paying the 2.0x 1-hour write premium when turn gaps don't justify it |
| **Worktree fragmentation** | Cold cache starts multiplied across many working directories |
| **Context bloat** | Baseline prefix size — CLAUDE.md, MCP tool schemas, system prompt |
| **Tool output waste** | Oversized tool results that stay in context and are re-read every turn |
| **Subagent model pinning** | Which subagents run on a premium model, agents pinned cheaper in one repo than another, and built-ins spawned with no override |
| **Config switches** | No subagent definitions anywhere, no-op `MAX_THINKING_TOKENS`, fallback chains, uncapped MCP output |

### Agent definitions are read at both levels

Subagents can be defined in `~/.claude/agents/` **or** in a repo's
`.claude/agents/`. The checks read both, and walk up from each working directory
so a worktree resolves to whichever config applies to it.

This matters: a check that only reads the user level will tell a team with a
well-configured repo to "create an agents directory", which is wrong and gets
the whole report dismissed. Built-in agents (`Explore`, `Plan`,
`general-purpose`) have no definition file by design, so their absence is never
reported as a misconfiguration — what's checked for those is whether spawns pass
a cheap `model` override.

The most useful signal here is **divergence**: when the same agent is pinned to
Opus in one repo and Sonnet in another, one team has already decided the cheaper
model does that job well enough, which makes the recommendation evidence-based
rather than speculative.

The report also benchmarks cost per active day against Anthropic's published
figures (~$13/developer/active day; 90% of users under $30/active day), so the
headline number is interpretable rather than merely large.

## How the cost model works

Each `assistant` record in a transcript is one billed API call carrying a
`usage` block. Costs are computed per call from published rates:

| Component | Multiplier on base input price |
|---|---|
| Cache read | 0.10x |
| Cache write, 5-minute TTL | 1.25x |
| Cache write, 1-hour TTL | 2.00x |
| Uncached input | 1.00x |

Rates live in `pricing.py` — update `FAMILY_PRICES` when Anthropic publishes new
ones and every estimate follows.

### Deduplication

Claude Code forks a transcript into a new file whenever a session is resumed or
branched, replaying earlier history into each new file. On a real machine this
meant **40,548 of 82,841 assistant records were replays** — naively summing them
overstated spend by roughly 2x.

Billing happens once per `requestId`, so that is the unit of truth: the first
occurrence of each request wins and the rest are dropped. Files sharing a
`sessionId` are merged into one session.

## Reading the numbers

Savings estimates are **directional, not invoices**. Every finding shows its
arithmetic so you can check the reasoning rather than trust the total. They are
deliberately conservative — each applies a discount factor for work that is
genuinely necessary rather than assuming all of it is waste.

Two caveats worth stating plainly:

- **Costs are API-equivalent.** If usage is covered by a subscription, the
  dollar figures represent relative cost, not an amount billed. They are still
  the right way to rank what to fix.
- **Findings are scoped to avoid double-counting**, but they are not fully
  independent — fixing runaway sessions also reduces the retrieval-on-Opus
  number, since both are consequences of oversized context. Treat the total as
  an upper bound on the combined effect, not a sum of separate wins.

## The one-line version

In practice the ranking is stable across setups: **context size dominates
everything**. A session at 800K tokens pays ~$1.20 per turn on Opus before doing
any work. Clearing at task boundaries and pushing file reading into cheap
subagents is worth more than every cache tweak combined.

## Development

The tool ships with no dependencies; `pytest` is needed only to run the tests.

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest
```

What the suite covers, and why those parts:

| Area | Why it is tested |
|---|---|
| `test_pricing.py` | Cost arithmetic and cache multipliers, pinned to hand-computed figures. Every number in every report derives from these. |
| `test_parse.py` | Transcript parsing against generated fixtures, including malformed, truncated and replayed lines, and the request-id dedup that stops spend being double-counted. |
| `test_redaction.py` | Adversarial proof of the two sharing boundaries, using hostile fixtures: a repo named like a path, a prompt carrying an API key, unicode lookalikes. |
| `test_checks.py` | The checks that produce a savings figure, plus invariants across every check (no negative savings, none exceeding the spend analysed). |
| `test_grade.py` | That the grade's components are computed from disjoint token pools, so summing them does not double-count. |
| `test_store.py` | Dated storage, `latest` symlinks, and tolerance of a corrupt stored report. |
| `test_cli.py` | Full subprocess runs: exit codes, an absent and an empty `~/.claude`, and the `--challenge` gate refusing a leaky packet. |

`scripts/check_stdlib_only.py` fails the build if any shipped module gains a
third-party import, since the zero-dependency property is what lets this be
copied onto a machine and run with the system Python.

### Keeping pricing current

`pricing.py` carries a `PRICES_AS_OF` date that every report prints. The table
is trusted for `STALENESS_WARNING_DAYS` (180); past that, reports carry a
warning telling the reader to re-verify before acting on a figure. When you
check the published rates, bump `PRICES_AS_OF` even if nothing moved — "verified
unchanged" is the useful signal.

Import-time sanity bounds reject a price from an older generation (Claude 3
Opus at $15/Mtok would raise rather than silently treble every figure).
