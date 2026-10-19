# octopus-client

Minimal async client for the **public** Octopus Energy REST API, shared by
[corvus](https://github.com/samthornton02/corvus) and
[energy-monitor](https://github.com/samthornton02/energy-monitor) so there is one
implementation of meter/tariff discovery, rate fetching, pagination and the gas m³ → kWh
conversion. It holds no secrets and no personal data.

```python
from octopus_client import OctopusClient, gas_m3_to_kwh

client = OctopusClient(api_key)
meters = await client.resolve_meters("A-XXXXXXXX")
rows = await client.fetch_consumption(meters[0], "2026-01-01T00:00:00Z", group_by="day")
```

Gas rows from SMETS2 meters are in m³; use `gas_m3_to_kwh` (m³ × 1.02264 × CV ÷ 3.6, default
CV 40 MJ/m³ ≈ 11.3627 kWh/m³) so every consumer reports the same kWh.

## Install (pin a tag)

```
octopus-client @ git+https://github.com/samthornton02/octopus-client.git@v0.1.0
```

## Develop

```
pip install -e ".[test]" && pytest
```

Out of scope: GraphQL / Saving Sessions (stays in corvus), persistence, and anything
account-specific.
