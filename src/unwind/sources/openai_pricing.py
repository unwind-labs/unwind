"""Standard API-equivalent token estimates for local Codex sessions.

Rates checked 2026-10-06 against https://developers.openai.com/api/docs/pricing
and https://developers.openai.com/api/docs/models/<model-id> for older models.
Cache-write policy: https://developers.openai.com/api/docs/guides/prompt-caching

These are current list-price estimates, not historical invoices or ChatGPT
subscription charges. Service tiers, regional uplifts, tool fees, and discounts
are excluded. Unknown models stay unpriced instead of inheriting another rate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenRates:
    """USD per million tokens, with an optional long-context threshold."""

    input: float
    cached: float
    output: float
    cache_write: float | None = None
    long_context_threshold: int | None = None


# GPT-5.6 Sol's published promotional price lasts at least until 2026-11-21.
# Earlier models have no cache-write premium: writes use the input rate.
RATES = {
    "gpt-6.1-sol": TokenRates(2, 0.10, 10, 2.50, 272_000),
    "gpt-6-astra": TokenRates(10, 1, 50, 12.50, 272_000),
    "gpt-6-sol": TokenRates(2, 0.20, 10, 2.50, 272_000),
    "gpt-6-luna": TokenRates(0.10, 0.01, 0.50, 0.125, 272_000),
    "gpt-5.6-sol": TokenRates(4, 0.40, 20, 5, 272_000),
    "gpt-5.6-terra": TokenRates(2, 0.20, 12, 2.50, 272_000),
    "gpt-5.6-luna": TokenRates(0.20, 0.02, 1.20, 0.25, 272_000),
    "gpt-5.5": TokenRates(5, 0.50, 30, long_context_threshold=272_000),
    "gpt-5.4": TokenRates(2.50, 0.25, 15, long_context_threshold=272_000),
    "gpt-5.4-mini": TokenRates(0.75, 0.075, 4.50),
    "gpt-5.4-nano": TokenRates(0.20, 0.02, 1.25),
    "gpt-5.3-codex": TokenRates(1.75, 0.175, 14),
    "gpt-5.2-codex": TokenRates(1.75, 0.175, 14),
    "gpt-5.2": TokenRates(1.75, 0.175, 14),
    "gpt-5.1-codex-max": TokenRates(1.25, 0.125, 10),
    "gpt-5.1-codex-mini": TokenRates(0.25, 0.025, 2),
    "gpt-5.1-codex": TokenRates(1.25, 0.125, 10),
    "gpt-5.1": TokenRates(1.25, 0.125, 10),
    "gpt-5-codex": TokenRates(1.25, 0.125, 10),
    "gpt-5": TokenRates(1.25, 0.125, 10),
    "codex-mini-latest": TokenRates(1.50, 0.375, 6),
}
ALIASES = {"gpt-5.6": "gpt-5.6-sol"}
_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def cost_usd(
    model: str | None, cw: int, cr: int, r: int, w: int
) -> dict[str, float] | None:
    """Price disjoint counters for one usage event at Standard rates.

    Accept exact model IDs and their dated snapshots, never arbitrary prefixes
    such as a future Pro or Spark variant. Input includes cache reads and writes
    when selecting the context tier; output tokens do not affect that tier.
    """
    model_id = _SNAPSHOT_SUFFIX.sub("", model or "")
    rates = RATES.get(ALIASES.get(model_id, model_id))
    if rates is None:
        return None
    long_context = (
        rates.long_context_threshold is not None
        and r + cr + cw > rates.long_context_threshold
    )
    input_multiplier = 2 if long_context else 1
    output_multiplier = 1.5 if long_context else 1
    write_rate = rates.input if rates.cache_write is None else rates.cache_write
    return {
        "cw": cw * write_rate * input_multiplier / 1_000_000,
        "cr": cr * rates.cached * input_multiplier / 1_000_000,
        "r": r * rates.input * input_multiplier / 1_000_000,
        "w": w * rates.output * output_multiplier / 1_000_000,
    }
