"""Pure (no I/O) helpers for turning Octopus rate payloads into clean, time-windowed prices.

Everything here works on instants, never on string comparison: Octopus mixes ``Z`` and
``+01:00`` offsets between endpoints, and comparing those lexically is wrong by an hour
whenever the clocks are on BST.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any, Iterable

_FMT = "%Y-%m-%dT%H:%M:%SZ"
_MIN = datetime.min.replace(tzinfo=timezone.utc)
_MAX = datetime.max.replace(tzinfo=timezone.utc)

DIRECT_DEBIT = "DIRECT_DEBIT"
NON_DIRECT_DEBIT = "NON_DIRECT_DEBIT"


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse an Octopus timestamp (``Z``, ``+01:00``, naive or date-only) to an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_FMT)


def pick_payment_method(rows: Iterable[dict[str, Any]], preferred: str = DIRECT_DEBIT) -> list[dict[str, Any]]:
    """Collapse rows that share a ``valid_from`` to one, preferring ``preferred`` payment
    method (rows with no payment method are neutral). Rows without a price are dropped."""
    preferred = (preferred or DIRECT_DEBIT).upper()
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("value_inc_vat") is None:
            continue
        key = r.get("valid_from") or ""
        cur = best.get(key)
        if cur is None:
            best[key] = r
        elif (r.get("payment_method") or "").upper() == preferred and (cur.get("payment_method") or "").upper() != preferred:
            best[key] = r
    return list(best.values())


def clip(rows: Iterable[dict[str, Any]], start: str | datetime | None, end: str | datetime | None) -> list[dict[str, Any]]:
    """Intersect each rate window with ``[start, end)``; windows that fall outside are dropped.

    Used to confine a product's published rates to the dates a customer was actually on it,
    so successive agreements can never leak rates into each other's periods.
    """
    lo, hi = parse_ts(start), parse_ts(end)
    out: list[dict[str, Any]] = []
    for r in rows:
        vf, vt = parse_ts(r.get("valid_from")), parse_ts(r.get("valid_to"))
        new_from = max([d for d in (vf, lo) if d is not None], default=None)
        new_to = min([d for d in (vt, hi) if d is not None], default=None)
        if new_from is not None and new_to is not None and new_to <= new_from:
            continue
        out.append({**r,
                    "valid_from": fmt_ts(new_from) if new_from is not None else None,
                    "valid_to": fmt_ts(new_to) if new_to is not None else None})
    return out


class RateTable:
    """Instant -> price lookup over (possibly overlapping) rate windows.

    Where windows overlap, the one that started latest wins (matches how a newer price
    supersedes an older one). Lookups are O(log n), so pricing a few years of half-hourly
    readings against a half-hourly tariff stays cheap.
    """

    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        wins = []
        for r in rows:
            if r.get("value_inc_vat") is None:
                continue
            wins.append((parse_ts(r.get("valid_from")) or _MIN, parse_ts(r.get("valid_to")) or _MAX,
                         float(r["value_inc_vat"])))
        wins.sort(key=lambda w: w[0])
        self._wins = wins
        self._starts = [w[0] for w in wins]
        # Longest-reaching end among windows up to each index lets us stop scanning early.
        self._prefix_max_end: list[datetime] = []
        m = _MIN
        for w in wins:
            m = max(m, w[1])
            self._prefix_max_end.append(m)

    def __bool__(self) -> bool:
        return bool(self._wins)

    def at(self, when: str | datetime) -> float | None:
        instant = parse_ts(when)
        if instant is None:
            return None
        i = bisect_right(self._starts, instant) - 1
        while i >= 0:
            if self._prefix_max_end[i] <= instant:
                return None  # nothing at or before i reaches this instant
            vf, vt, val = self._wins[i]
            if vf <= instant < vt:
                return val
            i -= 1
        return None
