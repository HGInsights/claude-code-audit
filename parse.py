"""Parse ~/.claude/projects/**/*.jsonl transcripts into an analysable model.

One JSONL file == one session. Each `assistant` record is one billed API call
and carries a `usage` block with the cache/token breakdown we need.

Nothing here interprets the data — that's the checks' job. This module only
normalises it.
"""

import fnmatch
import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pricing import cost_of, cost_without_cache, family_of


def _ts(value):
    """Parse an ISO timestamp into an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@dataclass
class Call:
    """One billed assistant API call."""

    request_id: str
    ts: datetime
    model: str
    family: str
    raw_input: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    output: int
    is_sidechain: bool
    tools: list = field(default_factory=list)
    skill: str = None

    @property
    def total_input(self):
        return self.raw_input + self.cache_write_5m + self.cache_write_1h + self.cache_read

    @property
    def cache_write(self):
        return self.cache_write_5m + self.cache_write_1h

    @property
    def cost(self):
        return cost_of(
            self.model, self.raw_input, self.cache_write_5m,
            self.cache_write_1h, self.cache_read, self.output,
        )

    @property
    def cost_uncached(self):
        return cost_without_cache(
            self.model, self.raw_input, self.cache_write_5m,
            self.cache_write_1h, self.cache_read, self.output,
        )


@dataclass
class ToolResult:
    """A tool result that landed in context, with its size and what produced it.

    `target` is the file path (Read/Grep) or command (Bash) behind the result.
    Without it a report can only say "you read too much"; with it, the report can
    name the file to stop reading.
    """

    name: str
    chars: int
    is_error: bool
    target: str = ""
    repo: str = ""
    is_sidechain: bool = False
    error_text: str = ""   # first line of the error, for classification

    IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")

    @property
    def is_image(self):
        return self.target.lower().endswith(self.IMAGE_EXT)

    @property
    def error_kind(self):
        """Classify a failure by cause, since each has a different fix."""
        if not self.is_error:
            return ""
        t = (self.error_text or "").lower()
        if "has not been read yet" in t:
            return "edit_before_read"
        if "string to replace not found" in t or "not unique" in t:
            return "stale_edit"
        if "does not exist" in t or "no such file" in t or "eisdir" in t:
            return "bad_path"
        if "permission" in t or "denied" in t or "not allowed" in t:
            return "permission_denied"
        if "exit code 127" in t or "command not found" in t:
            return "missing_command"
        if "timed out" in t or "timeout" in t:
            return "timeout"
        if "exit code" in t:
            return "shell_failure"
        return "other"

    @property
    def kind(self):
        """Coarse bucket used to pick the right remediation."""
        t = self.target.lower()
        if self.is_image:
            return "image"
        if "/skills/" in t or "/docs/dev/" in t or t.endswith("claude.md"):
            return "instructions"
        if any(seg in t for seg in ("/node_modules/", "/dist/", "/build/", "/.next/",
                                    "test-results", "/coverage/")):
            return "generated"
        # Lockfiles are the single biggest generated-file context sink, and
        # they are named every which way: `yarn.lock`, `package-lock.json`,
        # `pnpm-lock.yaml`, `Cargo.lock`, `poetry.lock`. A `.lock` substring
        # test catches only the ones where it is the extension, so match the
        # basename instead.
        base = t.rsplit("/", 1)[-1]
        if base.endswith(".lock") or "-lock." in base or base.endswith(".lockb"):
            return "generated"
        if self.name == "Bash":
            return "command"
        return "file"


def repo_of(cwd):
    """Best-effort repo name for a working directory.

    Worktrees are collapsed onto their parent repo so that excluding a repo also
    excludes its worktrees: `.../myrepo-worktrees/feature1` -> `myrepo`. Without
    this, a user excluding a personal repo would still leak its worktrees.
    """
    if not cwd:
        return "unknown"
    parts = [p for p in cwd.rstrip("/").split("/") if p]
    if not parts:
        return "unknown"

    for i, part in enumerate(parts):
        # `<repo>-worktrees/<branch>` and `<repo>.worktrees/<branch>` layouts.
        for marker in ("-worktrees", ".worktrees", "_worktrees"):
            if part.endswith(marker):
                return part[: -len(marker)]
        # A plain `worktrees/` dir: the repo is the segment before it.
        if part == "worktrees" and i > 0:
            return parts[i - 1]

    return parts[-1]


@dataclass
class AgentSpawn:
    """One `Agent` tool call: which subagent, and any per-call model override."""

    subagent_type: str
    model: str
    repo: str = ""


@dataclass
class Session:
    path: str
    session_id: str = None
    cwd: str = None
    git_branch: str = None
    version: str = None
    calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    agent_spawns: list = field(default_factory=list)
    _spawn_index: dict = field(default_factory=dict)  # tool_use_id -> AgentSpawn
    user_turns: int = 0
    compactions: int = 0
    skills_used: set = field(default_factory=set)

    @property
    def repo(self):
        return repo_of(self.cwd)

    @property
    def cost(self):
        return sum(c.cost for c in self.calls)

    @property
    def cost_uncached(self):
        return sum(c.cost_uncached for c in self.calls)

    @property
    def start(self):
        stamps = [c.ts for c in self.calls if c.ts]
        return min(stamps) if stamps else None

    @property
    def end(self):
        stamps = [c.ts for c in self.calls if c.ts]
        return max(stamps) if stamps else None

    @property
    def main_calls(self):
        return [c for c in self.calls if not c.is_sidechain]

    @property
    def sidechain_calls(self):
        return [c for c in self.calls if c.is_sidechain]

    def peak_context(self):
        """Largest total input on any single main-thread call.

        This is the high-water mark of the context window in this session.
        """
        return max((c.total_input for c in self.main_calls), default=0)


def _result_text(part):
    content = part.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content)


def parse_session(path):
    """Read one transcript file into a Session. Malformed lines are skipped."""
    s = Session(path=path)
    pending_targets = {}  # tool_use_id -> (tool name, target)
    spawn_ids = {}        # tool_use_id -> AgentSpawn (deduped within the file)
    try:
        fh = open(path, errors="replace")
    except OSError:
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            # A line can be valid JSON but not an object (`[]`, `"x"`, `null`).
            # Everything below assumes a dict, so skip anything else.
            if not isinstance(d, dict):
                continue

            s.session_id = s.session_id or d.get("sessionId")
            s.cwd = s.cwd or d.get("cwd")
            s.git_branch = s.git_branch or d.get("gitBranch")
            s.version = s.version or d.get("version")

            kind = d.get("type")

            if kind == "assistant":
                msg = d.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage") or {}
                if not isinstance(usage, dict):
                    continue
                creation = usage.get("cache_creation") or {}
                # `cache_creation` is the per-TTL split; `cache_creation_input_tokens`
                # is the documented total. They agree when both are present, but the
                # split can be absent — in which case attribute the total to the 5m
                # bucket (the default TTL) rather than silently counting zero.
                w5m = creation.get("ephemeral_5m_input_tokens", 0) or 0
                w1h = creation.get("ephemeral_1h_input_tokens", 0) or 0
                declared = usage.get("cache_creation_input_tokens", 0) or 0
                if declared and (w5m + w1h) == 0:
                    w5m = declared
                tools = []
                for p in msg.get("content", []) or []:
                    if not isinstance(p, dict) or p.get("type") != "tool_use":
                        continue
                    tools.append(p.get("name"))
                    # Remember what this call targeted so the matching
                    # tool_result (which doesn't say) can be attributed.
                    inp = p.get("input") or {}
                    if isinstance(inp, dict):
                        target = (
                            inp.get("file_path")
                            or inp.get("pattern")
                            or inp.get("command")
                            or inp.get("path")
                            or ""
                        )
                        if p.get("id"):
                            pending_targets[p["id"]] = (p.get("name"), str(target))
                        # Record how subagents are actually being spawned, so
                        # declared config can be checked against real behaviour.
                        if p.get("name") == "Agent":
                            spawn = AgentSpawn(
                                subagent_type=inp.get("subagent_type") or "",
                                model=(inp.get("model") or "").strip().lower(),
                                repo=repo_of(d.get("cwd") or s.cwd),
                            )
                            # Key on the tool_use id: two identical spawns are a
                            # real pair, but the same id replayed across forked
                            # transcript files is one event.
                            spawn_ids[p.get("id") or id(spawn)] = spawn
                model = msg.get("model") or "unknown"
                if model == "<synthetic>":
                    continue  # not a billed call
                skill = d.get("attributionSkill")
                if skill:
                    s.skills_used.add(skill)
                s.calls.append(Call(
                    request_id=d.get("requestId"),
                    ts=_ts(d.get("timestamp")),
                    model=model,
                    family=family_of(model),
                    raw_input=usage.get("input_tokens", 0) or 0,
                    cache_write_5m=w5m,
                    cache_write_1h=w1h,
                    cache_read=usage.get("cache_read_input_tokens", 0) or 0,
                    output=usage.get("output_tokens", 0) or 0,
                    is_sidechain=bool(d.get("isSidechain")),
                    tools=tools,
                    skill=skill,
                ))

            elif kind == "user":
                if d.get("isCompactSummary"):
                    s.compactions += 1
                if d.get("isMeta"):
                    continue
                content = (d.get("message") or {}).get("content")
                if isinstance(content, str):
                    # A real human turn (or a slash command expansion).
                    s.user_turns += 1
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "tool_result":
                            tool_name, target = pending_targets.get(
                                part.get("tool_use_id"), (None, "")
                            )
                            s.tool_results.append(ToolResult(
                                name=tool_name or _tool_name_for(d),
                                chars=len(_result_text(part)),
                                is_error=bool(part.get("is_error")),
                                # A subagent's own tool results never enter the
                                # parent's context — counting them would credit
                                # delegation with bloat it actually prevented.
                                is_sidechain=bool(d.get("isSidechain")),
                                target=target,
                                repo=repo_of(d.get("cwd") or s.cwd),
                                error_text=(
                                    _result_text(part)[:200]
                                    if part.get("is_error") else ""
                                ),
                            ))
                        elif part.get("type") == "text":
                            s.user_turns += 1

    # Keep ids and spawns as parallel ordered lists so merging can dedupe by id.
    s._spawn_index = dict(spawn_ids)
    s.agent_spawns = list(spawn_ids.values())
    return s if s.calls else None


def _tool_name_for(record):
    """Best-effort tool name for a tool_result record.

    The result record doesn't name the tool, but toolUseResult's shape hints at
    it. This is only used for grouping in the report, so a fallback is fine.
    """
    tur = record.get("toolUseResult")
    if isinstance(tur, dict):
        if "stdout" in tur:
            return "Bash"
        if "matches" in tur:
            return "Search"
        if "file" in tur or "content" in tur:
            return "Read"
        if "task" in tur:
            return "Agent"
    return "other"


def find_transcripts(root=None, since_days=None):
    """All transcript paths under the Claude projects dir, newest first."""
    root = root or os.path.expanduser("~/.claude/projects")
    paths = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    if since_days:
        cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
        paths = [p for p in paths if os.path.getmtime(p) >= cutoff]
    return sorted(paths, key=os.path.getmtime, reverse=True)


def is_excluded(session, patterns):
    """True if this session matches any exclusion pattern.

    A bare name matches the *repo* only — never an arbitrary path segment. That
    distinction matters: a worktree can share a name with an unrelated repo
    (`.../work-repo-worktrees/website` vs `.../website`), and excluding a
    personal repo must not silently drop a work repo's worktree. Repo names
    already fold worktrees into their parent, so `--exclude myrepo` still covers
    every worktree of `myrepo`.

    Patterns containing a slash or glob metacharacter are matched against the
    full working directory instead, for "drop everything under this path".
    """
    if not patterns:
        return False

    cwd = (session.cwd or "").lower()
    repo = (session.repo or "").lower()

    for raw in patterns:
        pat = raw.strip().lower().rstrip("/")
        if not pat:
            continue
        if "/" in pat:
            # Path-shaped: match the directory tree.
            if fnmatch.fnmatch(cwd, pat) or fnmatch.fnmatch(cwd, pat + "/*"):
                return True
        elif any(ch in pat for ch in "*?["):
            # Glob without a slash: a repo-name wildcard (`side-*`).
            if fnmatch.fnmatch(repo, pat):
                return True
        elif pat == repo:
            return True
    return False


def load_sessions(root=None, since_days=None, limit=None, exclude=None):
    """Load and deduplicate sessions.

    Claude Code forks a transcript into a new file whenever a session is
    resumed or branched, replaying the earlier history into each new file. So
    the same billed API call appears in several files, and roughly half of all
    assistant records on disk are replays.

    Billing happens once per `requestId`, so that is the unit of truth: keep the
    first occurrence of each request and drop the rest. Files sharing a
    sessionId are merged into one Session, since they are one conversation.

    `exclude` is a list of glob/name patterns; matching sessions are dropped
    before any analysis (see `is_excluded`).

    Returns (sessions, excluded_count).
    """
    # File mtime only decides which files are worth opening. A resumed session
    # replays old history into a freshly-written file, so the real window filter
    # has to be applied per call, using the call's own timestamp.
    paths = find_transcripts(root, since_days)
    if limit:
        paths = paths[:limit]

    cutoff = None
    if since_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    seen_requests = set()
    merged = {}
    excluded_sessions = set()
    for p in paths:
        s = parse_session(p)
        if not s:
            continue

        if is_excluded(s, exclude):
            excluded_sessions.add(s.session_id or p)
            continue

        fresh = []
        for c in s.calls:
            if cutoff and c.ts and c.ts < cutoff:
                continue  # replayed history from before the window
            # Calls with no requestId can't be dedup-keyed; keep them, they are rare.
            if c.request_id:
                if c.request_id in seen_requests:
                    continue
                seen_requests.add(c.request_id)
            fresh.append(c)

        key = s.session_id or p
        if key in merged:
            base = merged[key]
            base.calls.extend(fresh)
            # Tool results are replayed across forked files, so they can't simply
            # be summed. But taking only the largest file's view would discard
            # branch-specific results that exist in no other file. Dedupe on the
            # result's own identity instead, keeping the union.
            base.tool_results.extend(s.tool_results)
            # Spawns are replayed across forked files; keep one per tool_use id.
            for sid, sp in s._spawn_index.items():
                if sid not in base._spawn_index:
                    base._spawn_index[sid] = sp
                    base.agent_spawns.append(sp)
            base.user_turns = max(base.user_turns, s.user_turns)
            base.compactions = max(base.compactions, s.compactions)
            base.skills_used |= s.skills_used
        else:
            s.calls = fresh
            merged[key] = s

    # Collapse replayed tool results now that every fork has been folded in.
    for s in merged.values():
        seen = set()
        unique = []
        for r in s.tool_results:
            sig = (r.name, r.chars, r.is_error, r.target, r.error_text, r.is_sidechain)
            if sig in seen:
                continue
            seen.add(sig)
            unique.append(r)
        s.tool_results = unique

    sessions = [s for s in merged.values() if s.calls]
    return sessions, len(excluded_sessions)
