"""Token → USD pricing for canvas-card cost display.

Rates are USD per 1M tokens, grouped by model **family** (the marketing
tier: ``opus`` / ``sonnet`` / ``haiku``). The Claude Code JSONL records
the full model id on each assistant message — we string-match it to a
family rather than maintain a row per concrete model id, because the
rates within a family are flat and new point releases don't change them.

Unknown / missing model strings fall back to ``sonnet`` rates (the most
common case for ambient Claude Code use).
"""
from __future__ import annotations


# USD per 1M tokens. Source: Anthropic's published prompt-caching rates
# (cache write priced at 1.25× input; cache read at 0.1× input).
RATES: dict[str, dict[str, float]] = {
    "opus":   {"r": 15.00, "w": 75.00, "cw": 18.75, "cr": 1.50},
    "sonnet": {"r":  3.00, "w": 15.00, "cw":  3.75, "cr": 0.30},
    "haiku":  {"r":  0.80, "w":  4.00, "cw":  1.00, "cr": 0.08},
}

DEFAULT_FAMILY = "sonnet"


def model_family(model: str | None) -> str:
    """Map a concrete model id like ``claude-opus-4-7-...`` to its family.

    Substring match is intentional — Anthropic appends date/region suffixes
    to model ids over time, and the rates stay flat within a family.
    """
    if not model:
        return DEFAULT_FAMILY
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"


def cost_usd(
    model: str | None,
    cw: int,
    cr: int,
    r: int,
    w: int,
) -> dict[str, float]:
    """Return per-category dollar cost for one usage record.

    Keys mirror the ``TokenUsage`` shape (``cw`` / ``cr`` / ``r`` / ``w``)
    so callers can sum the dicts element-wise across records.
    """
    # ``model_family`` only returns keys we put in ``RATES``, but route
    # through ``.get`` with the default family so a future model-id that
    # doesn't match any branch in ``model_family`` (or a typo in ``RATES``)
    # degrades to sonnet rates instead of throwing KeyError mid-aggregation.
    rates = RATES.get(model_family(model), RATES[DEFAULT_FAMILY])
    return {
        "cw": cw * rates["cw"] / 1_000_000,
        "cr": cr * rates["cr"] / 1_000_000,
        "r":  r  * rates["r"]  / 1_000_000,
        "w":  w  * rates["w"]  / 1_000_000,
    }
