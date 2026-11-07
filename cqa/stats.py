"""Small statistics toolkit (no dependencies): Wilson intervals (SP.2), Cohen's κ (M.3, G.5), bootstrap
confidence intervals (ST.1), exact McNemar test (ST.2), Pearson correlation (RL.4)."""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Callable, Optional, Sequence


def wilson(k: int, n: int, z: float = 1.96) -> Optional[tuple]:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def proportion(k: int, n: int, z: float = 1.96) -> dict:
    return {"k": k, "n": n, "share": round(k / n, 4) if n else None, "ci95": wilson(k, n, z)}


def kappa(pairs: Sequence[tuple]) -> Optional[float]:
    """Cohen's κ for a list of (a, b) labels."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if pe >= 1:
        return 1.0 if po == 1 else 0.0
    return round((po - pe) / (1 - pe), 4)


def bootstrap(items: Sequence, statistic: Callable[[Sequence], Optional[float]], samples: int = 1000,
              seed: int = 7) -> Optional[tuple]:
    """Percentile 95% interval of `statistic` over resamples of `items` (e.g. conversations)."""
    if not items:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(samples):
        res = statistic([items[rng.randrange(len(items))] for _ in items])
        if res is not None:
            vals.append(res)
    if not vals:
        return None
    vals.sort()
    return round(vals[int(0.025 * (len(vals) - 1))], 4), round(vals[int(0.975 * (len(vals) - 1))], 4)


def mcnemar(b: int, c: int) -> Optional[float]:
    """Exact two-sided McNemar p-value: b = only A correct, c = only B correct."""
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 4)


def mean(xs: Sequence[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None
