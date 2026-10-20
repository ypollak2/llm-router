"""Wilson 95% interval, Newcombe (1998) method 10 paired CI, exact binomial power helpers (ported from PREREG-Q37). Pure functions."""
from __future__ import annotations

import math
from math import comb

Z = 1.96
POWER_GRID: tuple[float, ...] = (0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60)
_DIGITS = 4


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float, float]:
    """Wilson score interval -> (point, lower, upper); (nan, nan, nan) when n == 0."""
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def newcombe10(a: int, b: int, c: int, d: int) -> tuple[float, float, float]:
    """Paired 2x2: a=both ok, b=A only, c=B only, d=neither. Returns (diff pA-pB, lower, upper). Newcombe 1998b method 10."""
    n = a + b + c + d
    p1, p2 = (a + b) / n, (a + c) / n
    _, l1, u1 = wilson(a + b, n)
    _, l2, u2 = wilson(a + c, n)
    A = (a + b) * (c + d) * (a + c) * (b + d)
    B = a * d - b * c
    C = B - n / 2 if B > n / 2 else (0 if B >= 0 else B)
    phi = 0.0 if A == 0 else C / math.sqrt(A)
    D = p1 - p2
    lo = D - math.sqrt(max(0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2))
    hi = D + math.sqrt(max(0.0, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2))
    return D, lo, hi


def binom_sf(n: int, p: float, k: int) -> float:
    """P(X >= k), X ~ Bin(n, p), exact."""
    return sum(comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))


def kmin(n: int, point: float = 0.80, lb: float = 0.60) -> int | None:
    """smallest k with k/n >= point AND wilson_lb(k, n) >= lb (the D-241 bar); None if unreachable."""
    for k in range(n + 1):
        if k / n >= point - 1e-12 and wilson(k, n)[1] >= lb - 1e-12:
            return k
    return None


def kfail(n: int, point: float = 0.80) -> int | None:
    """largest k whose Wilson UB < point: a POWERED failure (the lane cannot be >= point at 95%); None if none."""
    ks = [k for k in range(n + 1) if wilson(k, n)[2] < point]
    return max(ks) if ks else None



def p_pass(n: int, p: float, point: float = 0.80, lb: float = 0.60) -> float:
    """Probability of passing the bar at true rate p."""
    k = kmin(n, point, lb)
    return binom_sf(n, p, k) if k is not None else 0.0


def p_powered_fail(n: int, p: float, point: float = 0.80) -> float:
    """Probability of a powered failure at true rate p."""
    kf = kfail(n, point)
    if kf is None:
        return 0.0
    return 1.0 - binom_sf(n, p, kf + 1)


def power_table(n: int, ps: tuple[float, ...] = POWER_GRID) -> list[dict[str, float]]:
    return [{"true": p, "P_pass": round(p_pass(n, p), _DIGITS), "P_powered_fail": round(p_powered_fail(n, p), _DIGITS),
             "P_inconclusive": round(1 - p_pass(n, p) - p_powered_fail(n, p), _DIGITS)} for p in ps]
