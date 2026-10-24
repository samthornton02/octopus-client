"""Off-peak (Economy 7 / Economy 10 style) pricing: flatten day + night rates to windows.

Octopus publishes day and night prices as two series with long validity windows; *when*
the off-peak hours are is a property of the customer's meter and is not in the API. The
rest of a consumer's code prices a reading by finding the single rate window containing
it, so we expand the two series into one series that alternates day/night.

Off-peak hours are given as UTC ``HH:MM-HH:MM`` windows (comma separated for several):
Economy 7 meters keep GMT all year, so e.g. ``00:30-07:30`` is 01:30-08:30 on the clock
in summer. Economy 10 uses three windows, e.g. ``01:00-06:00,13:00-16:00,20:00-22:00``.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

from octopus_client.rates import RateTable, fmt_ts, parse_ts

DEFAULT_OFFPEAK = "00:30-07:30"

Window = tuple[time, time]


def parse_windows(spec: str | list[Window] | None) -> list[Window]:
    """Parse ``"00:30-07:30,13:00-16:00"`` into ``[(time, time), ...]``; raises ValueError."""
    if not spec:
        spec = DEFAULT_OFFPEAK
    if not isinstance(spec, str):
        return list(spec)
    out: list[Window] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            a, b = part.split("-")
            ah, am = a.strip().split(":")
            bh, bm = b.strip().split(":")
            start, end = time(int(ah), int(am)), time(int(bh), int(bm))
        except ValueError as exc:
            raise ValueError(f"Invalid off-peak window {part!r}; expected HH:MM-HH:MM") from exc
        if start == end:
            raise ValueError(f"Off-peak window {part!r} is empty")
        out.append((start, end))
    if not out:
        raise ValueError("No off-peak windows given")
    return out


def format_windows(windows: list[Window]) -> str:
    return ",".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in windows)


def _in_window(t: time, windows: list[Window]) -> bool:
    for a, b in windows:
        if (a <= t < b) if a < b else (t >= a or t < b):  # a > b wraps midnight
            return True
    return False


def expand(day: list[dict[str, Any]], night: list[dict[str, Any]], start: str | datetime,
           windows: str | list[Window] | None = None, until: str | datetime | None = None,
           horizon_days: int = 7) -> list[dict[str, Any]]:
    """Return rate rows (value_inc_vat, valid_from, valid_to) from ``start`` to ``until``
    (default ``horizon_days`` ahead), using the night rate inside ``windows`` and the day
    rate outside. Contiguous windows with the same price are merged."""
    wins = parse_windows(windows)
    lo = parse_ts(start)
    if lo is None:
        return []
    lo = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    hi = (datetime.now(timezone.utc) + timedelta(days=horizon_days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    cap = parse_ts(until)
    if cap is not None:
        hi = min(hi, cap)
    if hi <= lo:
        return []

    # Cut at every daily window edge and every day/night rate change.
    cuts: set[datetime] = {lo, hi}
    d = lo
    while d < hi:
        for a, b in wins:
            cuts.add(datetime.combine(d.date(), a, tzinfo=timezone.utc))
            cuts.add(datetime.combine(d.date(), b, tzinfo=timezone.utc))
        d += timedelta(days=1)
    for r in (*day, *night):
        for k in ("valid_from", "valid_to"):
            t = parse_ts(r.get(k))
            if t is not None:
                cuts.add(t)
    ordered = sorted(c for c in cuts if lo <= c <= hi)

    day_t, night_t = RateTable(day), RateTable(night)
    out: list[dict[str, Any]] = []
    for a, b in zip(ordered, ordered[1:]):
        value = (night_t if _in_window(a.time(), wins) else day_t).at(a)
        if value is None:
            continue
        if out and out[-1]["valid_to"] == fmt_ts(a) and out[-1]["value_inc_vat"] == value:
            out[-1]["valid_to"] = fmt_ts(b)
            continue
        out.append({"valid_from": fmt_ts(a), "valid_to": fmt_ts(b), "value_inc_vat": value})
    return out
