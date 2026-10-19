"""Minimal Octopus Energy REST client: discover meters + tariffs, fetch rates and consumption."""

from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://api.octopus.energy/v1"


class OctopusClient:
    """Reads data for a single Octopus account using its API key.

    Auth is HTTP Basic with the API key as the username and a blank password,
    exactly as Octopus documents. ``transport`` is for tests only.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = httpx.BasicAuth(api_key, "")
        self._timeout = timeout
        self._transport = transport

    def _http(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=self._auth, timeout=timeout or self._timeout, transport=self._transport
        )

    async def _account(self, account_number: str) -> dict[str, Any]:
        async with self._http() as client:
            r = await client.get(f"{BASE_URL}/accounts/{account_number}/")
            r.raise_for_status()
            return r.json()

    async def resolve_meters(self, account_number: str) -> list[dict[str, Any]]:
        """Discover every (non-export) electricity + gas meter on the account."""
        data = await self._account(account_number)
        meters: list[dict[str, Any]] = []
        for prop in data.get("properties", []):
            for emp in prop.get("electricity_meter_points", []):
                if emp.get("is_export"):
                    continue  # skip solar export meters — we want consumption
                mpan = emp.get("mpan")
                for meter in emp.get("meters", []):
                    meters.append({"fuel": "electricity", "mpan": mpan, "serial": meter.get("serial_number")})
            for gmp in prop.get("gas_meter_points", []):
                mprn = gmp.get("mprn")
                for meter in gmp.get("meters", []):
                    meters.append({"fuel": "gas", "mprn": mprn, "serial": meter.get("serial_number")})
        return meters

    async def resolve_tariffs(self, account_number: str) -> list[dict[str, Any]]:
        """Discover the tariff agreements (code + validity window) for each fuel.

        An account can switch tariff over time, so a meter point may list several
        agreements — we keep them all so historical cost uses the rate in force then.
        """
        data = await self._account(account_number)
        tariffs: list[dict[str, Any]] = []
        for prop in data.get("properties", []):
            for emp in prop.get("electricity_meter_points", []):
                if emp.get("is_export"):
                    continue
                for a in emp.get("agreements", []):
                    if a.get("tariff_code"):
                        tariffs.append({"fuel": "electricity", "tariff_code": a["tariff_code"],
                                        "valid_from": a.get("valid_from"), "valid_to": a.get("valid_to")})
            for gmp in prop.get("gas_meter_points", []):
                for a in gmp.get("agreements", []):
                    if a.get("tariff_code"):
                        tariffs.append({"fuel": "gas", "tariff_code": a["tariff_code"],
                                        "valid_from": a.get("valid_from"), "valid_to": a.get("valid_to")})
        return tariffs

    @staticmethod
    def product_code(tariff_code: str) -> str:
        """Derive a product code from a tariff code.

        Tariff codes look like ``E-1R-VAR-22-11-01-C`` — two leading tokens (energy +
        register) and a trailing GSP letter wrap the product code in the middle.
        """
        parts = tariff_code.split("-")
        return "-".join(parts[2:-1]) if len(parts) > 3 else tariff_code

    async def fetch_rates(self, fuel: str, tariff_code: str, period_from: str) -> dict[str, list[dict[str, Any]]]:
        """Fetch unit rates (p/kWh) and standing charges (p/day) for one tariff.

        Returns ``{"unit": [...], "standing": [...]}`` of rows with value_inc_vat and a
        valid_from/valid_to window. Standard tariffs return a few windows; Agile returns
        half-hourly — either way every window is returned so callers can match by time.
        """
        product = self.product_code(tariff_code)
        seg = "electricity-tariffs" if fuel == "electricity" else "gas-tariffs"
        out: dict[str, list[dict[str, Any]]] = {"unit": [], "standing": []}
        async with self._http(30) as client:
            for kind, path in (("unit", "standard-unit-rates"), ("standing", "standing-charges")):
                url: str | None = f"{BASE_URL}/products/{product}/{seg}/{tariff_code}/{path}/"
                params: dict[str, Any] | None = {"period_from": period_from, "page_size": 1500}
                while url:
                    r = await client.get(url, params=params)
                    if r.status_code == 404:
                        break  # some products aren't individually addressable — skip gracefully
                    r.raise_for_status()
                    data = r.json()
                    out[kind].extend(data.get("results", []))
                    url = data.get("next")
                    params = None
        return out

    async def fetch_consumption(
        self, meter: dict[str, Any], period_from: str, *, group_by: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch consumption rows for one meter from ``period_from`` (ISO8601) onward.

        Half-hourly by default; pass ``group_by="day"`` (or hour/week/month/quarter) to
        have Octopus aggregate. Gas rows are in the meter's native unit (m³ for most
        SMETS2 meters) — convert with :func:`octopus_client.gas_m3_to_kwh`.
        """
        if meter["fuel"] == "electricity":
            url = f"{BASE_URL}/electricity-meter-points/{meter['mpan']}/meters/{meter['serial']}/consumption/"
        else:
            url = f"{BASE_URL}/gas-meter-points/{meter['mprn']}/meters/{meter['serial']}/consumption/"

        params: dict[str, Any] | None = {"period_from": period_from, "page_size": 25000, "order_by": "period"}
        if group_by:
            params["group_by"] = group_by
        rows: list[dict[str, Any]] = []
        async with self._http(30) as client:
            next_url: str | None = url
            while next_url:
                r = await client.get(next_url, params=params)
                r.raise_for_status()
                data = r.json()
                rows.extend(data.get("results", []))
                next_url = data.get("next")  # already a fully-formed URL with its own querystring
                params = None
        return rows
