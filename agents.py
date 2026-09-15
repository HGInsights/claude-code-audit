"""Discover subagent definitions and the models they actually run on.

Agents can be defined at two levels:
  - user level:    ~/.claude/agents/*.md
  - project level: <repo>/.claude/agents/*.md

A check that only reads the user level will wrongly tell someone with a
well-configured repo to "create an agents directory". Both levels matter, and
the project level is where most teams put reviewers.

The model an agent runs on comes from its frontmatter `model:` key. Absent that,
it inherits the main conversation's model — which is usually the expensive one.
"""

import glob
import os
import re

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text):
    """Pull simple `key: value` pairs out of a markdown frontmatter block.

    Deliberately not a YAML parser: agent frontmatter is flat, and descriptions
    routinely contain colons and embedded newlines that would need quoting to
    survive a strict parse. Only the first colon on a line is treated as the
    separator, and only keys we care about are read.
    """
    m = FRONTMATTER.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        # A continuation of a wrapped value, not a new key.
        if line[0] in " \t":
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip().strip("\"'")
    return out


class AgentDef:
    def __init__(self, path, scope, meta):
        self.path = path
        self.scope = scope            # "user" | "project"
        self.name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
        self.model = (meta.get("model") or "").strip().lower()
        self.description = meta.get("description") or ""

    @property
    def pinned(self):
        """True if this agent declares its own model rather than inheriting."""
        return bool(self.model)

    @property
    def is_cheap(self):
        return any(t in self.model for t in ("haiku", "sonnet"))

    @property
    def is_premium(self):
        return "opus" in self.model

    @property
    def delegates_externally(self):
        """Does this agent shell out to an external CLI as its primary path?

        A Codex-primary reviewer that falls back to Opus is not "pinned to
        Opus" in any meaningful sense: on the happy path the model does almost
        no generation, and the pin only sets the fallback tier. Recommending a
        downgrade there is wrong — it changes the fallback, not the reviewer.
        """
        text = f"{self.description}".lower()
        return any(w in text for w in ("codex", "external agent", "gemini", "falls back"))

    def __repr__(self):
        return f"<AgentDef {self.name} scope={self.scope} model={self.model or 'inherit'}>"


def _load_dir(d, scope):
    out = []
    for path in sorted(glob.glob(os.path.join(d, "*.md"))):
        try:
            with open(path, errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        meta = _parse_frontmatter(text)
        # An agent definition is identified by its frontmatter. Directories
        # often also hold a README or notes, which are not agents.
        if not meta.get("name") and not meta.get("description"):
            continue
        out.append(AgentDef(path, scope, meta))
    return out


def discover(repo_dirs=None):
    """All agent definitions visible to this user, user level plus each repo.

    `repo_dirs` is an iterable of working directories seen in the transcripts;
    each is walked upward to find a `.claude/agents/` directory, so a worktree
    resolves to whichever config actually applies to it.
    """
    found = {}

    user_dir = os.path.expanduser("~/.claude/agents")
    if os.path.isdir(user_dir):
        for a in _load_dir(user_dir, "user"):
            found[(a.scope, a.name)] = a

    seen_dirs = set()
    for cwd in repo_dirs or []:
        if not cwd:
            continue
        d = cwd
        # Walk up: a worktree's config may live at the repo root above it.
        for _ in range(6):
            candidate = os.path.join(d, ".claude", "agents")
            if candidate not in seen_dirs and os.path.isdir(candidate):
                seen_dirs.add(candidate)
                for a in _load_dir(candidate, "project"):
                    # The same agent name can be pinned differently in different
                    # repos. Key on the definition's path so both survive —
                    # a divergence is worth reporting, not collapsing.
                    found[("project", a.path)] = a
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent

    return list(found.values())


def by_name(defs):
    """Group definitions by agent name -> [AgentDef], for divergence checks."""
    out = {}
    for a in defs:
        out.setdefault(a.name, []).append(a)
    return out


def spawn_stats(sessions):
    """How each subagent type was actually spawned, from the transcripts.

    Returns {agent_type: {"spawns": n, "overrides": Counter(model)}}.
    A definition says what *should* happen; this says what did.
    """
    stats = {}
    for s in sessions:
        for spawn in s.agent_spawns:
            entry = stats.setdefault(
                spawn.subagent_type or "(default)",
                {"spawns": 0, "overrides": {}},
            )
            entry["spawns"] += 1
            key = spawn.model or "(no override)"
            entry["overrides"][key] = entry["overrides"].get(key, 0) + 1
    return stats
