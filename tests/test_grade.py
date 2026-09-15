"""The efficiency grade: waste as a share of this setup's own spend.

The grade's defensibility rests on its components being computed from disjoint
token pools, so that summing them does not double-count. That partition is what
these tests pin.
"""

import pytest

import grade
from tests.test_checks import FakeCall, FakeCtx, FakeSession


def ctx_of(*calls, **kw):
    return FakeCtx([FakeSession(list(calls), **kw)])


class TestNoSpend:
    def test_no_calls_has_no_grade(self):
        assert grade.compute(FakeCtx([])) is None

    def test_zero_cost_has_no_grade(self):
        # Dividing waste by a zero total would be a ZeroDivisionError, and a
        # grade on no spend means nothing anyway.
        assert grade.compute(ctx_of(FakeCall(raw=0, w5m=0, read=0, output=0))) is None


class TestGradeBands:
    def test_an_efficient_setup_grades_well(self):
        # Small context, cheap model, big output: nothing to recover.
        ctx = ctx_of(*[FakeCall("claude-haiku-4-5", raw=1_000, output=2_000)
                       for _ in range(10)])
        result = grade.compute(ctx)
        assert result["grade"] in ("A", "B")
        assert result["waste_pct"] < 12

    def test_a_wasteful_setup_grades_badly(self):
        # Opus doing retrieval on an oversized context, cache constantly rebuilt.
        ctx = ctx_of(*[FakeCall("claude-opus-5", raw=5_000, w5m=400_000,
                                read=10_000, output=50) for _ in range(20)])
        result = grade.compute(ctx)
        assert result["grade"] in ("D", "F")
        assert result["waste_pct"] > 22

    def test_waste_is_never_more_than_the_spend(self):
        ctx = ctx_of(*[FakeCall("claude-opus-5", raw=1_000, w5m=500_000,
                                read=500_000, output=10) for _ in range(30)])
        result = grade.compute(ctx)
        assert 0 <= result["waste_usd"] <= ctx.total_cost
        assert 0 <= result["waste_pct"] <= 100

    def test_every_band_is_reachable_and_ordered(self):
        thresholds = [t for t, _, _ in grade.GRADES]
        assert thresholds == sorted(thresholds)
        assert [g for _, g, _ in grade.GRADES] == ["A", "B", "C", "D", "F"]


class TestComponents:
    def test_components_are_reported_with_a_reason(self):
        ctx = ctx_of(*[FakeCall("claude-opus-5", raw=5_000, w5m=400_000,
                                read=10_000, output=50) for _ in range(10)])
        result = grade.compute(ctx)
        assert result["components"]
        for comp in result["components"]:
            assert comp["key"] and comp["label"] and comp["why"]
            assert comp["cost"] > 0

    def test_component_costs_sum_to_the_reported_waste(self):
        # This is the whole claim: the partition is additive.
        ctx = ctx_of(*[FakeCall("claude-opus-5", raw=5_000, w5m=300_000,
                                read=50_000, output=100) for _ in range(15)])
        result = grade.compute(ctx)
        assert sum(c["cost"] for c in result["components"]) == pytest.approx(
            result["waste_usd"], rel=0.01)

    def test_oversized_context_is_charged_once_not_also_as_retrieval(self):
        # A call that is both oversized and retrieval-shaped must not have its
        # above-ceiling tokens counted in both components.
        ctx = ctx_of(FakeCall("claude-opus-5", raw=0, read=500_000, output=50))
        result = grade.compute(ctx)
        assert sum(c["cost"] for c in result["components"]) == pytest.approx(
            result["waste_usd"], rel=0.01)
        assert result["waste_usd"] <= ctx.total_cost

    def test_context_under_the_ceiling_is_not_waste(self):
        ctx = ctx_of(FakeCall("claude-haiku-4-5",
                              raw=grade.CONTEXT_CEILING - 1_000, output=2_000))
        keys = {c["key"] for c in (grade.compute(ctx)["components"])}
        assert "oversized_context" not in keys

    def test_a_cheap_model_doing_retrieval_is_not_waste(self):
        # The point of the recommendation is that retrieval on Haiku is correct.
        ctx = ctx_of(*[FakeCall("claude-haiku-4-5", raw=50_000, output=50)
                       for _ in range(10)])
        keys = {c["key"] for c in (grade.compute(ctx)["components"] or [])}
        assert not any("retrieval" in k for k in keys)

    def test_sidechain_context_is_not_charged_as_oversized(self):
        # A subagent's context never sits in the parent's window.
        ctx = ctx_of(FakeCall("claude-opus-5", raw=900_000, output=100,
                              sidechain=True))
        keys = {c["key"] for c in (grade.compute(ctx)["components"] or [])}
        assert "oversized_context" not in keys
