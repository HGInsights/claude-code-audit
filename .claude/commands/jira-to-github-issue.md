Transform a Jira task (board prefix `AL`) into well-defined, developer-ready
GitHub issues on `HGInsights/claude-code-audit`.

> **Important:** always pass `--repo HGInsights/claude-code-audit` to `gh`
> commands — the CLI otherwise defaults to whatever remote it infers, which is
> wrong in a fork or a multi-remote clone.

## Progress Tracking

**REQUIRED**: create a task list using `TaskCreate` for each step below, all at
once in a single message with parallel tool calls. Set `in_progress` when
starting a step and `completed` when finishing. Chain them with `addBlockedBy`
so each is blocked by the previous.

1. **Fetch the Jira task (AL-XXXX)** — `activeForm: "Fetching task"`
2. **Reproduce the issue (bugs only)** — `activeForm: "Reproducing the issue"`
3. **Gather repo context (Haiku Explore)** — `activeForm: "Gathering repo context"`
4. **PM-coach gap analysis** — `activeForm: "Analyzing requirements gaps"`
5. **Grill user on requirements** 🔒 — `activeForm: "Grilling user on requirements"`
6. **Draft user stories (incl. ≥1 error/edge)** — `activeForm: "Drafting user stories"`
7. **Draft testable acceptance criteria with `**Story N:**` trace** — `activeForm: "Drafting acceptance criteria"`
8. **User-story + AC sign-off** 🔒 — `activeForm: "Confirming user stories and acceptance criteria"`
9. **Decide issue structure (single vs. multiple)** — `activeForm: "Deciding issue structure"`
10. **Run quality checklist (incl. bidirectional trace)** — `activeForm: "Running quality checklist"`
11. **Create issue(s) with labels** — `activeForm: "Creating GitHub issue(s)"`
12. **Link back to Jira & summarize** — `activeForm: "Linking back to Jira"`

> 🔒 **Hard gates.** Task 5 (requirements grilled & user-confirmed) and task 8
> (user stories + testable acceptance criteria signed off) must each be
> `completed` before you create any issue. Do not call `gh issue create` until
> both gates pass.

Read and follow the detailed instructions in
`.claude/skills/runbooks/jira-to-github-issue.md` to complete each step.

The Jira task to convert (ticket key or URL, e.g. `AL-123`): $ARGUMENTS
