"""Adversarial tests for the two redaction boundaries.

The README makes two promises that the whole design rests on:

  1. The stored JSON carries no paths, commands, repo names or session ids, so
     reports can be pooled across a team.
  2. The `--challenge` packet is config-only, so the critic pass cannot leak
     source off the machine.

Both are asserted here against deliberately hostile inputs: a repo named like a
path, a prompt containing an API key, unicode lookalikes. The rule is that a
leak must be *impossible by construction* (allowlist), not merely unobserved on
friendly data — so these tests attack the allowlist itself.
"""

import json

import pytest

import evidence
import store


# Values that must never survive into anything shared. Each is distinctive
# enough that a substring search for it is a reliable leak detector.
SECRETS = {
    "abs_path": "/Users/realperson/work/acme-secret-repo/src/billing.ts",
    "home_path": "/home/realperson/private/keys.py",
    "repo": "acme-secret-repo",
    "session": "8f3c1a2e-9b7d-4c5f-a1b2-c3d4e5f60718",
    "command": "gh pr create --title 'launch Q4 pricing'",
    "api_key": "sk-ant-api03-REALLOOKINGKEY1234567890",
    "branch": "feature/unreleased-acquisition",
    "prompt": "refactor the checkout flow before the Tesla deal closes",
}


def assert_no_secrets(blob, allow=()):
    """Fail naming the leaked value, so a regression is immediately legible."""
    text = blob if isinstance(blob, str) else json.dumps(blob)
    for label, value in SECRETS.items():
        if label in allow:
            continue
        assert value not in text, f"{label} leaked into the shared payload: {value}"


class FakeCall:
    def __init__(self, family="opus", cost=1.0, sidechain=False):
        self.family = family
        self.cost = cost
        self.cache_read = 1000
        self.cache_write = 500
        self.raw_input = 100
        self.is_sidechain = sidechain


class FakeSession:
    def __init__(self, repo=SECRETS["repo"]):
        self._repo = repo

    @property
    def repo(self):
        return self._repo

    def peak_context(self):
        return 50_000


class FakeCtx:
    """A context whose every free-text field is hostile."""

    def __init__(self):
        self.calls = [FakeCall(), FakeCall(family="haiku", sidechain=True)]
        self.sessions = [FakeSession(), FakeSession("another-private-repo")]
        self.total_cost = 42.0
        self.compactions = 3
        self.mcp_tool_count = 7
        self.agent_defs = []
        self.spawn_stats = {}
        self.settings = {}

    def active_days(self):
        return 5


def hostile_payload():
    """A full report payload with a secret in every field a leak could use."""
    return {
        "user_id": "alice-ab12",
        "generated": "2026-09-15T10:00:00",
        "window_days": 30,
        "pricing_as_of": "2026-09-15",
        "pricing_stale": False,
        "sessions": 2,
        "api_calls": 2,
        "total_cost_usd": 42.0,
        "estimated_savings_usd": 10.0,
        "grade": "B",
        "waste_usd": 10.0,
        "waste_pct": 23.8,
        "input_tokens": 1000,
        "output_tokens": 100,
        # Every one of these is a leak vector.
        "repo": SECRETS["repo"],
        "cwd": SECRETS["abs_path"],
        "session_id": SECRETS["session"],
        "git_branch": SECRETS["branch"],
        "excluded_patterns": [SECRETS["repo"], SECRETS["abs_path"]],
        "excluded_files": 2,
        "findings": [{
            "key": "model_choice",
            "title": "Opus used for retrieval",
            "severity": "high",
            "savings_usd": 10.0,
            # Free text quotes real paths, commands and prompts.
            "summary": f"Reading {SECRETS['abs_path']} on every turn",
            "fix": f"Run {SECRETS['command']} and stop reading {SECRETS['repo']}",
            "evidence": [SECRETS["prompt"], SECRETS["api_key"]],
        }],
    }


class TestSanitizeIsAnAllowlist:
    """The JSON promise: safe to pool across a team."""

    def test_no_secret_survives_sanitization(self):
        out = store.sanitize(hostile_payload(), FakeCtx())
        assert_no_secrets(out)

    def test_findings_keep_only_the_four_shareable_keys(self):
        out = store.sanitize(hostile_payload(), FakeCtx())
        assert set(out["findings"][0]) <= set(store._SHAREABLE_FINDING_KEYS)
        # And they keep what makes pooling useful.
        assert out["findings"][0]["severity"] == "high"
        assert out["findings"][0]["savings_usd"] == 10.0

    def test_free_text_finding_fields_are_dropped_entirely(self):
        out = store.sanitize(hostile_payload(), FakeCtx())
        f = out["findings"][0]
        for dropped in ("summary", "fix", "evidence"):
            assert dropped not in f

    def test_top_level_unknown_keys_are_not_copied_through(self):
        # The guarantee has to be an allowlist: a new field added upstream must
        # not appear in the shared JSON until someone declares it safe.
        payload = hostile_payload()
        payload["a_new_field_someone_added"] = SECRETS["abs_path"]
        out = store.sanitize(payload, FakeCtx())
        assert "a_new_field_someone_added" not in out
        assert_no_secrets(out)

    def test_exclusions_are_reduced_to_a_count(self):
        # The patterns themselves are repo names and paths.
        out = store.sanitize(hostile_payload(), FakeCtx())
        assert out["excluded_count"] == 2
        assert "excluded_patterns" not in out

    def test_repo_names_are_reduced_to_a_count(self):
        out = store.sanitize(hostile_payload(), FakeCtx())
        assert out["metrics"]["repo_count"] == 2
        assert_no_secrets(out)

    def test_metrics_are_numbers_only(self):
        out = store.sanitize(hostile_payload(), FakeCtx())
        for key, value in out["metrics"].items():
            if key == "by_model_family":
                # Model family names are ours, not the user's.
                assert set(value) <= {"opus", "sonnet", "haiku", "fable", "unknown"}
                continue
            assert isinstance(value, (int, float)), f"{key} is not numeric"

    def test_survives_a_payload_with_no_findings_or_context(self):
        out = store.sanitize({"user_id": "x"}, None)
        assert out["findings"] == []
        assert "metrics" not in out

    def test_carries_the_pricing_stamp_for_pooling(self):
        out = store.sanitize(hostile_payload(), FakeCtx())
        assert out["pricing_as_of"] == "2026-09-15"


class TestSanitizeUnicodeAndEncodingTricks:
    def test_a_repo_named_like_a_path_is_still_only_counted(self):
        payload = hostile_payload()
        ctx = FakeCtx()
        ctx.sessions = [FakeSession("/Users/realperson/work/acme-secret-repo")]
        out = store.sanitize(payload, ctx)
        assert out["metrics"]["repo_count"] == 1
        assert_no_secrets(out)

    def test_unicode_lookalike_paths_do_not_survive(self):
        # Cyrillic 'е' in a path that would defeat an ASCII denylist. The
        # allowlist does not care, which is the point of using one.
        payload = hostile_payload()
        sneaky = "/Users/rеalperson/wоrk/repo.ts"
        payload["findings"][0]["summary"] = sneaky
        out = store.sanitize(payload, FakeCtx())
        assert sneaky not in json.dumps(out, ensure_ascii=False)

    def test_secrets_hidden_in_a_nested_structure_do_not_survive(self):
        payload = hostile_payload()
        payload["findings"][0]["nested"] = {
            "deep": {"deeper": [SECRETS["api_key"], SECRETS["abs_path"]]}}
        out = store.sanitize(payload, FakeCtx())
        assert_no_secrets(out)

    def test_the_output_is_json_serialisable(self):
        # It is written with json.dump; a non-serialisable value would fail at
        # write time, after the report already claimed success.
        out = store.sanitize(hostile_payload(), FakeCtx())
        json.dumps(out)


class TestStoredFilesOnDisk:
    def test_the_json_file_written_to_disk_is_clean(self, tmp_path):
        md, js = store.save(
            markdown=f"# Report\n\nFull detail: {SECRETS['abs_path']}\n",
            payload=hostile_payload(), ctx=FakeCtx(), root=str(tmp_path))
        assert_no_secrets(json.loads(open(js).read()))

    def test_the_markdown_keeps_detail_for_the_person_who_ran_it(self, tmp_path):
        # The split is deliberate: the markdown is private and must stay
        # actionable, naming the file to stop reading.
        md, js = store.save(
            markdown=f"# Report\n\nStop reading {SECRETS['abs_path']}\n",
            payload=hostile_payload(), ctx=FakeCtx(), root=str(tmp_path))
        assert SECRETS["abs_path"] in open(md).read()

    def test_user_id_carries_no_hostname(self, tmp_path):
        uid = store.user_id()
        import platform
        node = platform.node()
        if node:
            assert node not in uid
        assert len(uid.split("-")[-1]) == 4


class TestChallengePacketIsConfigOnly:
    """The `--challenge` promise: nothing conversational leaves the machine."""

    def _packet(self, ctx=None, findings=None):
        ctx = ctx or FakeCtx()
        sanitized = store.sanitize(hostile_payload(), ctx)
        return evidence.build(ctx, findings or [], sanitized)

    def test_packet_from_a_hostile_context_leaks_nothing(self):
        # The findings' own text is written by this tool and legitimately names
        # example commands, so it is excluded from the secret sweep below by
        # design — `find_leaks` covers the machine-derived fields instead.
        packet = self._packet()
        assert_no_secrets({
            "agent_inventory": packet["agent_inventory"],
            "settings_env": packet["settings_env"],
            "metrics": packet["metrics"],
            "totals": packet["totals"],
        })

    def test_env_var_values_are_never_sent(self):
        ctx = FakeCtx()
        ctx.settings = {"settings.json": {"env": {
            "ANTHROPIC_API_KEY": SECRETS["api_key"],
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI",
            "MAX_MCP_OUTPUT_TOKENS": "25000",
            "PATH": SECRETS["abs_path"],
        }}}
        packet = self._packet(ctx)
        env = packet["settings_env"]
        assert_no_secrets(env)
        # Names are kept — they are the config signal the critic reasons about.
        assert set(env) == {"ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
                            "MAX_MCP_OUTPUT_TOKENS", "PATH"}
        # Every value is replaced by a placeholder; secret-shaped names get the
        # louder label. `MAX_MCP_OUTPUT_TOKENS` matches on "token" and so reads
        # as <redacted> too — over-redaction is the safe direction to err, since
        # the critic only ever needs to know the name is set.
        assert env["ANTHROPIC_API_KEY"] == "<redacted>"
        assert env["AWS_SECRET_ACCESS_KEY"] == "<redacted>"
        assert set(env.values()) <= {"<set>", "<redacted>"}

    @pytest.mark.parametrize("name", [
        "MY_SECRET", "GH_TOKEN", "api_key", "DB_PASSWORD", "PASSWD",
        "SOME_CREDENTIAL", "Api_Key",
    ])
    def test_secret_shaped_names_are_flagged_case_insensitively(self, name):
        ctx = FakeCtx()
        ctx.settings = {"settings.json": {"env": {name: "value"}}}
        assert self._packet(ctx)["settings_env"][name] == "<redacted>"

    def test_no_env_value_ever_survives_whatever_the_name(self):
        # The property that actually matters: the label is cosmetic, but the
        # value must be gone for every name, secret-shaped or not.
        ctx = FakeCtx()
        ctx.settings = {"settings.json": {"env": {
            f"VAR_{i}": SECRETS["api_key"] for i in range(5)}}}
        env = self._packet(ctx)["settings_env"]
        assert set(env.values()) <= {"<set>", "<redacted>"}
        assert_no_secrets(env)

    def test_env_merges_both_settings_files(self):
        ctx = FakeCtx()
        ctx.settings = {
            "settings.json": {"env": {"A": "1"}},
            "settings.local.json": {"env": {"B": "2"}},
        }
        assert set(self._packet(ctx)["settings_env"]) == {"A", "B"}

    def test_agent_descriptions_are_truncated(self):
        class Def:
            name = "reviewer"
            scope = "user"
            model = "sonnet"
            description = "x" * 5000
            delegates_externally = True

        ctx = FakeCtx()
        ctx.agent_defs = [Def()]
        entry = self._packet(ctx)["agent_inventory"][0]
        assert len(entry["description"]) == 400


class TestFindLeaksGate:
    """The hard gate that runs before anything is sent."""

    @pytest.mark.parametrize("hostile", [
        "/Users/realperson/work/thing/",
        "/home/realperson/private/",
        "billing.ts",
        "app.tsx",
        "handler.py",
        "main.go",
        "Service.java",
        "https://internal.example.com/runbook",
        "gh pr create --title x",
        "gh issue list",
        "gh api /repos/acme/secret",
    ])
    def test_conversational_content_is_detected(self, hostile):
        packet = {"agent_inventory": [{"description": hostile}],
                  "settings_env": {}}
        assert evidence.find_leaks(packet), f"not detected: {hostile}"

    def test_a_clean_packet_passes(self):
        packet = {
            "agent_inventory": [{
                "name": "explore", "scope": "user", "pinned_model": "haiku",
                "description": "Read-only search agent for broad fan-out.",
                "repos": 3, "spawns": 12, "model_overrides": {},
            }],
            "settings_env": {"MAX_MCP_OUTPUT_TOKENS": "<set>"},
        }
        assert evidence.find_leaks(packet) == []

    def test_our_own_documented_paths_are_allowed(self):
        # The tool's recommendations name these by design; flagging them would
        # make the gate fire on every run and train people to bypass it.
        for allowed in evidence._ALLOWED:
            packet = {"agent_inventory": [
                {"description": f"create {allowed} to fix this"}],
                "settings_env": {}}
            assert evidence.find_leaks(packet) == [], allowed

    def test_our_own_docs_urls_are_allowed(self):
        packet = {"agent_inventory": [
            {"description": "see https://docs.claude.com/en/docs for detail"}],
            "settings_env": {}}
        assert evidence.find_leaks(packet) == []

    def test_a_leak_in_settings_env_is_detected(self):
        packet = {"agent_inventory": [],
                  "settings_env": {"PATH": "/Users/realperson/bin/"}}
        assert evidence.find_leaks(packet)

    def test_accepts_a_raw_string_subject(self):
        assert evidence.find_leaks("/Users/realperson/x/")
        assert evidence.find_leaks("nothing here") == []

    def test_reports_each_distinct_hit_once(self):
        packet = {"agent_inventory": [
            {"description": "billing.ts billing.ts app.py"}],
            "settings_env": {}}
        assert sorted(evidence.find_leaks(packet)) == ["app.py", "billing.ts"]

    def test_the_gate_runs_on_the_real_builder_output(self):
        # An end-to-end check: a hostile context, through the real builder,
        # must produce a packet the gate accepts — i.e. the builder's
        # structured-fields-only design actually holds.
        ctx = FakeCtx()
        packet = evidence.build(ctx, [], store.sanitize(hostile_payload(), ctx))
        assert evidence.find_leaks(packet) == []
