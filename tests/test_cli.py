"""End-to-end runs of the CLI as a subprocess.

These exercise what a user actually invokes, including the cases the issue
called out as untested beyond one laptop: no `~/.claude` at all, an empty one,
and the `--challenge` gate refusing to send a packet.

Running it as a subprocess rather than calling `main()` keeps the exit codes
and stderr contract under test, which is what CI and shell wrappers depend on.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.test_parse import _assistant, write_transcript

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "cc_audit.py")


def run(*args, home=None, cwd=None):
    """Invoke the CLI with an isolated HOME so the real ~/.claude is untouched."""
    env = dict(os.environ)
    if home:
        env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True, text=True, env=env, cwd=cwd or ROOT, timeout=120)


@pytest.fixture
def fake_home(tmp_path):
    """A HOME with a populated ~/.claude/projects tree."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "-Users-x-code-myrepo"
    projects.mkdir(parents=True)
    write_transcript(projects / "a.jsonl", [
        _assistant("r1", model="claude-opus-5", raw=5_000, w5m=30_000,
                   read=200_000, output=80),
        _assistant("r2", model="claude-opus-5", raw=2_000, w5m=25_000,
                   read=250_000, output=120),
        _assistant("r3", model="claude-haiku-4-5", raw=1_000, read=5_000,
                   output=400, sidechain=True),
    ])
    return home


class TestHelp:
    def test_help_exits_clean(self):
        p = run("--help")
        assert p.returncode == 0
        assert "cc-audit" in p.stdout or "usage" in p.stdout.lower()

    def test_help_documents_the_challenge_flag(self):
        assert "--challenge" in run("--help").stdout


class TestNoTranscripts:
    def test_absent_claude_dir_exits_1_with_an_explanation(self, tmp_path):
        # A machine that has never run Claude Code must get a clear message,
        # not a traceback.
        home = tmp_path / "empty-home"
        home.mkdir()
        p = run("--no-store", home=home, cwd=str(tmp_path))
        assert p.returncode == 1
        assert "No transcripts found" in p.stderr
        assert "Traceback" not in p.stderr

    def test_empty_projects_dir_exits_1(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        p = run("--no-store", home=home, cwd=str(tmp_path))
        assert p.returncode == 1
        assert "No transcripts found" in p.stderr

    def test_a_projects_dir_of_junk_exits_1(self, tmp_path):
        home = tmp_path / "home"
        proj = home / ".claude" / "projects"
        proj.mkdir(parents=True)
        (proj / "a.jsonl").write_text("not json\n{broken\n")
        (proj / "b.txt").write_text("ignored")
        p = run("--no-store", home=home, cwd=str(tmp_path))
        assert p.returncode == 1
        assert "Traceback" not in p.stderr

    def test_everything_excluded_says_so_distinctly(self, fake_home, tmp_path):
        # A different message from "no transcripts": the fix is to loosen the
        # exclusions, not to check the machine.
        p = run("--no-store", "--no-config", "--exclude", "myrepo",
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 1
        assert "excluded" in p.stderr.lower()


class TestFullRun:
    def test_writes_a_report_and_exits_zero(self, fake_home, tmp_path):
        out = tmp_path / "report.md"
        p = run("--no-store", "--no-config", "--out", str(out),
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 0, p.stderr
        body = out.read_text()
        assert body.startswith("# Claude Code cost audit")
        assert "Bottom line" in body

    def test_the_report_carries_the_pricing_as_of_date(self, fake_home, tmp_path):
        import pricing
        out = tmp_path / "report.md"
        run("--no-store", "--no-config", "--out", str(out),
            home=fake_home, cwd=str(tmp_path))
        assert pricing.PRICES_AS_OF in out.read_text()

    def test_json_output_is_parseable_and_stamped(self, fake_home, tmp_path):
        import pricing
        p = run("--no-store", "--no-config", "--json",
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert data["pricing_as_of"] == pricing.PRICES_AS_OF
        assert data["api_calls"] > 0
        assert data["total_cost_usd"] > 0

    def test_progress_goes_to_stderr_so_stdout_stays_machine_readable(
            self, fake_home, tmp_path):
        p = run("--no-store", "--no-config", "--json",
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        json.loads(p.stdout)  # would fail if progress text were interleaved
        assert "Reading transcripts" in p.stderr

    def test_storing_writes_the_dated_pair(self, fake_home, tmp_path):
        reports = tmp_path / "reports"
        p = run("--no-config", "--report-dir", str(reports),
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 0, p.stderr
        found = list(reports.glob("*/*.json"))
        assert found
        data = json.loads(found[0].read_text())
        # The stored JSON is the shareable one: no paths anywhere in it.
        assert "/Users/" not in json.dumps(data)

    def test_list_repos_names_the_repo(self, fake_home, tmp_path):
        p = run("--list-repos", home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 0, p.stderr
        assert "myrepo" in p.stdout

    def test_days_window_is_honoured(self, fake_home, tmp_path):
        # The fixture's calls are dated 2026-09-01; a 1-day window excludes them.
        p = run("--no-store", "--no-config", "--days", "1",
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 1


class TestChallengeGate:
    def test_show_packet_prints_config_only_json(self, fake_home, tmp_path):
        p = run("--no-store", "--no-config", "--show-packet",
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 0, p.stderr
        packet = json.loads(p.stdout)
        assert set(packet) >= {"metrics", "totals", "agent_inventory",
                               "settings_env", "findings"}

    def test_the_packet_contains_no_paths_or_repo_names(self, fake_home, tmp_path):
        p = run("--no-store", "--no-config", "--show-packet",
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        packet = json.loads(p.stdout)
        # The findings' own text is written by the tool and may name example
        # commands; the machine-derived fields must be clean.
        machine_derived = json.dumps({
            "agent_inventory": packet["agent_inventory"],
            "settings_env": packet["settings_env"],
            "metrics": packet["metrics"],
        })
        assert "/Users/" not in machine_derived
        assert "myrepo" not in machine_derived

    def test_show_packet_does_not_invoke_the_critic(self, fake_home, tmp_path):
        # --show-packet is the audit path: it must never send anything.
        p = run("--no-store", "--no-config", "--show-packet",
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        assert "Challenging" not in p.stderr

    def test_a_leaky_packet_is_refused_with_exit_2(self, fake_home, tmp_path,
                                                   monkeypatch):
        # Simulate a regression in the packet builder by planting an agent
        # definition whose description carries an absolute path. The gate must
        # refuse rather than send it.
        agents_dir = fake_home / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "leaky.md").write_text(
            "---\nname: leaky\ndescription: reads /Users/realperson/secret/x.ts\n"
            "model: haiku\n---\n\nBody.\n")
        p = run("--no-store", "--no-config", "--challenge",
                "--out", str(tmp_path / "r.md"),
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 2
        assert "refusing to send" in p.stderr


class TestTrend:
    def test_trend_with_nothing_stored_explains_rather_than_crashing(
            self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        p = run("--trend", "--report-dir", str(tmp_path / "reports"),
                home=home, cwd=str(tmp_path))
        assert p.returncode == 1
        assert "Run an audit first" in p.stdout + p.stderr
        assert "Traceback" not in p.stderr

    def test_trend_reads_back_stored_runs(self, fake_home, tmp_path):
        reports = tmp_path / "reports"
        run("--no-config", "--report-dir", str(reports),
            "--out", str(tmp_path / "r.md"), home=fake_home, cwd=str(tmp_path))
        p = run("--trend", "--report-dir", str(reports),
                home=fake_home, cwd=str(tmp_path))
        assert p.returncode == 0, p.stderr
        assert "$" in p.stdout
