"""Persist audit reports so learnings can be extracted as more people run this.

Layout, under `~/.claude/cc-audit-reports/`:

    <user-id>/
      2026-08-05.md        full report — paths, commands, session ids
      2026-08-05.json      sanitized metrics — safe to pool across people
      latest.md -> ...     symlink to the most recent run

One run per user per day; re-running the same day overwrites in place.

The split is the whole point. The markdown is for the engineer who ran it and
keeps every detail that made a finding actionable. The JSON is what you collect
centrally, and it deliberately carries no file paths, no shell commands, and no
repo names — so pooling it cannot leak someone's work.
"""

import getpass
import hashlib
import json
import os
import platform
import re
from datetime import datetime

DEFAULT_ROOT = os.path.expanduser("~/.claude/cc-audit-reports")


def user_id():
    """Stable `<user>-<machine hash>` id.

    The machine hash distinguishes two laptops belonging to the same person
    without embedding the hostname, which often contains a real name or an
    asset tag.
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or "unknown"
    user = re.sub(r"[^a-z0-9._-]+", "-", user.strip().lower()) or "unknown"

    node = platform.node() or ""
    machine = hashlib.sha256(node.encode("utf-8", "replace")).hexdigest()[:4]
    return f"{user}-{machine}"


# Anything that could carry a path, a command, or a repo name is dropped from
# the shared record rather than filtered — a denylist on free text leaks.
_SHAREABLE_FINDING_KEYS = ("key", "title", "severity", "savings_usd")


def sanitize(payload, ctx=None):
    """Reduce a full report payload to metrics that are safe to pool.

    Keeps: counts, token totals, costs, per-finding severity and savings.
    Drops: repo names, file paths, shell commands, session ids, and every
    free-text field (summaries and fixes quote real paths).
    """
    findings = []
    for f in payload.get("findings", []):
        findings.append({k: f[k] for k in _SHAREABLE_FINDING_KEYS if k in f})

    out = {
        "schema": 1,
        "user_id": payload.get("user_id"),
        "generated": payload.get("generated"),
        "window_days": payload.get("window_days"),
        # Which pricing table produced these figures. Pooling reports across a
        # team is only meaningful if each one says what it was priced against.
        "pricing_as_of": payload.get("pricing_as_of"),
        "pricing_stale": payload.get("pricing_stale"),
        "sessions": payload.get("sessions"),
        "api_calls": payload.get("api_calls"),
        "total_cost_usd": payload.get("total_cost_usd"),
        "estimated_savings_usd": payload.get("estimated_savings_usd"),
        # Efficiency grade: waste as a share of this setup's own spend.
        "grade": payload.get("grade"),
        "waste_usd": payload.get("waste_usd"),
        "waste_pct": payload.get("waste_pct"),
        "input_tokens": payload.get("input_tokens"),
        "output_tokens": payload.get("output_tokens"),
        # How many patterns were excluded, never which ones (they are paths).
        "excluded_count": len(payload.get("excluded_patterns") or []),
        "findings": findings,
    }

    if ctx is not None:
        calls = ctx.calls
        read = sum(c.cache_read for c in calls)
        write = sum(c.cache_write for c in calls)
        raw = sum(c.raw_input for c in calls)
        billed = read + write + raw
        by_family = {}
        for c in calls:
            fam = by_family.setdefault(c.family, {"calls": 0, "cost_usd": 0.0})
            fam["calls"] += 1
            fam["cost_usd"] += c.cost
        for fam in by_family.values():
            fam["cost_usd"] = round(fam["cost_usd"], 2)

        out["metrics"] = {
            "active_days": ctx.active_days(),
            "cost_per_active_day_usd": round(
                ctx.total_cost / ctx.active_days(), 2) if ctx.active_days() else 0,
            "cache_hit_rate_pct": round(100.0 * read / billed, 2) if billed else 0,
            "cache_read_tokens": read,
            "cache_write_tokens": write,
            "uncached_input_tokens": raw,
            "compactions": ctx.compactions,
            "mcp_tool_count": ctx.mcp_tool_count,
            "repo_count": len({s.repo for s in ctx.sessions}),
            "sidechain_calls": sum(1 for c in calls if c.is_sidechain),
            "main_calls": sum(1 for c in calls if not c.is_sidechain),
            "peak_context_tokens": max(
                (s.peak_context() for s in ctx.sessions), default=0),
            "by_model_family": by_family,
        }
    return out


def save(markdown, payload, ctx=None, root=None, when=None):
    """Write the dated report pair. Returns (md_path, json_path).

    Re-running on the same day overwrites that day's files: one audit per user
    per day is the intended granularity.
    """
    root = root or DEFAULT_ROOT
    uid = payload.get("user_id") or user_id()
    day = (when or datetime.now()).strftime("%Y-%m-%d")

    target = os.path.join(root, uid)
    os.makedirs(target, exist_ok=True)

    md_path = os.path.join(target, f"{day}.md")
    json_path = os.path.join(target, f"{day}.json")

    with open(md_path, "w") as fh:
        fh.write(markdown)
    with open(json_path, "w") as fh:
        json.dump(sanitize(payload, ctx), fh, indent=2)
        fh.write("\n")

    _link_latest(target, day)
    return md_path, json_path


def _link_latest(target, day):
    """Point `latest.*` at today's files, so tooling has a stable path."""
    for ext in ("md", "json"):
        link = os.path.join(target, f"latest.{ext}")
        try:
            if os.path.islink(link) or os.path.exists(link):
                os.unlink(link)
            os.symlink(f"{day}.{ext}", link)
        except OSError:
            pass  # symlinks are a convenience, not a requirement


def history(root=None, uid=None):
    """Every stored sanitized report, oldest first.

    With no `uid`, returns reports for all users under the root — this is what
    a future cross-user analysis would read.
    """
    root = root or DEFAULT_ROOT
    if not os.path.isdir(root):
        return []

    users = [uid] if uid else sorted(
        d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
    )
    out = []
    for user in users:
        d = os.path.join(root, user)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json") or name.startswith("latest"):
                continue
            try:
                with open(os.path.join(d, name)) as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            data.setdefault("user_id", user)
            data.setdefault("date", name[: -len(".json")])
            out.append(data)
    return sorted(out, key=lambda r: (r.get("date", ""), r.get("user_id", "")))
