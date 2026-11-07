"""Token counting. Exact counts come from the agent/API when available; otherwise an estimate.

If `tiktoken` is installed, the o200k_base tokenizer is used as a proxy for all models.
Otherwise a per-script heuristic: Cyrillic ≈ 3 chars/token, Latin ≈ 4, digits ≈ 3, other ≈ 2.
Both are estimates — the report always states which method was used.
"""

from __future__ import annotations

import math

try:  # optional dependency
    import tiktoken  # type: ignore

    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:  # pragma: no cover - depends on environment
    _ENC = None

METHOD = "tiktoken:o200k_base" if _ENC else "heuristic:chars-per-script"


def count(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    cyr = lat = dig = other = 0
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            cyr += 1
        elif ch.isascii() and ch.isalpha():
            lat += 1
        elif ch.isdigit():
            dig += 1
        elif not ch.isspace():
            other += 1
    return math.ceil(cyr / 3.0 + lat / 4.0 + dig / 3.0 + other / 2.0 + text.count(" ") * 0.1)


def cost(tokens_in: int, tokens_out: int, price: dict) -> float | None:
    """price = {"input_per_mtok": $, "output_per_mtok": $}; None if prices are not set."""
    pin, pout = price.get("input_per_mtok"), price.get("output_per_mtok")
    if pin is None or pout is None:
        return None
    return round(tokens_in / 1e6 * pin + tokens_out / 1e6 * pout, 6)
