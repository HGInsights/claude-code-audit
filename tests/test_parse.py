"""Transcript parsing, deduplication, and repo/exclusion resolution.

The transcripts on disk are messier than the format suggests: resumed sessions
replay history into new files, lines can be truncated mid-write, and records
that look like billed calls are sometimes synthetic. These tests fix the
handling of each.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import parse


def _assistant(request_id, model="claude-sonnet-5", *, ts=None, raw=1000,
               w5m=0, w1h=0, read=0, output=100, sidechain=False,
               cwd="/Users/x/code/myrepo", session="s1", tools=None,
               declared_write=None, skill=None):
    """One billed assistant record, shaped like a real transcript line."""
    usage = {
        "input_tokens": raw,
        "cache_read_input_tokens": read,
        "output_tokens": output,
    }
    if w5m or w1h:
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": w5m,
            "ephemeral_1h_input_tokens": w1h,
        }
    if declared_write is not None:
        usage["cache_creation_input_tokens"] = declared_write

    content = []
    for t in (tools or []):
        content.append({"type": "tool_use", "id": t["id"], "name": t["name"],
                        "input": t.get("input", {})})

    rec = {
        "type": "assistant",
        "requestId": request_id,
        "sessionId": session,
        "cwd": cwd,
        "timestamp": (ts or datetime(2026, 9, 1, tzinfo=timezone.utc)).isoformat(),
        "isSidechain": sidechain,
        "message": {"model": model, "usage": usage, "content": content},
    }
    if skill:
        rec["attributionSkill"] = skill
    return rec


def _user_text(text="hello", session="s1", cwd="/Users/x/code/myrepo"):
    return {"type": "user", "sessionId": session, "cwd": cwd,
            "message": {"content": text}}


def _tool_result(tool_use_id, content="result body", is_error=False,
                 session="s1", cwd="/Users/x/code/myrepo", sidechain=False):
    return {
        "type": "user", "sessionId": session, "cwd": cwd,
        "isSidechain": sidechain,
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": tool_use_id,
            "content": content, "is_error": is_error,
        }]},
    }


def write_transcript(path, records):
    """Write records as JSONL. Raw strings are written verbatim, so a test can
    inject a truncated or non-JSON line."""
    with open(path, "w") as fh:
        for r in records:
            fh.write(r if isinstance(r, str) else json.dumps(r))
            fh.write("\n")
    return path


@pytest.fixture
def transcript_dir(tmp_path):
    d = tmp_path / "projects" / "-Users-x-code-myrepo"
    d.mkdir(parents=True)
    return d


class TestTimestamps:
    def test_parses_z_suffixed_iso(self):
        got = parse._ts("2026-09-01T12:00:00Z")
        assert got == datetime(2026, 9, 1, 12, tzinfo=timezone.utc)

    @pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345, {}])
    def test_unparseable_values_are_none(self, bad):
        assert parse._ts(bad) is None


class TestRepoOf:
    @pytest.mark.parametrize("cwd,repo", [
        ("/Users/x/code/myrepo", "myrepo"),
        ("/Users/x/code/myrepo/", "myrepo"),
        ("/Users/x/code/myrepo/packages/web", "web"),
    ])
    def test_plain_paths_use_the_last_segment(self, cwd, repo):
        assert parse.repo_of(cwd) == repo

    @pytest.mark.parametrize("cwd", [
        "/Users/x/code/myrepo-worktrees/feature1",
        "/Users/x/code/myrepo.worktrees/feature1",
        "/Users/x/code/myrepo_worktrees/feature1",
        "/Users/x/code/myrepo/worktrees/feature1",
    ])
    def test_worktrees_collapse_onto_the_parent_repo(self, cwd):
        # Excluding a repo has to exclude its worktrees, or a user who excludes
        # a personal repo still leaks it.
        assert parse.repo_of(cwd) == "myrepo"

    @pytest.mark.parametrize("cwd", [None, "", "/", "///"])
    def test_missing_cwd_is_unknown(self, cwd):
        assert parse.repo_of(cwd) == "unknown"


class TestParseSession:
    def test_reads_usage_into_a_call(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("req1", raw=500, w5m=200, read=1000, output=50)])
        s = parse.parse_session(str(p))
        assert len(s.calls) == 1
        c = s.calls[0]
        assert (c.raw_input, c.cache_write_5m, c.cache_read, c.output) == (
            500, 200, 1000, 50)
        assert c.family == "sonnet"
        assert c.total_input == 1700

    def test_skips_malformed_and_truncated_lines(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("req1"),
            '{"type": "assistant", "requestId": "trunc", "mess',  # truncated mid-write
            "not json at all",
            "",
            _assistant("req2"),
        ])
        s = parse.parse_session(str(p))
        assert [c.request_id for c in s.calls] == ["req1", "req2"]

    @pytest.mark.parametrize("line", ["[]", '"a string"', "null", "123", "true"])
    def test_skips_valid_json_that_is_not_an_object(self, transcript_dir, line):
        p = write_transcript(transcript_dir / "a.jsonl", [line, _assistant("req1")])
        s = parse.parse_session(str(p))
        assert len(s.calls) == 1

    def test_skips_records_whose_message_is_not_a_dict(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            {"type": "assistant", "requestId": "bad", "message": "oops"},
            {"type": "assistant", "requestId": "bad2", "message": 42},
            _assistant("good"),
        ])
        s = parse.parse_session(str(p))
        assert [c.request_id for c in s.calls] == ["good"]

    def test_a_wrongly_typed_usage_block_bills_as_zero(self, transcript_dir):
        # `usage: []` is malformed but falsy, so it reads as no usage rather
        # than raising. Billing it as zero is the safe reading — the call is
        # still counted, it just contributes no cost.
        p = write_transcript(transcript_dir / "a.jsonl", [
            {"type": "assistant", "requestId": "r", "sessionId": "s1",
             "message": {"model": "claude-sonnet-5", "usage": []}}])
        s = parse.parse_session(str(p))
        assert len(s.calls) == 1
        assert s.calls[0].cost == 0.0

    def test_synthetic_model_is_not_billed(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("req1", model="<synthetic>"),
            _assistant("req2"),
        ])
        s = parse.parse_session(str(p))
        assert [c.request_id for c in s.calls] == ["req2"]

    def test_missing_usage_fields_default_to_zero(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [{
            "type": "assistant", "requestId": "r", "sessionId": "s1",
            "message": {"model": "claude-sonnet-5", "usage": {}},
        }])
        s = parse.parse_session(str(p))
        c = s.calls[0]
        assert (c.raw_input, c.cache_read, c.output) == (0, 0, 0)
        assert c.cost == 0.0

    def test_null_usage_values_are_treated_as_zero(self, transcript_dir):
        # Real transcripts carry explicit nulls, which would break arithmetic.
        p = write_transcript(transcript_dir / "a.jsonl", [{
            "type": "assistant", "requestId": "r", "sessionId": "s1",
            "message": {"model": "claude-sonnet-5", "usage": {
                "input_tokens": None, "cache_read_input_tokens": None,
                "output_tokens": None, "cache_creation_input_tokens": None}},
        }])
        s = parse.parse_session(str(p))
        assert s.calls[0].total_input == 0

    def test_declared_cache_total_without_split_lands_in_the_5m_bucket(
            self, transcript_dir):
        # The per-TTL split can be absent. Counting zero would understate the
        # cache write cost, which is the expensive half.
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r", declared_write=4000)])
        s = parse.parse_session(str(p))
        assert s.calls[0].cache_write_5m == 4000

    def test_explicit_split_wins_over_the_declared_total(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r", w5m=1000, w1h=500, declared_write=1500)])
        s = parse.parse_session(str(p))
        c = s.calls[0]
        assert (c.cache_write_5m, c.cache_write_1h) == (1000, 500)

    def test_returns_none_for_a_file_with_no_billed_calls(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [_user_text()])
        assert parse.parse_session(str(p)) is None

    def test_returns_none_for_an_unreadable_path(self, tmp_path):
        assert parse.parse_session(str(tmp_path / "nope.jsonl")) is None

    def test_counts_compactions_and_user_turns(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _user_text("first"),
            _assistant("r1"),
            {"type": "user", "sessionId": "s1", "isCompactSummary": True,
             "message": {"content": "summary"}},
            _user_text("second"),
        ])
        s = parse.parse_session(str(p))
        assert s.compactions == 1
        assert s.user_turns >= 2

    def test_meta_user_records_are_not_turns(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1"),
            {"type": "user", "sessionId": "s1", "isMeta": True,
             "message": {"content": "<system>"}},
        ])
        assert parse.parse_session(str(p)).user_turns == 0

    def test_attributes_a_tool_result_to_the_call_that_made_it(
            self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", tools=[{
                "id": "tu1", "name": "Read",
                "input": {"file_path": "/Users/x/code/myrepo/big.ts"}}]),
            _tool_result("tu1", "x" * 5000),
        ])
        s = parse.parse_session(str(p))
        assert len(s.tool_results) == 1
        r = s.tool_results[0]
        assert r.name == "Read"
        assert r.target.endswith("big.ts")
        assert r.chars == 5000

    def test_records_agent_spawns_with_model_overrides(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", tools=[{
                "id": "tu1", "name": "Agent",
                "input": {"subagent_type": "Explore", "model": "Haiku"}}]),
        ])
        s = parse.parse_session(str(p))
        assert len(s.agent_spawns) == 1
        assert s.agent_spawns[0].subagent_type == "Explore"
        assert s.agent_spawns[0].model == "haiku"  # normalised

    def test_skills_are_collected(self, transcript_dir):
        p = write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", skill="code-review")])
        assert parse.parse_session(str(p)).skills_used == {"code-review"}


class TestToolResult:
    def test_error_kinds_map_to_distinct_causes(self):
        cases = {
            "File has not been read yet": "edit_before_read",
            "String to replace not found": "stale_edit",
            "ENOENT: no such file": "bad_path",
            "Permission denied": "permission_denied",
            "exit code 127: command not found": "missing_command",
            "Command timed out": "timeout",
            "exit code 1": "shell_failure",
            "something else entirely": "other",
        }
        for text, kind in cases.items():
            r = parse.ToolResult("Bash", 10, True, error_text=text)
            assert r.error_kind == kind, text

    def test_non_errors_have_no_error_kind(self):
        assert parse.ToolResult("Read", 10, False, error_text="x").error_kind == ""

    @pytest.mark.parametrize("target,kind", [
        ("/x/screenshot.png", "image"),
        ("/x/.claude/skills/foo/SKILL.md", "instructions"),
        ("/x/CLAUDE.md", "instructions"),
        ("/x/node_modules/react/index.js", "generated"),
        ("/x/dist/bundle.js", "generated"),
        ("/x/src/app.ts", "file"),
    ])
    def test_kind_buckets_targets_for_remediation(self, target, kind):
        assert parse.ToolResult("Read", 10, False, target=target).kind == kind

    @pytest.mark.parametrize("target", [
        "/x/yarn.lock",
        "/x/Cargo.lock",
        "/x/poetry.lock",
        "/x/package-lock.json",
        "/x/pnpm-lock.yaml",
        "/x/bun.lockb",
    ])
    def test_lockfiles_count_as_generated_however_they_are_named(self, target):
        # Lockfiles are the biggest generated-file context sink. Naming varies:
        # only some put `.lock` at the end, so a substring test misses the rest
        # and the report never flags reading them.
        assert parse.ToolResult("Read", 10, False, target=target).kind == "generated"

    @pytest.mark.parametrize("target", [
        "/x/src/lock-manager.ts",
        "/x/src/locks.py",
    ])
    def test_source_files_mentioning_lock_are_not_generated(self, target):
        assert parse.ToolResult("Read", 10, False, target=target).kind == "file"

    def test_bash_without_a_file_target_is_a_command(self):
        assert parse.ToolResult("Bash", 10, False, target="git status").kind == "command"


class TestIsExcluded:
    def _session(self, cwd):
        s = parse.Session(path="x")
        s.cwd = cwd
        return s

    def test_no_patterns_excludes_nothing(self):
        assert not parse.is_excluded(self._session("/Users/x/code/myrepo"), [])

    def test_a_bare_name_matches_the_repo(self):
        assert parse.is_excluded(self._session("/Users/x/code/myrepo"), ["myrepo"])

    def test_a_bare_name_also_covers_that_repos_worktrees(self):
        s = self._session("/Users/x/code/myrepo-worktrees/feature1")
        assert parse.is_excluded(s, ["myrepo"])

    def test_a_bare_name_does_not_match_an_arbitrary_path_segment(self):
        # The case this guards: excluding a personal repo called `website` must
        # not drop a work repo's worktree that happens to be named `website`.
        s = self._session("/Users/x/code/work-repo-worktrees/website")
        assert parse.repo_of(s.cwd) == "work-repo"
        assert not parse.is_excluded(s, ["website"])

    def test_a_repo_name_wildcard_matches_repos(self):
        assert parse.is_excluded(self._session("/Users/x/code/side-thing"),
                                 ["side-*"])

    def test_a_path_shaped_pattern_matches_the_directory_tree(self):
        s = self._session("/Users/x/personal/thing")
        assert parse.is_excluded(s, ["/users/x/personal/*"])
        assert not parse.is_excluded(s, ["/users/x/work/*"])

    def test_matching_is_case_insensitive(self):
        assert parse.is_excluded(self._session("/Users/x/code/MyRepo"), ["myrepo"])

    def test_blank_patterns_are_ignored(self):
        assert not parse.is_excluded(self._session("/Users/x/code/myrepo"),
                                     ["", "   "])


class TestLoadSessions:
    def test_deduplicates_replayed_calls_by_request_id(self, transcript_dir):
        # A resumed session replays earlier calls into a new file. Billing
        # happens once per requestId, so counting both doubles the reported spend.
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("req1"), _assistant("req2")])
        write_transcript(transcript_dir / "b.jsonl", [
            _assistant("req1"), _assistant("req2"), _assistant("req3")])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert len(sessions) == 1
        assert sorted(c.request_id for c in sessions[0].calls) == [
            "req1", "req2", "req3"]

    def test_a_streamed_call_keeps_its_final_output_count(self, transcript_dir):
        # One call is written once per content block, and only the last record
        # carries the finished output count; the earlier ones are placeholders.
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("req1", output=1),
            _assistant("req1", output=3),
            _assistant("req1", output=278)])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert [c.output for c in sessions[0].calls] == [278]

    def test_a_streamed_call_keeps_its_final_input_counts(self, transcript_dir):
        # A server tool running inside one call grows the input counters as it
        # iterates, so the finished record is the one that was billed.
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("req1", raw=2679, read=0, output=3),
            _assistant("req1", raw=10682, read=7123, output=510)])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        call, = sessions[0].calls
        assert (call.raw_input, call.cache_read, call.output) == (10682, 7123, 510)

    def test_keeps_calls_that_have_no_request_id(self, transcript_dir):
        rec = _assistant("x")
        del rec["requestId"]
        write_transcript(transcript_dir / "a.jsonl", [rec])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert len(sessions[0].calls) == 1

    def test_separate_session_ids_stay_separate(self, transcript_dir):
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", session="s1")])
        write_transcript(transcript_dir / "b.jsonl", [
            _assistant("r2", session="s2")])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert len(sessions) == 2

    def test_window_is_applied_per_call_not_per_file(self, transcript_dir):
        # A freshly-written file can replay months-old calls; filtering by file
        # mtime alone would pull them into the window.
        now = datetime.now(timezone.utc)
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("old", ts=now - timedelta(days=90)),
            _assistant("new", ts=now - timedelta(days=1)),
        ])
        sessions, _ = parse.load_sessions(
            root=str(transcript_dir.parent), since_days=30)
        assert [c.request_id for c in sessions[0].calls] == ["new"]

    def test_excluded_sessions_are_dropped_and_counted(self, transcript_dir):
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", session="s1", cwd="/Users/x/code/secret")])
        write_transcript(transcript_dir / "b.jsonl", [
            _assistant("r2", session="s2", cwd="/Users/x/code/public")])
        sessions, excluded = parse.load_sessions(
            root=str(transcript_dir.parent), exclude=["secret"])
        assert excluded == 1
        assert [s.repo for s in sessions] == ["public"]

    def test_replayed_tool_results_are_collapsed(self, transcript_dir):
        records = [
            _assistant("r1", tools=[{"id": "tu1", "name": "Read",
                                     "input": {"file_path": "/x/a.ts"}}]),
            _tool_result("tu1", "body"),
        ]
        write_transcript(transcript_dir / "a.jsonl", records)
        write_transcript(transcript_dir / "b.jsonl", records)
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert len(sessions[0].tool_results) == 1

    def test_replayed_agent_spawns_are_deduped_by_tool_use_id(self, transcript_dir):
        rec = _assistant("r1", tools=[{"id": "tu1", "name": "Agent",
                                       "input": {"subagent_type": "Explore"}}])
        write_transcript(transcript_dir / "a.jsonl", [rec])
        write_transcript(transcript_dir / "b.jsonl", [rec, _assistant("r2")])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert len(sessions[0].agent_spawns) == 1

    def test_limit_caps_the_files_opened(self, transcript_dir):
        for i in range(5):
            write_transcript(transcript_dir / f"f{i}.jsonl", [
                _assistant(f"r{i}", session=f"s{i}")])
        sessions, _ = parse.load_sessions(
            root=str(transcript_dir.parent), limit=2)
        assert len(sessions) == 2

    def test_missing_root_returns_nothing_rather_than_raising(self, tmp_path):
        # The tool must degrade gracefully on a machine that has never run
        # Claude Code, not traceback.
        sessions, excluded = parse.load_sessions(
            root=str(tmp_path / "does-not-exist"))
        assert (sessions, excluded) == ([], 0)

    def test_empty_root_returns_nothing(self, tmp_path):
        empty = tmp_path / "projects"
        empty.mkdir()
        assert parse.load_sessions(root=str(empty)) == ([], 0)


class TestSessionAggregates:
    def test_peak_context_ignores_sidechain_calls(self, transcript_dir):
        # A subagent's context never enters the parent's window, so counting it
        # would credit delegation with bloat it actually prevented.
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", raw=10_000),
            _assistant("r2", raw=900_000, sidechain=True),
        ])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert sessions[0].peak_context() == 10_000

    def test_cost_sums_its_calls(self, transcript_dir):
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", model="claude-opus-5", raw=1_000_000, output=0),
            _assistant("r2", model="claude-opus-5", raw=1_000_000, output=0),
        ])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        assert sessions[0].cost == pytest.approx(10.0)

    def test_start_and_end_bracket_the_calls(self, transcript_dir):
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        write_transcript(transcript_dir / "a.jsonl", [
            _assistant("r1", ts=base + timedelta(hours=2)),
            _assistant("r2", ts=base),
        ])
        sessions, _ = parse.load_sessions(root=str(transcript_dir.parent))
        s = sessions[0]
        assert s.start == base
        assert s.end == base + timedelta(hours=2)


class TestFindTranscripts:
    def test_finds_nested_jsonl_newest_first(self, tmp_path):
        root = tmp_path / "projects"
        (root / "a" / "b").mkdir(parents=True)
        old = write_transcript(root / "a" / "old.jsonl", [_assistant("r1")])
        new = write_transcript(root / "a" / "b" / "new.jsonl", [_assistant("r2")])
        os.utime(old, (1_000_000, 1_000_000))
        found = parse.find_transcripts(root=str(root))
        assert found[0] == str(new)
        assert str(old) in found

    def test_ignores_non_jsonl_files(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        (root / "notes.md").write_text("x")
        assert parse.find_transcripts(root=str(root)) == []

    def test_missing_root_is_empty(self, tmp_path):
        assert parse.find_transcripts(root=str(tmp_path / "nope")) == []
