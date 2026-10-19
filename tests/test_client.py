import asyncio

import httpx
import pytest

from octopus_client import GAS_M3_TO_KWH, OctopusClient, gas_m3_to_kwh

ACCOUNT = {
    "properties": [
        {
            "electricity_meter_points": [
                {
                    "mpan": "111",
                    "meters": [{"serial_number": "E1"}],
                    "agreements": [
                        {"tariff_code": "E-1R-VAR-22-11-01-C", "valid_from": "2022-01-01", "valid_to": "2023-01-01"},
                        {"tariff_code": "E-1R-FIX-23-01-01-C", "valid_from": "2023-01-01", "valid_to": None},
                    ],
                },
                {"mpan": "999", "is_export": True, "meters": [{"serial_number": "X1"}],
                 "agreements": [{"tariff_code": "E-1R-OUTGOING-C"}]},
            ],
            "gas_meter_points": [
                {"mprn": "222", "meters": [{"serial_number": "G1"}],
                 "agreements": [{"tariff_code": "G-1R-VAR-22-11-01-C"}]},
            ],
        }
    ]
}


def _client(handler):
    return OctopusClient("key", transport=httpx.MockTransport(handler))


def test_resolve_meters_skips_export():
    c = _client(lambda req: httpx.Response(200, json=ACCOUNT))
    meters = asyncio.run(c.resolve_meters("A-1"))
    assert meters == [
        {"fuel": "electricity", "mpan": "111", "serial": "E1"},
        {"fuel": "gas", "mprn": "222", "serial": "G1"},
    ]


def test_resolve_tariffs_keeps_all_agreements_and_skips_export():
    c = _client(lambda req: httpx.Response(200, json=ACCOUNT))
    codes = [t["tariff_code"] for t in asyncio.run(c.resolve_tariffs("A-1"))]
    assert codes == ["E-1R-VAR-22-11-01-C", "E-1R-FIX-23-01-01-C", "G-1R-VAR-22-11-01-C"]


def test_basic_auth_uses_key_as_username():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={})

    asyncio.run(_client(handler).resolve_meters("A-1"))
    assert seen["auth"] == "Basic a2V5Og=="  # base64("key:")


def test_product_code():
    assert OctopusClient.product_code("E-1R-VAR-22-11-01-C") == "VAR-22-11-01"
    assert OctopusClient.product_code("SHORT") == "SHORT"


def test_fetch_consumption_paginates_and_passes_group_by():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(200, json={"results": [{"consumption": 1}],
                                             "next": "https://api.octopus.energy/v1/page2"})
        return httpx.Response(200, json={"results": [{"consumption": 2}], "next": None})

    rows = asyncio.run(
        _client(handler).fetch_consumption({"fuel": "gas", "mprn": "222", "serial": "G1"},
                                           "2026-01-01T00:00:00Z", group_by="day")
    )
    assert [r["consumption"] for r in rows] == [1, 2]
    assert "/gas-meter-points/222/meters/G1/consumption/" in str(calls[0].url)
    assert calls[0].url.params["group_by"] == "day"
    assert "group_by" not in calls[1].url.params  # next URL is used verbatim


def test_fetch_rates_tolerates_404_for_one_kind():
    def handler(req):
        if "standing-charges" in str(req.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"results": [{"value_inc_vat": 24.5}], "next": None})

    out = asyncio.run(_client(handler).fetch_rates("electricity", "E-1R-VAR-22-11-01-C", "2026-01-01"))
    assert out["unit"] == [{"value_inc_vat": 24.5}]
    assert out["standing"] == []


def test_fetch_consumption_raises_on_http_error():
    c = _client(lambda req: httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(c.fetch_consumption({"fuel": "electricity", "mpan": "1", "serial": "s"}, "x"))


def test_gas_conversion():
    assert GAS_M3_TO_KWH == pytest.approx(11.3627, abs=1e-3)
    assert gas_m3_to_kwh(10) == pytest.approx(113.63, abs=0.01)
    assert gas_m3_to_kwh(10, calorific_value=39.5) < gas_m3_to_kwh(10)
