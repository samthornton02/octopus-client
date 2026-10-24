"""Minimal Octopus Energy REST client: discover meters + tariffs, fetch rates and consumption."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from octopus_client.offpeak import expand
from octopus_client.rates import DIRECT_DEBIT, clip, fmt_ts, parse_ts, pick_payment_method

BASE_URL = "https://api.octopus.energy/v1"

# E-1R-VAR-22-11-01-C -> register "1R", product "VAR-22-11-01" (trailing letter = GSP region).
_TARIFF_RE = re.compile(r"^[EG]-(\d+R)-(.+)-[A-Z]$")


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
        retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._auth = httpx.BasicAuth(api_key, "")
        self._timeout = timeout
        self._transport = transport
        self._retries = retries
        self._retry_delay = retry_delay

    def _http(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=self._auth, timeout=timeout or self._timeout, transport=self._transport
        )

    async def _account(self, account_number: str) -> dict[str, Any]:
        async with self._http() as client:
            r = await self._get(client, f"{BASE_URL}/accounts/{account_number}/")
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
        Each entry carries its ``meter_point`` (MPAN/MPRN) so callers can spot accounts
        with more than one meter point per fuel.
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
                                        "valid_from": a.get("valid_from"), "valid_to": a.get("valid_to"),
                                        "meter_point": emp.get("mpan")})
            for gmp in prop.get("gas_meter_points", []):
                for a in gmp.get("agreements", []):
                    if a.get("tariff_code"):
                        tariffs.append({"fuel": "gas", "tariff_code": a["tariff_code"],
                                        "valid_from": a.get("valid_from"), "valid_to": a.get("valid_to"),
                                        "meter_point": gmp.get("mprn")})
        return tariffs

    @staticmethod
    def product_code(tariff_code: str) -> str:
        """Derive a product code from a tariff code.

        Tariff codes look like ``E-1R-VAR-22-11-01-C`` — energy letter, register count,
        the product code, then a one-letter GSP region. Anything else is returned as-is.
        """
        m = _TARIFF_RE.match(tariff_code)
        return m.group(2) if m else tariff_code

    @staticmethod
    def register(tariff_code: str) -> str | None:
        """``"1R"`` (single rate), ``"2R"`` (day/night, e.g. Economy 7/10) or None if unknown."""
        m = _TARIFF_RE.match(tariff_code)
        return m.group(1) if m else None

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with retries on rate limiting (429) and transient server errors (5xx)."""
        for attempt in range(self._retries + 1):
            r = await client.get(url, params=params)
            if r.status_code != 429 and r.status_code < 500:
                return r
            if attempt < self._retries:
                wait = float(r.headers.get("Retry-After", 0) or 0) or self._retry_delay * 2**attempt
                await asyncio.sleep(min(wait, 30))
        return r

    async def _series(self, client: httpx.AsyncClient, base: str, path: str, period_from: str) -> list[dict[str, Any]] | None:
        """All pages of one rate series; None if the endpoint doesn't exist (404)."""
        rows: list[dict[str, Any]] = []
        url: str | None = f"{base}/{path}/"
        params: dict[str, Any] | None = {"period_from": period_from, "page_size": 1500}
        while url:
            r = await self._get(client, url, params)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            rows.extend(data.get("results", []))
            url = data.get("next")
            params = None
        return rows

    async def fetch_rates(self, fuel: str, tariff_code: str, period_from: str) -> dict[str, Any]:
        """Fetch unit rates (p/kWh) and standing charges (p/day) for one tariff.

        Returns ``{"unit", "standing", "day", "night", "register", "warnings"}``. Single-rate
        tariffs (fixed, variable, Tracker, Agile, Go, Cosy, gas …) fill ``unit``; two-rate
        tariffs (Economy 7/10) fill ``day`` and ``night`` instead and ``register`` is ``"2R"``.
        The register is taken from the tariff code but verified against the API: if Octopus
        answers 400 ("has day and night rates, not standard", or vice-versa) the other layout
        is tried, so an unfamiliar tariff code can't silently lose its prices.
        """
        product = self.product_code(tariff_code)
        seg = "electricity-tariffs" if fuel == "electricity" else "gas-tariffs"
        base = f"{BASE_URL}/products/{product}/{seg}/{tariff_code}"
        reg = self.register(tariff_code)
        layouts = ["1R"] if fuel == "gas" else (["2R", "1R"] if reg == "2R" else ["1R", "2R"])
        out: dict[str, Any] = {"unit": [], "standing": [], "day": [], "night": [], "register": None, "warnings": []}
        async with self._http(30) as client:
            standing = await self._series(client, base, "standing-charges", period_from)
            if standing is None:
                out["warnings"].append(f"{tariff_code}: no standing charges published")
            out["standing"] = standing or []
            for i, layout in enumerate(layouts):
                try:
                    if layout == "1R":
                        unit = await self._series(client, base, "standard-unit-rates", period_from)
                        out["unit"] = unit or []
                        got = unit
                    else:
                        day = await self._series(client, base, "day-unit-rates", period_from)
                        night = await self._series(client, base, "night-unit-rates", period_from)
                        out["day"], out["night"] = day or [], night or []
                        got = day if (day and night) else None
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 400 and i < len(layouts) - 1:
                        continue
                    raise
                out["register"] = layout
                if not got:
                    out["warnings"].append(f"{tariff_code}: no unit rates published")
                break
        return out

    async def fetch_agreement_rates(
        self,
        agreement: dict[str, Any],
        period_from: str,
        *,
        offpeak: str | None = None,
        payment_method: str = DIRECT_DEBIT,
    ) -> dict[str, Any]:
        """Priced, ready-to-store rate windows for one tariff agreement.

        Returns ``{"unit", "standing", "warnings"}`` where every row is
        ``{valid_from, valid_to, value_inc_vat}`` clipped to the agreement's own dates (so a
        customer's successive tariffs never overlap), one price per window for the chosen
        ``payment_method``, and — for Economy 7/10 — day/night flattened using ``offpeak``
        (UTC ``HH:MM-HH:MM[,…]`` windows, see :mod:`octopus_client.offpeak`).
        """
        agr_from, agr_to = agreement.get("valid_from"), agreement.get("valid_to")
        pf = parse_ts(period_from)
        start = max([d for d in (pf, parse_ts(agr_from)) if d is not None])
        end = parse_ts(agr_to)
        empty: dict[str, Any] = {"unit": [], "standing": [], "warnings": []}
        if end is not None and end <= start:
            return empty  # agreement ended before the window we care about
        rr = await self.fetch_rates(agreement["fuel"], agreement["tariff_code"], fmt_ts(start))
        if rr["register"] == "2R":
            unit = expand(pick_payment_method(rr["day"], payment_method),
                          pick_payment_method(rr["night"], payment_method),
                          start, offpeak, until=end)
        else:
            unit = clip(pick_payment_method(rr["unit"], payment_method), start, end)
        standing = clip(pick_payment_method(rr["standing"], payment_method), start, end)
        return {"unit": unit, "standing": standing, "warnings": rr["warnings"]}

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
                r = await self._get(client, next_url, params)
                r.raise_for_status()
                data = r.json()
                rows.extend(data.get("results", []))
                next_url = data.get("next")  # already a fully-formed URL with its own querystring
                params = None
        return rows
