import pytest

from octopus_client import RateTable, clip, parse_ts, pick_payment_method
from octopus_client.offpeak import format_windows, parse_windows


def r(v, vf=None, vt=None, pm=None):
    return {"value_inc_vat": v, "valid_from": vf, "valid_to": vt, "payment_method": pm}


def test_parse_ts_handles_every_octopus_shape():
    z = parse_ts("2026-07-01T00:00:00Z")
    assert parse_ts("2026-07-01T01:00:00+01:00") == z
    assert parse_ts("2026-07-01") == z
    assert parse_ts("2026-07-01T00:00:00") == z
    assert parse_ts(None) is None and parse_ts("") is None


def test_rate_table_picks_latest_start_and_handles_long_early_window():
    t = RateTable([r(1.0), r(2.0, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")])
    assert t.at("2025-12-31T00:00:00Z") == 1.0
    assert t.at("2026-01-15T00:00:00Z") == 2.0
    assert t.at("2026-02-01T00:00:00Z") == 1.0  # short window ended; the long one still applies


def test_rate_table_gap_returns_none_and_end_is_exclusive():
    t = RateTable([r(1.0, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")])
    assert t.at("2026-01-02T00:00:00Z") is None
    assert t.at("2025-12-31T23:59:59Z") is None
    assert not RateTable([])


def test_rate_table_ignores_rows_without_price():
    assert RateTable([{"valid_from": None, "valid_to": None, "value_inc_vat": None}]).at("2026-01-01") is None


def test_clip_edges():
    rows = [r(1.0, None, None)]
    assert clip(rows, "2026-01-01T00:00:00Z", None)[0]["valid_from"] == "2026-01-01T00:00:00Z"
    assert clip(rows, None, "2026-01-01T00:00:00Z")[0]["valid_to"] == "2026-01-01T00:00:00Z"
    assert clip([r(1.0, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")], "2026-01-02T00:00:00Z", None) == []
    assert clip([r(1.0, "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")], None, "2026-01-01T00:00:00Z") == []
    assert clip(rows, None, None)[0]["valid_from"] is None


def test_payment_method_falls_back_when_preferred_absent():
    only_ndd = [r(6.0, "2026-01-01T00:00:00Z", None, "NON_DIRECT_DEBIT")]
    assert pick_payment_method(only_ndd, "DIRECT_DEBIT")[0]["value_inc_vat"] == 6.0
    assert pick_payment_method([r(None)], "DIRECT_DEBIT") == []


def test_parse_windows():
    assert format_windows(parse_windows("00:30-07:30, 13:00-16:00")) == "00:30-07:30,13:00-16:00"
    assert format_windows(parse_windows(None)) == "00:30-07:30"
    for bad in ("nonsense", "07:30-07:30", "25:00-26:00", " , "):
        with pytest.raises(ValueError):
            parse_windows(bad)
