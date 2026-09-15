"""Optional LLM pass that CHALLENGES the deterministic recommendations.

This is a critic, not a narrator. Its job is to catch recommendations that
assert something false about the setup — the failure mode the fixed-threshold
checks cannot see:

  - "create an explore.md agent"  ... when 40 agents are already defined
  - "pin the reviewer to Sonnet"  ... when that pin is a Codex fallback tier
  - "/clear between tasks"        ... when the session is one long task

It never computes or adjusts a number. The deterministic pass owns the
arithmetic; the critic only rules on whether a recommendation is supported by
the evidence. Keeping that boundary is what keeps the figures auditable.

Runs via the already-authenticated `claude` CLI, so there is no API key to
handle and anyone auditing a Claude Code setup necessarily has it installed.
"""

import json
import shutil
import subprocess

SYSTEM = """You are auditing the RECOMMENDATIONS of a cost-analysis tool, not the user's code.

You will receive a JSON evidence packet describing a Claude Code setup: its agent
definitions, how those agents were spawned, settings, aggregate token/cost
metrics, and the tool's findings. Each finding carries a `recommendation` and a
list of `implicit_claims` — things the recommendation assumes about the setup.

Your job is to CHALLENGE each recommendation against the evidence. For every one:

1. Does it assert something the evidence CONTRADICTS? (e.g. it says to create an
   agent, but `agent_inventory` already contains cheap-pinned agents.)
2. Does it recommend changing something whose purpose it has misread? (e.g. an
   agent whose description says it uses an external CLI first — its pinned model
   is a FALLBACK tier, so downgrading it changes the safety net, not the hot path.)
3. Is the stated cause actually supported, or could the numbers have another
   explanation? (e.g. attributing cost to model choice when the calls emit almost
   no output, meaning the cost is re-read context and turn count.)
4. Do any findings share a root cause, so their savings should not be added?

Be adversarial and concrete. A recommendation that survives scrutiny should be
marked `verdict: "supported"` with no commentary — do not manufacture doubt.
Reserve `contradicted` for a claim the evidence actually disproves, and
`unsupported` for one the evidence simply cannot back either way.

Never invent numbers. Cite only fields present in the packet.

IMPORTANT — `revised_recommendation` REPLACES the original in the report. The
reader never sees the version you rejected, so do not write it as a rebuttal.
Write it as the instruction they should follow: what to change, where, and why
it is right for *this* setup. No "instead of", no "actually", no reference to
the original recommendation. If the original was fine, return null.

Keep every revised recommendation under 60 words and lead with the action.

Return ONLY valid JSON:
{
  "verdicts": [
    {
      "finding_key": "...",
      "verdict": "supported" | "unsupported" | "contradicted",
      "why": "one sentence, citing the evidence field that decides it",
      "revised_recommendation": "the corrected instruction, or null if unchanged"
    }
  ],
  "shared_root_causes": [
    {"finding_keys": ["a","b"], "why": "one sentence on the shared mechanism"}
  ]
}"""


class CriticUnavailable(RuntimeError):
    pass


def available():
    return shutil.which("claude") is not None


def review(packet, model="sonnet", timeout=180):
    """Ask the critic to challenge the findings. Returns parsed JSON."""
    if not available():
        raise CriticUnavailable(
            "the `claude` CLI was not found on PATH; skipping the critic pass"
        )

    prompt = (
        "Evidence packet:\n\n```json\n"
        + json.dumps(packet, indent=2, sort_keys=True)
        + "\n```\n\nChallenge each recommendation. Return only the JSON object."
    )

    try:
        proc = subprocess.run(
            ["claude", "-p", prompt,
             "--model", model,
             "--append-system-prompt", SYSTEM,
             "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CriticUnavailable(f"critic timed out after {timeout}s")

    if proc.returncode != 0:
        raise CriticUnavailable(
            f"claude CLI exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
        )

    return _extract(proc.stdout)


def _extract(stdout):
    """Pull the critic's JSON out of the CLI envelope."""
    text = stdout.strip()
    if not text:
        raise CriticUnavailable("critic returned empty output")

    # `--output-format json` wraps the reply; unwrap it if present.
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and "result" in envelope:
            text = envelope["result"]
        elif isinstance(envelope, dict) and "verdicts" in envelope:
            return envelope
    except json.JSONDecodeError:
        pass

    if not isinstance(text, str):
        raise CriticUnavailable("unexpected critic envelope")

    # The model may fence the JSON.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise CriticUnavailable("critic returned no JSON object")
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise CriticUnavailable(f"critic returned unparseable JSON: {exc}")

    if "verdicts" not in parsed:
        raise CriticUnavailable("critic JSON missing `verdicts`")
    return parsed
