"""USD per million tokens. Edit freely, or pass `--price`. An unknown Claude model falls back to FALLBACK_MODEL;
any other model (codex / OpenAI) has no built-in price and stays unpriced until you give it one.

Cache writes are priced relative to input: 1.25x for the 5 minute TTL, 2x for the 1 hour TTL.
Prices are API list prices, so the result is an "API-equivalent cost" (useful for relative
shares even if you are on a flat subscription).
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_WRITE_5M_FACTOR = 1.25
CACHE_WRITE_1H_FACTOR = 2.0
FALLBACK_MODEL = "claude-sonnet-5-5"


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, 0.25),
    "claude-fable-5": Price(10.0, 50.0, 1.0),
    "claude-opus-5-5": Price(4.0, 20.0, 0.20),
    "claude-opus-5": Price(5.0, 25.0, 0.50),
    "claude-opus-4-8": Price(5.0, 25.0, 0.50),
    "claude-opus-4-7": Price(5.0, 25.0, 0.50),
    "claude-opus-4-6": Price(5.0, 25.0, 0.50),
    "claude-sonnet-5-5": Price(2.0, 10.0, 0.20),
    "claude-sonnet-5": Price(2.0, 10.0, 0.20),
    "claude-sonnet-4-6": Price(3.0, 15.0, 0.30),
    "claude-haiku-4-5": Price(1.0, 5.0, 0.10),
    "claude-haiku-4-5-20251001": Price(1.0, 5.0, 0.10),
    "<synthetic>": Price(0.0, 0.0, 0.0),
}


def parse_price(option: str) -> tuple[str, Price]:
    """`--price MODEL=INPUT,OUTPUT,CACHE_READ` (USD per million tokens), e.g. `gpt-6-sol=1.25,10,0.125`."""
    model, sep, rest = option.partition("=")
    parts = rest.split(",")
    try:
        if not (sep and model.strip() and len(parts) == 3):
            raise ValueError
        inp, out, cached = (float(x) for x in parts)
    except ValueError:
        raise ValueError(f"Expected MODEL=INPUT,OUTPUT,CACHE_READ (USD per million tokens), got {option!r}") from None
    if min(inp, out, cached) < 0:
        raise ValueError(f"Prices must not be negative: {option!r}")
    return model.strip(), Price(inp, out, cached)


def price_rows(extra: dict[str, Price] | None = None) -> list[tuple[str, float, float, float]]:
    """Rows for the `pricing` table: the built-in prices, overridden / extended by `extra`."""
    return [(m, p.input, p.output, p.cache_read) for m, p in {**PRICES, **(extra or {})}.items()]
