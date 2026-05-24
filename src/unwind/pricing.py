"""Token → USD pricing for canvas-card cost display.

Rates are USD per 1M tokens, grouped by model **family** (the marketing
tier: ``opus`` / ``sonnet`` / ``haiku``). The Claude Code JSONL records
the full model id on each assistant message — we string-match it to a
family rather than maintain a row per concrete model id, because the
rates within a family are flat and new point releases don't change them.

Sonnet has a second tier for prompts whose input context exceeds 200K
tokens (~2× the base rate). We detect that per-record from the token
counts on the event itself, not the model id — Anthropic prices any
Sonnet 4.x prompt at the premium rate once it crosses 200K.

Unknown / missing model strings fall back to ``sonnet`` rates (the most
common case for ambient Claude Code use).
"""
from __future__ import annotations


# USD per 1M tokens. Source: Anthropic's published prompt-caching rates
# (cache write priced at 1.25× input for the 5-minute TTL that Claude
# Code uses; cache read at 0.1× input).
#
# ``sonnet_200k`` is the >200K-context tier for Sonnet 4.x (input $6,
# output $22.50). Opus and Haiku currently have no tiered pricing.
# Haiku rates here are Haiku **4.5** (released Oct 2025) — the model
# Claude Code actually invokes today.
RATES: dict[str, dict[str, float]] = {
    "opus":        {"r": 15.00, "w": 75.00,  "cw": 18.75, "cr": 1.50},
    "sonnet":      {"r":  3.00, "w": 15.00,  "cw":  3.75, "cr": 0.30},
    "sonnet_200k": {"r":  6.00, "w": 22.50,  "cw":  7.50, "cr": 0.60},
    "haiku":       {"r":  1.00, "w":  5.00,  "cw":  1.25, "cr": 0.10},
}

DEFAULT_FAMILY = "sonnet"

# Sonnet's premium tier kicks in once the prompt (uncached input +
# cache-read + cache-write) crosses this threshold.
SONNET_200K_THRESHOLD = 200_000


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
    so callers can sum the dicts element-wise across records. Output
    tokens (``w``) don't count toward the Sonnet tier threshold — the
    threshold is on prompt context size, not completion size.
    """
    fam = model_family(model)
    # Sonnet 4.x flips to the premium row once the prompt crosses 200K.
    # (Sonnet 3.x has a 200K context cap, so this branch never fires for
    # those older ids — no special-case needed.)
    if fam == "sonnet" and cw + cr + r > SONNET_200K_THRESHOLD:
        fam = "sonnet_200k"
    # ``.get`` with the default family so a future model-id that doesn't
    # match any branch in ``model_family`` (or a typo in ``RATES``)
    # degrades to sonnet rates instead of throwing KeyError mid-aggregation.
    rates = RATES.get(fam, RATES[DEFAULT_FAMILY])
    return {
        "cw": cw * rates["cw"] / 1_000_000,
        "cr": cr * rates["cr"] / 1_000_000,
        "r":  r  * rates["r"]  / 1_000_000,
        "w":  w  * rates["w"]  / 1_000_000,
    }
