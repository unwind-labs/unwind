"""Published Standard rates and model matching for Codex estimates."""

import pytest

from unwind.sources.openai_pricing import cost_usd


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gpt-6.1-sol", {"cw": 0.025, "cr": 0.001, "r": 0.02, "w": 0.10}),
        ("gpt-6-astra", {"cw": 0.125, "cr": 0.01, "r": 0.10, "w": 0.50}),
        ("gpt-5.6-sol", {"cw": 0.05, "cr": 0.004, "r": 0.04, "w": 0.20}),
        ("gpt-5.3-codex", {"cw": 0.0175, "cr": 0.00175, "r": 0.0175, "w": 0.14}),
        ("gpt-5.1-codex-mini", {"cw": 0.0025, "cr": 0.00025, "r": 0.0025, "w": 0.02}),
    ],
)
def test_disjoint_token_categories(model, expected):
    assert cost_usd(model, 10_000, 10_000, 10_000, 10_000) == pytest.approx(expected)


@pytest.mark.parametrize(
    "model", ["gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.4"]
)
def test_context_boundary_counts_cached_input_but_not_output(model):
    boundary = cost_usd(model, 100_000, 100_000, 72_000, 1_000_000)
    over = cost_usd(model, 100_000, 100_000, 72_001, 1_000_000)
    assert over["cw"] == pytest.approx(2 * boundary["cw"])
    assert over["cr"] == pytest.approx(2 * boundary["cr"])
    assert over["r"] / 72_001 == pytest.approx(2 * boundary["r"] / 72_000)
    assert over["w"] == pytest.approx(1.5 * boundary["w"])
    # A million output tokens at the boundary still use the short-context rate.
    assert boundary["w"] == cost_usd(model, 0, 0, 0, 1_000_000)["w"]


def test_codex_and_mini_models_do_not_inherit_flagship_context_tier():
    for model in ("gpt-5.3-codex", "gpt-5.4-mini"):
        assert cost_usd(model, 0, 0, 300_000, 10) == pytest.approx(
            {k: 2 * v for k, v in cost_usd(model, 0, 0, 150_000, 5).items()}
        )


def test_dated_snapshots_and_explicit_alias():
    assert cost_usd("gpt-5.4-2026-03-05", 0, 10, 20, 30) == cost_usd(
        "gpt-5.4", 0, 10, 20, 30
    )
    assert cost_usd("gpt-5.6", 0, 10, 20, 30) == cost_usd("gpt-5.6-sol", 0, 10, 20, 30)


@pytest.mark.parametrize(
    "model", [None, "", "gpt-test", "gpt-5.4-pro", "gpt-5.3-codex-spark", "gpt-7-sol"]
)
def test_unknown_models_do_not_receive_a_guessed_rate(model):
    assert cost_usd(model, 10, 20, 30, 40) is None


def test_known_model_without_usage_costs_zero():
    assert cost_usd("gpt-6.1-sol", 0, 0, 0, 0) == {"cw": 0, "cr": 0, "r": 0, "w": 0}
