"""Build the evidence packet sent to the LLM critic.

The rule this module enforces: **configuration and counters may be sent;
conversation content may not.**

Allowed — things the user wrote as config, or numbers we derived:
  - agent definitions (name, scope, pinned model, description)
  - how agents were actually spawned, and with what model override
  - settings keys and env var names (never values)
  - the findings' own numbers, severities and recommendation text
  - aggregate counters (call counts, token totals, costs, ratios)

Never sent — anything containing conversation or code:
  - prompts, assistant messages, thinking
  - tool results, file contents, diffs
  - file paths, shell commands, repo names, session ids, URLs

The distinction matters because the critic's job is to check whether a
recommendation contradicts the *setup*, and the setup is fully described by
config and counters. It never needs to read what was said.
"""

import json
import re

# Recommendations assert things about the setup. Each claim below is checkable
# against the evidence packet, which is what makes the critic useful rather
# than merely opinionated.
CLAIM_PATTERNS = [
    (r"creat\w*\s+`?~?/?\.?claude/agents", "asserts no agent definitions exist"),
    (r"\bexplore\.md\b", "asserts no cheap retrieval agent exists"),
    (r"pin(ned|ning)?\s+to\s+\w*haiku", "asserts agents are not already cheap-pinned"),
    (r"\bmodel:\s*haiku\b", "recommends a model pin"),
    (r"CLAUDE_CODE_SUBAGENT_MODEL", "asserts the default subagent model is unset"),
    (r"MAX_MCP_OUTPUT_TOKENS", "asserts the MCP output cap is unset"),
    (r"/clear\b", "asserts sessions can be cleared at a boundary"),
    (r"\bdowngrad\w+|cheaper model", "recommends a model downgrade"),
    (r"subagents?\b.*\bdelegat", "asserts work is not already delegated"),
]

_SECRETISH = re.compile(r"(secret|token|key|password|passwd|credential)", re.I)


def _redact_env(env):
    """Keep env var NAMES (they are config signals); never their values."""
    out = {}
    for k in sorted(env or {}):
        out[k] = "<redacted>" if _SECRETISH.search(k) else "<set>"
    return out


def agent_inventory(defs, stats):
    """Deduplicated agent config: the fact that a reviewer is Codex-primary
    lives in its description, so descriptions are included."""
    seen = {}
    for a in defs:
        key = (a.name, a.model, a.description)
        if key in seen:
            seen[key]["repos"] += 1
            continue
        seen[key] = {
            "name": a.name,
            "scope": a.scope,
            "pinned_model": a.model or None,
            "delegates_externally": a.delegates_externally,
            # Truncated: enough to reveal intent, not a whole system prompt.
            "description": (a.description or "")[:400],
            "repos": 1,
        }

    inventory = list(seen.values())
    for entry in inventory:
        s = stats.get(entry["name"])
        entry["spawns"] = s["spawns"] if s else 0
        entry["model_overrides"] = dict(s["overrides"]) if s else {}
    inventory.sort(key=lambda e: -e["spawns"])

    # Types spawned that have no definition at all.
    defined = {e["name"] for e in inventory}
    for kind, s in stats.items():
        if kind not in defined:
            inventory.append({
                "name": kind,
                "scope": "undefined",
                "pinned_model": None,
                "delegates_externally": False,
                "description": "",
                "repos": 0,
                "spawns": s["spawns"],
                "model_overrides": dict(s["overrides"]),
            })
    return inventory


def build(ctx, findings, sanitized):
    """Assemble the packet. Contains no conversation content by construction."""
    settings = ctx.settings or {}
    env = {}
    for name in ("settings.json", "settings.local.json"):
        env.update((settings.get(name) or {}).get("env") or {})

    claims = []
    for f in findings:
        matched = []
        text = f"{f.summary} {f.fix}"
        for pattern, meaning in CLAIM_PATTERNS:
            if re.search(pattern, text, re.I):
                matched.append(meaning)
        claims.append({
            "key": f.key,
            "title": f.title,
            "severity": f.severity,
            "estimated_savings_usd": round(f.savings, 2),
            "summary": f.summary,
            "recommendation": f.fix,
            "implicit_claims": matched,
        })

    return {
        "metrics": sanitized.get("metrics", {}),
        "totals": {
            k: sanitized.get(k)
            for k in ("sessions", "api_calls", "total_cost_usd",
                      "estimated_savings_usd", "input_tokens", "output_tokens")
        },
        "agent_inventory": agent_inventory(ctx.agent_defs, ctx.spawn_stats),
        "settings_env": _redact_env(env),
        "mcp_servers_configured": len(settings.get("mcp_servers") or []),
        "mcp_tools_seen": ctx.mcp_tool_count,
        "findings": claims,
    }


# Belt and braces: the packet is built from structured fields only, but a
# regression could still let a path or command through. Scan before sending.
_LEAK_PATTERNS = [
    re.compile(r"/(?:Users|home)/[^/\s\"]+/"),          # absolute home paths
    re.compile(r"\b[a-zA-Z0-9_.-]+\.(?:ts|tsx|js|jsx|py|go|rb|java)\b"),
    re.compile(r"\bhttps?://(?!claude\.com|docs\.claude\.com)"),
    re.compile(r"\bgh (?:pr|issue|api) "),
]

# Phrases that legitimately appear in our own recommendation text.
_ALLOWED = (
    "~/.claude/agents/explore.md",
    "~/.claude/settings.json",
    ".claude/agents",
    "cc_audit.py",
)


def find_leaks(packet):
    """Return substrings that look like conversation content.

    Scans only the fields derived from the user's machine — agent metadata and
    settings. The findings' own `summary`/`recommendation` text is written by
    this tool and legitimately names things like `gh pr diff` as examples, so
    scanning it produces false positives rather than catching real leaks.
    """
    if isinstance(packet, str):
        subject = packet
    else:
        subject = json.dumps({
            "agent_inventory": packet.get("agent_inventory"),
            "settings_env": packet.get("settings_env"),
        })

    hits = []
    for pattern in _LEAK_PATTERNS:
        for m in pattern.finditer(subject):
            window = subject[max(0, m.start() - 40):m.end() + 40]
            if any(a in window for a in _ALLOWED):
                continue
            hits.append(m.group(0))
    return sorted(set(hits))
