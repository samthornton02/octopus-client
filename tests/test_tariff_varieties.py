"""Tariff-variety tests: a fake Octopus API serving the shapes each tariff family really returns."""

import asyncio

import httpx
import pytest

from octopus_client import OctopusClient, RateTable

P = "https://api.octopus.energy/v1/products"


def _page(rows, nxt=None):
    return httpx.Response(200, json={"count": len(rows), "next": nxt, "results": rows})


def _rate(v, vf=None, vt=None, pm=None):
    return {"value_inc_vat": v, "valid_from": vf, "valid_to": vt, "payment_method": pm}


def fake_api(routes, calls=None):
    """routes: {url-suffix after /products/: rows | callable | (status, text)}."""

    def handler(req):
        if calls is not None:
            calls.append(str(req.url))
        path = req.url.path.split("/v1/products/")[1]
        hit = routes.get(path)
        if hit is None:
            return httpx.Response(404, json={"detail": "Not found."})
        if callable(hit):
            return hit(req)
        if isinstance(hit, tuple):
            return httpx.Response(hit[0], text=hit[1])
        return _page(hit)

    return OctopusClient("k", transport=httpx.MockTransport(handler), retry_delay=0)


def run(coro):
    return asyncio.run(coro)


def agr(fuel, code, vf=None, vt=None):
    return {"fuel": fuel, "tariff_code": code, "valid_from": vf, "valid_to": vt}


# --- multi-agreement history (the "switched tariff" case) -------------------------------

def test_successive_agreements_never_leak_into_each_other():
    # Every product publishes an open-ended rate; only agreement dates decide who applied.
    def series(price):
        return {"standard-unit-rates/": [_rate(price)], "standing-charges/": [_rate(price * 10)]}

    routes = {}
    for code, price in (("VAR-22-11-01", 10.0), ("OE-FIX-12M-24-11-28", 20.0), ("OE-FIX-12M-25-11-24", 30.0)):
        for suffix, rows in series(price).items():
            routes[f"{code}/electricity-tariffs/E-1R-{code}-C/{suffix}"] = rows
    c = fake_api(routes)
    agreements = [
        agr("electricity", "E-1R-VAR-22-11-01-C", "2022-11-01T00:00:00Z", "2024-11-28T00:00:00Z"),
        agr("electricity", "E-1R-OE-FIX-12M-24-11-28-C", "2024-11-28T00:00:00Z", "2025-11-24T00:00:00Z"),
        agr("electricity", "E-1R-OE-FIX-12M-25-11-24-C", "2025-11-24T00:00:00Z", None),
    ]
    unit, standing = [], []
    for a in agreements:
        got = run(c.fetch_agreement_rates(a, "2022-01-01T00:00:00Z"))
        unit += got["unit"]
        standing += got["standing"]
    ut, st = RateTable(unit), RateTable(standing)
    assert ut.at("2023-06-01T12:00:00Z") == 10.0
    assert ut.at("2024-11-27T23:59:00Z") == 10.0
    assert ut.at("2024-11-28T00:00:00Z") == 20.0
    assert ut.at("2025-11-23T23:59:00Z") == 20.0
    assert ut.at("2025-11-24T00:00:00Z") == 30.0
    assert ut.at("2026-10-24T12:00:00Z") == 30.0
    assert st.at("2025-01-01T12:00:00Z") == 200.0
    assert ut.at("2021-01-01T00:00:00Z") is None  # before any agreement


def test_agreement_that_ended_before_window_is_not_fetched():
    calls = []
    c = fake_api({}, calls)
    got = run(c.fetch_agreement_rates(agr("gas", "G-1R-OLD-20-01-01-C", "2020-01-01", "2021-01-01"),
                                      "2026-01-01T00:00:00Z"))
    assert got == {"unit": [], "standing": [], "warnings": []}
    assert calls == []


def test_agreement_starting_mid_window_is_clipped_and_future_agreement_kept():
    c = fake_api({
        "X-1/gas-tariffs/G-1R-X-1-A/standard-unit-rates/": [_rate(5.0)],
        "X-1/gas-tariffs/G-1R-X-1-A/standing-charges/": [_rate(25.0)],
    })
    got = run(c.fetch_agreement_rates(agr("gas", "G-1R-X-1-A", "2026-12-01T00:00:00Z", None),
                                      "2026-10-01T00:00:00Z"))
    assert got["unit"][0]["valid_from"] == "2026-12-01T00:00:00Z"


# --- single-rate families ---------------------------------------------------------------

def test_agile_half_hourly_paginates_and_keeps_every_window():
    base = "AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-C/"
    pages = {}

    def handler(req):
        if req.url.params.get("page") == "2":
            return _page([_rate(12.0, "2026-10-24T00:30:00Z", "2026-10-24T01:00:00Z")])
        return _page([_rate(10.0, "2026-10-24T00:00:00Z", "2026-10-24T00:30:00Z")],
                     nxt=f"{P}/{base}standard-unit-rates/?page=2")

    c = fake_api({base + "standard-unit-rates/": handler,
                  base + "standing-charges/": [_rate(46.0)]})
    got = run(c.fetch_agreement_rates(agr("electricity", "E-1R-AGILE-24-10-01-C", "2026-01-01"),
                                      "2026-10-24T00:00:00Z"))
    t = RateTable(got["unit"])
    assert t.at("2026-10-24T00:10:00Z") == 10.0
    assert t.at("2026-10-24T00:40:00Z") == 12.0
    assert t.at("2026-10-24T01:10:00Z") is None


def test_time_of_use_windows_with_bst_offsets_are_priced_by_instant():
    # Go-style: cheap 00:30-05:30 UTC. A reading stamped +01:00 must be matched by instant.
    base = "GO-VAR-22-10-14/electricity-tariffs/E-1R-GO-VAR-22-10-14-C/"
    c = fake_api({
        base + "standard-unit-rates/": [
            _rate(8.0, "2026-10-24T00:30:00Z", "2026-10-24T05:30:00Z"),
            _rate(25.0, "2026-10-24T05:30:00Z", "2026-10-25T00:30:00Z"),
            _rate(25.0, "2026-10-23T00:30:00Z", "2026-10-24T00:30:00Z"),
        ],
        base + "standing-charges/": [_rate(40.0)],
    })
    got = run(c.fetch_agreement_rates(agr("electricity", "E-1R-GO-VAR-22-10-14-C"), "2026-10-23T00:00:00Z"))
    t = RateTable(got["unit"])
    assert t.at("2026-10-24T01:30:00+01:00") == 8.0   # = 00:30Z
    assert t.at("2026-10-24T01:00:00+01:00") == 25.0  # = 00:00Z


def test_payment_method_preference():
    base = "VAR-22-11-01/gas-tariffs/G-1R-VAR-22-11-01-C/"
    rows = [_rate(6.0, "2026-04-01T00:00:00Z", None, "NON_DIRECT_DEBIT"),
            _rate(5.5, "2026-04-01T00:00:00Z", None, "DIRECT_DEBIT")]
    c = fake_api({base + "standard-unit-rates/": rows, base + "standing-charges/": rows})
    a = agr("gas", "G-1R-VAR-22-11-01-C")
    dd = run(c.fetch_agreement_rates(a, "2026-01-01T00:00:00Z"))
    ndd = run(c.fetch_agreement_rates(a, "2026-01-01T00:00:00Z", payment_method="NON_DIRECT_DEBIT"))
    assert dd["unit"][0]["value_inc_vat"] == 5.5
    assert ndd["unit"][0]["value_inc_vat"] == 6.0


# --- two-rate families ------------------------------------------------------------------

E7 = "FIX-12M-20-02-12/electricity-tariffs/E-2R-FIX-12M-20-02-12-E/"


def _e7_routes():
    return {
        E7 + "day-unit-rates/": [_rate(12.0, "2026-03-31T23:00:00Z", None)],
        E7 + "night-unit-rates/": [_rate(6.5, "2026-03-31T23:00:00Z", None)],
        E7 + "standing-charges/": [_rate(20.0)],
    }


def test_economy7_day_night_priced_and_standard_endpoint_never_needed():
    calls = []
    c = fake_api(_e7_routes(), calls)
    got = run(c.fetch_agreement_rates(agr("electricity", "E-2R-FIX-12M-20-02-12-E", "2020-02-12"),
                                      "2026-10-22T00:00:00Z", offpeak="00:30-07:30"))
    t = RateTable(got["unit"])
    assert t.at("2026-10-22T00:10:00Z") == 12.0
    assert t.at("2026-10-22T03:00:00Z") == 6.5
    assert t.at("2026-10-22T07:30:00Z") == 12.0
    assert not any("standard-unit-rates" in u for u in calls)
    assert RateTable(got["standing"]).at("2026-10-22T12:00:00Z") == 20.0


def test_economy10_three_windows_and_custom_windows():
    c = fake_api(_e7_routes())
    got = run(c.fetch_agreement_rates(agr("electricity", "E-2R-FIX-12M-20-02-12-E"), "2026-10-22T00:00:00Z",
                                      offpeak="01:00-06:00,13:00-16:00,20:00-22:00"))
    t = RateTable(got["unit"])
    assert [t.at(f"2026-10-22T{h}:00:00Z") for h in ("00", "02", "10", "14", "18", "21", "23")] == \
        [12.0, 6.5, 12.0, 6.5, 12.0, 6.5, 12.0]


def test_offpeak_window_wrapping_midnight():
    c = fake_api(_e7_routes())
    got = run(c.fetch_agreement_rates(agr("electricity", "E-2R-FIX-12M-20-02-12-E"), "2026-10-22T00:00:00Z",
                                      offpeak="22:00-06:00"))
    t = RateTable(got["unit"])
    assert t.at("2026-10-22T05:59:00Z") == 6.5
    assert t.at("2026-10-22T06:00:00Z") == 12.0
    assert t.at("2026-10-22T23:00:00Z") == 6.5


def test_two_rate_detected_even_when_code_says_1r():
    # An unfamiliar code layout: the API's 400 reveals the real register.
    base = "ODD-1/electricity-tariffs/E-1R-ODD-1-A/"
    c = fake_api({
        base + "standard-unit-rates/": (400, '{"detail":"This tariff has day and night rates, not standard."}'),
        base + "day-unit-rates/": [_rate(11.0)],
        base + "night-unit-rates/": [_rate(5.0)],
        base + "standing-charges/": [_rate(30.0)],
    })
    got = run(c.fetch_agreement_rates(agr("electricity", "E-1R-ODD-1-A"), "2026-10-22T00:00:00Z"))
    t = RateTable(got["unit"])
    assert (t.at("2026-10-22T12:00:00Z"), t.at("2026-10-22T03:00:00Z")) == (11.0, 5.0)


def test_single_rate_detected_even_when_code_says_2r():
    base = "ODD-2/electricity-tariffs/E-2R-ODD-2-A/"
    c = fake_api({
        base + "day-unit-rates/": (400, '{"detail":"This tariff has standard rates."}'),
        base + "standard-unit-rates/": [_rate(14.0)],
        base + "standing-charges/": [_rate(30.0)],
    })
    got = run(c.fetch_agreement_rates(agr("electricity", "E-2R-ODD-2-A"), "2026-10-22T00:00:00Z"))
    assert RateTable(got["unit"]).at("2026-10-22T12:00:00Z") == 14.0


def test_two_rate_prices_respect_a_mid_period_rate_change():
    base = "FIX-A/electricity-tariffs/E-2R-FIX-A-A/"
    c = fake_api({
        base + "day-unit-rates/": [_rate(15.0, None, "2026-10-23T00:00:00Z"), _rate(12.0, "2026-10-23T00:00:00Z", None)],
        base + "night-unit-rates/": [_rate(9.0, None, "2026-10-23T00:00:00Z"), _rate(6.0, "2026-10-23T00:00:00Z", None)],
        base + "standing-charges/": [_rate(20.0)],
    })
    got = run(c.fetch_agreement_rates(agr("electricity", "E-2R-FIX-A-A"), "2026-10-22T00:00:00Z"))
    t = RateTable(got["unit"])
    assert t.at("2026-10-22T12:00:00Z") == 15.0
    assert t.at("2026-10-22T03:00:00Z") == 9.0
    assert t.at("2026-10-23T12:00:00Z") == 12.0
    assert t.at("2026-10-23T03:00:00Z") == 6.0


# --- failure handling -------------------------------------------------------------------

def test_missing_unit_rates_is_reported_not_silent():
    base = "GONE-1/electricity-tariffs/E-1R-GONE-1-A/"
    c = fake_api({base + "standing-charges/": [_rate(30.0)]})
    got = run(c.fetch_agreement_rates(agr("electricity", "E-1R-GONE-1-A"), "2026-10-22T00:00:00Z"))
    assert got["unit"] == []
    assert any("no unit rates" in w for w in got["warnings"])


def test_retries_rate_limit_then_succeeds():
    attempts = {"n": 0}

    def flaky(req):
        attempts["n"] += 1
        return httpx.Response(429) if attempts["n"] < 3 else _page([_rate(7.0)])

    base = "R-1/gas-tariffs/G-1R-R-1-A/"
    c = fake_api({base + "standard-unit-rates/": flaky, base + "standing-charges/": [_rate(1.0)]})
    got = run(c.fetch_agreement_rates(agr("gas", "G-1R-R-1-A"), "2026-10-22T00:00:00Z"))
    assert got["unit"][0]["value_inc_vat"] == 7.0
    assert attempts["n"] == 3


def test_persistent_server_error_raises():
    base = "R-2/gas-tariffs/G-1R-R-2-A/"
    c = fake_api({base + "standard-unit-rates/": (503, "down"), base + "standing-charges/": [_rate(1.0)]})
    with pytest.raises(httpx.HTTPStatusError):
        run(c.fetch_agreement_rates(agr("gas", "G-1R-R-2-A"), "2026-10-22T00:00:00Z"))


def test_resolve_tariffs_reports_meter_point():
    acct = {"properties": [{"electricity_meter_points": [
        {"mpan": "M1", "agreements": [{"tariff_code": "E-1R-A-1-C", "valid_from": "2024-01-01", "valid_to": None}]}]}]}
    c = OctopusClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=acct)))
    assert run(c.resolve_tariffs("A-1"))[0]["meter_point"] == "M1"
