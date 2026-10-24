"""Minimal async client for the public Octopus Energy REST API."""

from octopus_client.client import BASE_URL, OctopusClient
from octopus_client.offpeak import DEFAULT_OFFPEAK, expand, format_windows, parse_windows
from octopus_client.rates import (
    DIRECT_DEBIT,
    NON_DIRECT_DEBIT,
    RateTable,
    clip,
    parse_ts,
    pick_payment_method,
)
from octopus_client.units import GAS_M3_TO_KWH, gas_m3_to_kwh

__all__ = [
    "BASE_URL", "OctopusClient", "GAS_M3_TO_KWH", "gas_m3_to_kwh",
    "RateTable", "clip", "parse_ts", "pick_payment_method", "DIRECT_DEBIT", "NON_DIRECT_DEBIT",
    "DEFAULT_OFFPEAK", "expand", "format_windows", "parse_windows",
]
__version__ = "0.2.0"
