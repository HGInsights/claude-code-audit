"""The checks that put a dollar figure in front of the reader.

These tests pin two things: the arithmetic behind each savings number, and the
invariants that must hold across every check (no negative savings, no savings
larger than the spend analysed, a crash in one check never loses the report).

The savings estimates are deliberately model-based — "assume 60% is delegable"
— so the tests assert the model is applied as documented, not that the estimate
is objectively true.
"""

import pytest

import cc_audit
import checks
import grade
from checks import Finding, blended_input_price, build_findings
from pricing import CACHE_WRITE_5M_MULT, cost_of


class FakeCall:
    """One billed call, with the fields the checks read."""

    def __init__(self, model="claude-sonnet-5", raw=1000, w5m=0, w1h=0,
                 read=0, output=100, sidechain=False, tools=None, ts=None,
                 skill=None):
        self.model = model
        self.family = model.split("-")[1] if "-" in model else "unknown"
        self.raw_input = raw
        self.cache_write_5m = w5m
        self.cache_write_1h = w1h
        self.cache_read = read
        self.output = output
        self.is_sidechain = sidechain
        self.tools = tools or []
        self.ts = ts
        self.skill = skill
        self.request_id = "r"

    @property
    def total_input(self):
        return self.raw_input + self.cache_write_5m + self.cache_write_1h + self.cache_read

    @property
    def cache_write(self):
        return self.cache_write_5m + self.cache_write_1h

    @property
    def cost(self):
        return cost_of(self.model, self.raw_input, self.cache_write_5m,
                       self.cache_write_1h, self.cache_read, self.output)

    @property
    def cost_uncached(self):
        from pricing import cost_without_cache
        return cost_without_cache(self.model, self.raw_input, self.cache_write_5m,
                                  self.cache_write_1h, self.cache_read, self.output)


class FakeSession:
    def __init__(self, calls, repo="myrepo", tool_results=None, spawns=None,
                 user_turns=1, compactions=0):
        self.calls = calls
        self.repo = repo
        self.tool_results = tool_results or []
        self.agent_spawns = spawns or []
        self.user_turns = user_turns
        self.compactions = compactions
        self.skills_used = set()
        self.cwd = f"/Users/x/code/{repo}"
        self.session_id = "s1"
        self.path = "/tmp/s1.jsonl"

    @property
    def cost(self):
        return sum(c.cost for c in self.calls)

    @property
    def cost_uncached(self):
        return sum(c.cost_uncached for c in self.calls)

    @property
    def main_calls(self):
        return [c for c in self.calls if not c.is_sidechain]

    @property
    def sidechain_calls(self):
        return [c for c in self.calls if c.is_sidechain]

    @property
    def start(self):
        return min((c.ts for c in self.calls if c.ts), default=None)

    @property
    def end(self):
        return max((c.ts for c in self.calls if c.ts), default=None)

    def peak_context(self):
        return max((c.total_input for c in self.main_calls), default=0)


class FakeCtx(cc_audit.Context):
    """The real Context, with only its filesystem reads stubbed.

    Subclassing rather than duplicating matters: `has_cheap_explore` and
    `active_days` are read by the checks, and a hand-rolled double drifts from
    them silently — which is exactly how a check starts passing against a
    context shape that no longer exists.
    """

    def __init__(self, sessions, settings=None, agent_defs=None,
                 spawn_stats=None):
        self.sessions = sessions
        self.calls = [c for s in sessions for c in s.calls]
        self.settings = settings or {}
        self.total_cost = sum(c.cost for c in self.calls)
        self.compactions = sum(s.compactions for s in sessions)
        self.mcp_tool_count = self.settings.get("mcp_tool_count", 0)
        self.exclude_patterns = []
        self.excluded_files = 0
        # Stubbed: the real ones scan ~/.claude and the sessions' cwds.
        self.agent_defs = agent_defs or []
        self.spawn_stats = spawn_stats or {}
        self.grade = grade.compute(self)


def ctx_of(*calls, **kw):
    return FakeCtx([FakeSession(list(calls), **kw)])


class TestBlendedInputPrice:
    def test_weights_by_tokens_not_by_call_count(self):
        # One huge Opus call and many tiny Haiku calls. An unweighted mean over
        # calls would price this near Haiku, understating the real cost basis.
        calls = [FakeCall("claude-opus-5", raw=1_000_000, output=0)]
        calls += [FakeCall("claude-haiku-4-5", raw=100, output=0) for _ in range(100)]
        price = blended_input_price(calls)
        assert price == pytest.approx(5.0 / 1e6, rel=0.01)

    def test_single_model_equals_that_models_price(self):
        calls = [FakeCall("claude-sonnet-5", raw=500_000, output=0)]
        assert blended_input_price(calls) == pytest.approx(3.0 / 1e6)

    def test_no_tokens_is_zero_not_a_division_error(self):
        assert blended_input_price([]) == 0.0
        assert blended_input_price([FakeCall(raw=0, output=0)]) == 0.0


class TestCacheHitRate:
    def test_healthy_hit_rate_is_ok_with_no_savings(self):
        # 95% of billed input is cache reads.
        ctx = ctx_of(FakeCall(raw=1000, w5m=4000, read=95_000, output=100))
        f = checks.cache_hit_rate(ctx)
        assert f.severity == "ok"
        assert f.savings == 0.0
        assert f.fix == ""

    def test_low_hit_rate_is_high_severity(self):
        ctx = ctx_of(FakeCall(raw=10_000, w5m=50_000, read=10_000, output=100))
        f = checks.cache_hit_rate(ctx)
        assert f.severity == "high"
        assert f.savings > 0

    def test_savings_model_prices_excess_writes_at_the_write_read_spread(self):
        # read=10k, write=50k, raw=0 -> excess = 50k - 60k*0.10 = 44k tokens,
        # each priced at the 1.25x-0.10x spread on the blended input price.
        ctx = ctx_of(FakeCall(raw=0, w5m=50_000, read=10_000, output=0))
        f = checks.cache_hit_rate(ctx)
        per_tok = blended_input_price(ctx.calls)
        expected = 44_000 * per_tok * (CACHE_WRITE_5M_MULT - 0.10)
        assert f.savings == pytest.approx(expected)

    def test_no_billed_input_yields_no_finding(self):
        assert checks.cache_hit_rate(ctx_of(FakeCall(raw=0, output=0))) is None

    def test_no_savings_claimed_when_writes_are_a_small_share(self):
        # Writes below 15% of reads are normal prefix growth, not churn.
        ctx = ctx_of(FakeCall(raw=0, w5m=1000, read=100_000, output=0))
        assert checks.cache_hit_rate(ctx).savings == 0.0


class TestExpensiveModelGruntWork:
    def _grunt_call(self, n=1, **kw):
        # >20K input, <300 output, on Opus: retrieval-shaped.
        return [FakeCall("claude-opus-5", raw=50_000, output=100, **kw)
                for _ in range(n)]

    def test_no_opus_calls_yields_no_finding(self):
        ctx = ctx_of(FakeCall("claude-sonnet-5", raw=50_000, output=100))
        assert checks.expensive_model_grunt_work(ctx) is None

    def test_reasoning_shaped_opus_calls_are_not_flagged(self):
        # Large output means it was reasoning, not retrieval.
        ctx = ctx_of(FakeCall("claude-opus-5", raw=50_000, output=5_000))
        assert checks.expensive_model_grunt_work(ctx) is None

    def test_small_input_opus_calls_are_not_flagged(self):
        ctx = ctx_of(FakeCall("claude-opus-5", raw=1_000, output=100))
        assert checks.expensive_model_grunt_work(ctx) is None

    def test_savings_are_the_opus_sonnet_delta_discounted_to_60_pct(self):
        ctx = FakeCtx([FakeSession(self._grunt_call(10))])
        f = checks.expensive_model_grunt_work(ctx)
        opus = sum(c.cost for c in ctx.calls)
        sonnet = sum(
            cost_of("claude-sonnet-5", c.raw_input, c.cache_write_5m,
                    c.cache_write_1h, c.cache_read, c.output)
            for c in ctx.calls)
        assert f.savings == pytest.approx((opus - sonnet) * 0.6)

    def test_savings_never_exceed_what_was_actually_spent_on_those_calls(self):
        ctx = FakeCtx([FakeSession(self._grunt_call(50))])
        f = checks.expensive_model_grunt_work(ctx)
        assert 0 < f.savings < sum(c.cost for c in ctx.calls)

    def test_severity_escalates_with_the_figure(self):
        small = FakeCtx([FakeSession(self._grunt_call(1))])
        large = FakeCtx([FakeSession(self._grunt_call(2000))])
        assert checks.expensive_model_grunt_work(small).severity == "low"
        assert checks.expensive_model_grunt_work(large).severity == "high"

    def test_names_the_repos_carrying_the_waste(self):
        # The fix is worthless if it cannot say where to apply it.
        ctx = FakeCtx([
            FakeSession(self._grunt_call(10), repo="hot-repo"),
            FakeSession(self._grunt_call(1), repo="cold-repo"),
        ])
        f = checks.expensive_model_grunt_work(ctx)
        assert "hot-repo" in str(f.table)


class TestBuildFindings:
    def test_a_crashing_check_becomes_a_loud_finding_not_a_lost_report(
            self, monkeypatch, capsys):
        def exploding(ctx):
            raise ValueError("boom")
        exploding.__name__ = "exploding_check"

        monkeypatch.setattr(checks, "CHECKS", [exploding])
        findings = build_findings(ctx_of(FakeCall()))

        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "high"
        assert f.savings == 0.0
        assert "failed to run" in f.title
        # And it must be visible on stderr, not only in the report body.
        assert "crashed" in capsys.readouterr().err

    def test_one_crash_does_not_stop_the_other_checks(self, monkeypatch):
        def exploding(ctx):
            raise ValueError("boom")
        exploding.__name__ = "exploding_check"

        def fine(ctx):
            return Finding(key="fine", title="Fine", severity="ok", savings=0.0,
                           summary="ok")

        monkeypatch.setattr(checks, "CHECKS", [exploding, fine])
        keys = [f.key for f in build_findings(ctx_of(FakeCall()))]
        assert keys == ["exploding_check", "fine"]

    def test_checks_returning_none_are_omitted(self, monkeypatch):
        monkeypatch.setattr(checks, "CHECKS", [lambda ctx: None])
        assert build_findings(ctx_of(FakeCall())) == []


class TestInvariantsAcrossEveryCheck:
    """Properties that must hold for all checks on realistic inputs."""

    def _realistic_ctx(self):
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        calls = []
        for i in range(40):
            calls.append(FakeCall(
                "claude-opus-5", raw=5_000, w5m=20_000, read=200_000,
                output=150, ts=base + timedelta(minutes=3 * i),
                tools=["Read", "Bash"]))
        for i in range(20):
            calls.append(FakeCall(
                "claude-haiku-4-5", raw=1_000, read=10_000, output=500,
                sidechain=True, ts=base + timedelta(minutes=2 * i)))

        from parse import ToolResult
        results = [
            ToolResult("Read", 50_000, False, target="/Users/x/code/myrepo/big.ts"),
            ToolResult("Read", 80_000, False, target="/Users/x/code/myrepo/yarn.lock"),
            ToolResult("Bash", 2_000, True, target="npm test",
                       error_text="exit code 1"),
        ]
        return FakeCtx([FakeSession(calls, tool_results=results, user_turns=6,
                                    compactions=2)])

    @pytest.mark.parametrize("fn", checks.CHECKS, ids=lambda f: f.__name__)
    def test_every_check_runs_without_raising(self, fn):
        # build_findings swallows crashes into a finding, so an exception here
        # would otherwise show up only as a degraded report.
        fn(self._realistic_ctx())

    @pytest.mark.parametrize("fn", checks.CHECKS, ids=lambda f: f.__name__)
    def test_savings_are_never_negative(self, fn):
        f = fn(self._realistic_ctx())
        if f:
            assert f.savings >= 0, f"{fn.__name__} claims negative savings"

    @pytest.mark.parametrize("fn", checks.CHECKS, ids=lambda f: f.__name__)
    def test_no_single_check_claims_more_than_the_total_spend(self, fn):
        ctx = self._realistic_ctx()
        f = fn(ctx)
        if f:
            assert f.savings <= ctx.total_cost, (
                f"{fn.__name__} claims to save more than was spent")

    @pytest.mark.parametrize("fn", checks.CHECKS, ids=lambda f: f.__name__)
    def test_severity_is_one_of_the_four_levels(self, fn):
        f = fn(self._realistic_ctx())
        if f:
            assert f.severity in ("high", "medium", "low", "ok")

    @pytest.mark.parametrize("fn", checks.CHECKS, ids=lambda f: f.__name__)
    def test_an_actionable_finding_states_a_fix(self, fn):
        # A finding with a dollar figure and no remediation is not actionable.
        f = fn(self._realistic_ctx())
        if f and f.severity != "ok" and f.savings > 0:
            assert f.fix.strip(), f"{fn.__name__} has savings but no fix"

    @pytest.mark.parametrize("fn", checks.CHECKS, ids=lambda f: f.__name__)
    def test_healthy_findings_claim_no_savings(self, fn):
        f = fn(self._realistic_ctx())
        if f and f.severity == "ok":
            assert f.savings == 0.0

    @pytest.mark.parametrize("fn", checks.CHECKS, ids=lambda f: f.__name__)
    def test_finding_keys_and_titles_are_populated(self, fn):
        f = fn(self._realistic_ctx())
        if f:
            assert f.key and f.title and f.summary

    def test_finding_keys_are_unique(self):
        findings = build_findings(self._realistic_ctx())
        keys = [f.key for f in findings]
        assert len(keys) == len(set(keys))


class TestChecksOnDegenerateInput:
    """The checks must survive the shapes a real machine produces."""

    def test_a_single_trivial_call_crashes_nothing(self):
        ctx = ctx_of(FakeCall(raw=1, output=1))
        for fn in checks.CHECKS:
            fn(ctx)

    def test_zero_cost_calls_crash_nothing(self):
        ctx = ctx_of(FakeCall(raw=0, w5m=0, read=0, output=0))
        for fn in checks.CHECKS:
            fn(ctx)

    def test_calls_without_timestamps_crash_nothing(self):
        ctx = ctx_of(FakeCall(raw=10_000, output=100, ts=None))
        for fn in checks.CHECKS:
            fn(ctx)

    def test_a_session_with_no_tool_results_crashes_nothing(self):
        ctx = FakeCtx([FakeSession([FakeCall()], tool_results=[])])
        for fn in checks.CHECKS:
            fn(ctx)


class TestFormatters:
    @pytest.mark.parametrize("value,text", [
        (1234.5, "$1,234"), (12.345, "$12.35"), (0.001234, "$0.001"), (0.0, "$0.000"),
    ])
    def test_money_scales_precision_to_magnitude(self, value, text):
        assert checks._money(value) == text

    @pytest.mark.parametrize("value,text", [
        (2_500_000_000, "2.5B"), (1_500_000, "1.5M"), (2_500, "2.5K"), (999, "999"),
    ])
    def test_tokens_are_humanised(self, value, text):
        assert checks._tokens(value) == text

    def test_pct_of_zero_is_zero_not_an_error(self):
        assert checks._pct(5, 0) == 0.0
