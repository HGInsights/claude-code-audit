Create a pull request for the current branch.

Steps:

1. Read `.github/pull_request_template.md` if it exists (this repo does not have one — write a
   concise summary + test plan body instead).
2. Run `git log main..HEAD --oneline` to get commit history.
3. Run `git diff main..HEAD --stat` to get the changed files summary.
4. Fill in each section of the PR template using the git context (or write
   `## Summary` / `## Test plan` sections if there is no template).
5. Create the PR with `gh pr create --title "..." --body "..."`.

IMPORTANT: `gh pr create --body` overrides GitHub's template auto-fill.
You MUST read and reproduce the template manually in the `--body` argument.
