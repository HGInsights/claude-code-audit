# Jira → GitHub Issue

Turn a Jira task (board prefix `AL`) into a GitHub issue a developer — or
`/fix-issue` — can pick up cold.

## When to use

- "create a GitHub issue from AL-123"
- "convert this Jira ticket to a GitHub issue"
- The user pastes an `AL-XXXX` key or Jira URL and wants an issue

## Mandatory task list

The command file that invokes this runbook creates the task list. Do not skip
steps, and do not create an issue before both hard gates pass.

### 🔒 Hard gates — issue creation is BLOCKED until both pass

- **Gate A** (Step 3): requirements grilled, every material gap resolved or
  consciously accepted by the user.
- **Gate B** (Step 5): user stories + acceptance criteria explicitly signed off.

Neither gate is satisfiable by your own judgement. Both require the user to
answer.

## Step 1: Fetch the Jira task

```bash
jira issue view AL-123 --plain
jira issue list --parent AL-123 --plain 2>/dev/null   # subtasks
```

No `jira` CLI available? Use the Atlassian MCP tools (`getJiraIssue`,
`searchJiraIssuesUsingJql`), or ask the user to paste the ticket content.

Come away with:

1. Title, description, and type (bug / feature / chore)
2. Any subtasks or linked items
3. Any linked spec, design doc, or thread — read those too
4. The ticket key (`AL-XXXX`), for the link-back in Step 8

> Treat the ticket as **the starting point, not the source of truth** — see
> the warning in Step 5.

## Step 1b: Reproduce the issue (bugs — standing instruction)

**Always reproduce before writing anything.** For any bug-type task, replicate
the problem first so the issue carries a confirmed root cause and exact code
citations, not a restatement of the reporter's words. This is not optional.

Reproduce at the most concrete level available:

- **Code-level** (default here — cc-audit is a deterministic CLI that parses
  transcripts and computes cost/grade metrics): trace the path from where the
  symptom surfaces back to the defective line in `cc_audit.py`, `parse.py`,
  `checks.py`, `grade.py`, `pricing.py`, `evidence.py`, `store.py`, or
  `agents.py`. Pin the precise `file:line`, the current wrong code, and
  confirm the data a correct fix needs is actually available upstream.
- **Live**: run the CLI against a real transcript fixture (see `tests/`) and
  capture the concrete evidence — the command, the wrong output, the error.

Record for the issue body:

1. The exact trigger condition (e.g. a transcript shape, CLI flag combo)
2. The observed wrong behavior, with evidence (`file:line`, error, output)
3. The root cause — *why*, not just *that*
4. Which adjacent code is already correct and must NOT change

If you genuinely cannot reproduce (non-deterministic, environment-specific, no
access), say so explicitly during the grilling step and ask for repro steps or
evidence. **Do not paper over an unreproduced bug with a guessed root cause.**

For feature requests, this step becomes "confirm the current behavior or gap
the feature addresses."

## Step 2: Gather repo context (delegate to Haiku)

Gathering context is open-ended — scanning the repo root, `tests/`, `scripts/`,
and prior issues for anything related. Do this via a single `Explore` subagent
pinned to **Haiku**, not inline `grep` / `gh issue list` in the main session.

```
Agent({
  subagent_type: "Explore",
  model: "haiku",
  description: "Find related context",
  prompt: "Thoroughness: quick. The task is: <one-paragraph summary including key terms, affected modules, and any referenced CLI commands or data files>.

Return a concise report with four sections:
1. **Related docs** — matching sections in README.md. One-line summary each.
2. **Related tests** — matching files under `tests/`. Path + one-line summary.
3. **Overlapping GitHub issues** — up to 5 on HGInsights/claude-code-audit (open or closed) whose title or body clearly overlaps. Number, title, state, why relevant.
4. **Relevant code anchors** — up to 5 files/symbols at the repo root (cc_audit.py, parse.py, checks.py, grade.py, pricing.py, evidence.py, store.py, agents.py) the work would touch. `path:line` with a one-line reason.

If a category has no matches, say 'None found' — don't pad."
})
```

Read the report and the files it cites. **Do not** run `grep -r` or
`gh issue list` inline — that is the pattern this step replaces.

> Step 3 stays in the main context — it is the reasoning-heavy part.

## Step 3: PM-coach gap analysis

Evaluate the task through a PM lens before writing anything. Common gaps:

**Problem definition** — Is the "why" clear (what user pain or business need)?
Is scope bounded (can a developer tell what's in vs. out)? Are assumptions
stated?

**Success criteria** — Is "done" measurable; can you write a test for each
criterion? Are edge cases identified (empty result, unknown input, missing
data)? Is the happy path distinct from error paths?

**User stories** — Who benefits; are the actors/personas identified? Is the
flow described step by step? Are multiple consumers affected?

**Technical clarity** — Are interfaces defined (CLI flags, output shapes,
fields)? Are dependencies explicit (what must exist first)? Is the testing
strategy clear (unit vs. fixture-based)?

Present the analysis with `AskUserQuestion`: what is well-defined, what gaps
you found, and your suggested answers.

### 🔒 Gate A — grill until requirements are resolved

Mandatory. You may **not** advance past Step 3 by silently filling gaps with
your own assumptions.

1. Present gaps as a numbered list, each with your recommended answer so the
   user has something concrete to react to:

   ```
   Before I write the issue, I found gaps that need your call:

   1. **Edge case not covered**: what does the CLI do for a malformed
      transcript line? I'd suggest skipping it with a warning rather than
      aborting the whole run.
   2. **Success criteria vague**: "should be fast" — I'd propose the audit
      completes in under N seconds for a typical transcript.
   3. **Missing dependency**: this seems to need <X> — is that available?
   4. **Persona unclear**: who runs this — an individual dev auditing their
      own sessions, or a team lead auditing others'?

   For each: accept my recommendation, or give me the right answer.
   ```

2. **Loop, don't one-shot.** If an answer opens a new ambiguity or only
   partially resolves a gap, ask the follow-up. Keep going until every
   material gap is resolved or consciously accepted.

3. Only then mark the gate complete.

Even when the task looks well-defined, surface your read of the requirements
and personas and get an explicit "yes, proceed." Do not skip the gate.

## Step 4: Decide issue structure

**Single issue** if focused and completable in one PR. **Multiple** if large
and separable into independent, shippable increments — then identify
boundaries, dependencies (what must come first), labels, and sizes.

Recommend, and get confirmation before creating.

## Step 5: Write the issue(s)

### ⚠️ The ticket is the starting point, not the source of truth

The issue must be **fully self-contained**. A developer picking it up cold —
no Jira access, no access to this conversation, none of your exploration —
must have everything needed to implement it correctly. If they would have to
re-derive something you already found (file paths, line numbers, the offending
logic, existing patterns to follow, what is already correct and must not
change), the issue is incomplete.

### Body structure

`## Context` · `## Problem` · `## Fix — exact changes required` (bugs) ·
`## Requirements` · `## User Stories` · `## Acceptance Criteria` ·
`## Technical Notes` · `## Dependencies` · `## Size: [Small/Medium/Large]`

Include a link back to the Jira ticket (`AL-XXXX`) under `## Context`.

### User stories — end-user altitude, NOT restated unit tests

Keep the literal `### Story <N>: <title>` form — downstream parsers
(`/fix-issue` pre-flight, the code reviewer's User Story Verification) match on
it.

A story describes **an actor interacting with the system and the outcome they
experience**. It must be implementation-agnostic — still correct even if the
internal design changes completely.

**The test:** if a story reads like the name of a `test_*` function, or
mentions a function, kwarg, `file:line`, or an internal implementation detail
— it is written too low. Rewrite as **persona + intent + observable outcome**.
All mechanical detail belongs in the acceptance criteria.

- ❌ Too low: "The `grade()` function's threshold constant is moved to
  `pricing.py`."
- ✅ Right altitude: "As someone auditing a session, when my transcript has an
  unusually high spend, I see a grade that reflects that consistently no
  matter which module computed it."

Include **at least one error/edge-case story**, not just happy paths.

### Acceptance criteria — bidirectional trace

Every criterion references at least one story, and every story is referenced
by at least one criterion. Use a bold `**Story N:**` prefix so the trace is
visible at a glance.

```markdown
- [ ] **Story 1 (happy path):** [testable criterion]
- [ ] **Story 1 (edge case):** [testable criterion]
- [ ] **Story 2 (error case):** [testable criterion]
- [ ] **Story N (tests):** [specific test types required]
```

### 🔒 Gate B — story + AC sign-off

Blocks `gh issue create`. Before Step 6:

1. Verify **at least one concrete user story** plus **at least one error/edge
   story**, each using the literal heading form.
2. Verify every criterion is testable and carries the `**Story N:**` prefix,
   and every story is covered by at least one criterion — no orphans in either
   direction.
3. Show the user the drafted stories and criteria and ask for explicit
   sign-off: *"Do these capture it? Anything to add, remove, or sharpen before
   I create it?"*
4. Wait for an explicit yes. If changes are requested, revise and re-confirm.

If the work is split across multiple issues, **each** must clear this gate.

### Quality checklist — apply to every issue before creating

1. Could a developer (or `/fix-issue`) start on this today? If not, what's
   missing?
2. Could two developers independently implement this and arrive at similar
   solutions? If not, it's too ambiguous.
3. Can every acceptance criterion be verified with a test (see `tests/`)?
4. Are there implied requirements not written down? Make them explicit.
5. Does the issue stand alone — no need to visit Jira, this conversation, or
   any exploration output?
6. **Bugs:** is the exact code that must change cited with file path and line
   numbers? "Fix the parser" is not enough. Show before/after for every line,
   and state what adjacent code must NOT change.
7. **Bugs:** is concrete evidence included — the error, the trigger, the
   observed behavior? The implementer shouldn't need to reproduce it to
   understand it.
8. Is the test pattern spelled out — test file under `tests/`, pattern to
   follow, minimum cases?
9. Is the story↔AC trace bidirectional? Orphans in either direction fail.
10. Did both hard gates pass? If either is unmet, stop.

## Step 6: Labels

Use this repo's existing labels: `bug`, `documentation`, `enhancement`,
`good first issue`, `help wanted`, `question`. Pick the ones that fit; don't
invent new labels without asking.

## Step 7: Create the issue(s)

```bash
gh issue create --repo HGInsights/claude-code-audit \
  --title "<title>" \
  --body-file <(cat <<'BODY'
<issue body>
BODY
) \
  --label "<labels>"
```

Create in dependency order when there are several, so earlier issues can be
referenced by number in later ones.

## Step 8: Link back and confirm

Comment the GitHub issue URL(s) back on the Jira ticket (`AL-XXXX`), then
summarize for the user: issue number(s), title(s), labels, size, and the
dependency order.

## Error handling

- **`jira` CLI unavailable** — fall back to Atlassian MCP tools, then to
  asking the user to paste the content. Do not invent task details.
- **`gh` not authenticated** — surface `gh auth status` and stop; do not
  attempt a partial create.
- **Task too vague** — that is what Gate A is for. Grill, don't guess.
- **Task too large for one issue** — Step 4; propose a split with explicit
  dependencies.
