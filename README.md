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

## Pricing a tariff history (v0.2)

```python
for agreement in await client.resolve_tariffs("A-XXXXXXXX"):
    priced = await client.fetch_agreement_rates(
        agreement, "2026-01-01T00:00:00Z",
        offpeak="00:30-07:30",            # Economy 7; E10: "01:00-06:00,13:00-16:00,20:00-22:00"
        payment_method="DIRECT_DEBIT",
    )
    priced["unit"], priced["standing"], priced["warnings"]
```

`fetch_agreement_rates` returns flat `{valid_from, valid_to, value_inc_vat}` windows:

- **clipped to the agreement's own dates**, so a customer who switched tariff is never
  priced with another product's rates;
- **single-rate** (fixed, variable, Tracker, Agile, Go, Intelligent Go, Cosy, Flux, gas) and
  **two-rate** (Economy 7/10) handled — the register comes from the tariff code and is
  verified against the API, falling back to the other layout on a 400;
- one price per window for the chosen payment method;
- 429/5xx retried; missing rate series come back as `warnings` rather than silently empty.

Look rates up with `RateTable(rows).at(instant)`. It compares **instants**, so readings
stamped `+01:00` (BST) match rates stamped `Z` correctly — never compare the raw strings.

Off-peak hours are not in the API; they're UTC windows you supply (meters stay on GMT year
round). Not covered: Intelligent Octopus smart-charge dispatches (GraphQL) and export tariffs.

## Develop

```
pip install -e ".[test]" && pytest
```

Out of scope: GraphQL / Saving Sessions (stays in corvus), persistence, and anything
account-specific.
