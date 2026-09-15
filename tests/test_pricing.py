"""Cost arithmetic and the model-family mapping.

The report's entire value is that its numbers are right, so these tests pin the
arithmetic to hand-computed figures rather than to the implementation.
"""

from datetime import date

import pytest

import pricing


class TestFamilyOf:
    @pytest.mark.parametrize("model,family", [
        ("claude-opus-5", "opus"),
        ("claude-opus-4-8", "opus"),
        ("claude-sonnet-5", "sonnet"),
        ("claude-haiku-4-5-20251001", "haiku"),
        ("claude-fable-5-1", "fable"),
        ("us.anthropic.claude-opus-4-8-v1:0", "opus"),
        ("CLAUDE-SONNET-5", "sonnet"),
    ])
    def test_maps_real_model_ids(self, model, family):
        assert pricing.family_of(model) == family

    @pytest.mark.parametrize("model", ["", None, "gpt-4", "unknown-model"])
    def test_unrecognised_ids_are_unknown(self, model):
        assert pricing.family_of(model) == "unknown"

    def test_unknown_family_falls_back_to_sonnet_pricing(self):
        # A new model id must not be free: an unpriced call silently costing
        # nothing would understate the total and hide the very waste the tool
        # is looking for. Sonnet is the mid-tier, so it is the safe default.
        assert pricing.prices_for("some-future-model") == (3.0, 15.0)


class TestCostOf:
    def test_raw_input_and_output(self):
        # 1M raw input at $5 + 1M output at $25 = $30 for Opus.
        assert pricing.cost_of("claude-opus-5", 1_000_000, 0, 0, 0, 1_000_000) == 30.0

    def test_cache_write_5m_costs_1_25x_input(self):
        cost = pricing.cost_of("claude-opus-5", 0, 1_000_000, 0, 0, 0)
        assert cost == pytest.approx(5.0 * 1.25)

    def test_cache_write_1h_costs_2x_input(self):
        cost = pricing.cost_of("claude-opus-5", 0, 0, 1_000_000, 0, 0)
        assert cost == pytest.approx(5.0 * 2.0)

    def test_cache_read_costs_0_1x_input(self):
        cost = pricing.cost_of("claude-opus-5", 0, 0, 0, 1_000_000, 0)
        assert cost == pytest.approx(5.0 * 0.10)

    def test_all_components_sum(self):
        # Every bucket at 1M tokens, Sonnet at $3/$15:
        #   raw 3.00 + write5m 3.75 + write1h 6.00 + read 0.30 + output 15.00
        cost = pricing.cost_of(
            "claude-sonnet-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        assert cost == pytest.approx(28.05)

    def test_zero_usage_is_free(self):
        assert pricing.cost_of("claude-opus-5", 0, 0, 0, 0, 0) == 0.0

    def test_haiku_is_cheaper_than_sonnet_is_cheaper_than_opus(self):
        # The report's core recommendation is "use a cheaper model", which is
        # only sound if the ordering holds.
        usage = (100_000, 0, 0, 0, 10_000)
        haiku = pricing.cost_of("claude-haiku-4-5", *usage)
        sonnet = pricing.cost_of("claude-sonnet-5", *usage)
        opus = pricing.cost_of("claude-opus-5", *usage)
        assert haiku < sonnet < opus


class TestCostWithoutCache:
    def test_cached_tokens_rebill_at_full_input_rate(self):
        # 900k read + 100k write would all have been raw input: 1M at $3.
        actual = pricing.cost_without_cache(
            "claude-sonnet-5", 0, 100_000, 0, 900_000, 0)
        assert actual == pytest.approx(3.0)

    def test_is_never_cheaper_than_the_cached_cost(self):
        # Caching cannot lose money; if this inverts, the savings figure the
        # report prints would be negative.
        args = ("claude-opus-5", 5_000, 20_000, 0, 500_000, 2_000)
        assert pricing.cost_without_cache(*args) >= pricing.cost_of(*args)

    def test_matches_cost_of_when_nothing_was_cached(self):
        args = ("claude-opus-5", 50_000, 0, 0, 0, 5_000)
        assert pricing.cost_without_cache(*args) == pytest.approx(
            pricing.cost_of(*args))


class TestSanityBounds:
    """The import-time guard against a stale price from an older generation."""

    def test_current_table_passes_its_own_bounds(self):
        for fam, (inp, out) in pricing.FAMILY_PRICES.items():
            lo, hi = pricing._SANE_INPUT_RANGE[fam]
            assert lo <= inp <= hi
            assert 3.0 <= out / inp <= 7.0

    def test_claude_3_era_opus_price_would_be_rejected(self):
        # $15/Mtok input was Claude 3 Opus. Pasting it back in must fail loudly
        # rather than tripling every figure in the report.
        lo, hi = pricing._SANE_INPUT_RANGE["opus"]
        assert not (lo <= 15.0 <= hi)

    def test_every_priced_family_has_a_tier(self):
        assert set(pricing.FAMILY_PRICES) <= set(pricing.FAMILY_TIER)


class TestStaleness:
    def test_as_of_is_a_valid_date(self):
        date.fromisoformat(pricing.PRICES_AS_OF)

    def test_fresh_table_is_not_stale_and_stamps_the_date(self):
        asof = date.fromisoformat(pricing.PRICES_AS_OF)
        note = pricing.staleness_note(asof)
        assert not pricing.is_stale(asof)
        assert pricing.PRICES_AS_OF in note
        assert "re-verify" not in note

    def test_becomes_stale_after_the_threshold(self):
        asof = date.fromisoformat(pricing.PRICES_AS_OF)
        just_after = asof.toordinal() + pricing.STALENESS_WARNING_DAYS + 1
        assert pricing.is_stale(date.fromordinal(just_after))

    def test_not_stale_exactly_on_the_threshold(self):
        asof = date.fromisoformat(pricing.PRICES_AS_OF)
        on_day = asof.toordinal() + pricing.STALENESS_WARNING_DAYS
        assert not pricing.is_stale(date.fromordinal(on_day))

    def test_stale_note_tells_the_reader_to_re_verify(self):
        asof = date.fromisoformat(pricing.PRICES_AS_OF)
        late = date.fromordinal(asof.toordinal() + 400)
        note = pricing.staleness_note(late)
        assert "re-verify" in note
        assert "claude.com/pricing" in note

    def test_age_is_measured_in_days(self):
        asof = date.fromisoformat(pricing.PRICES_AS_OF)
        assert pricing.pricing_age_days(
            date.fromordinal(asof.toordinal() + 30)) == 30
