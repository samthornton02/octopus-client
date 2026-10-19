"""Unit conversions shared by every consumer, so gas figures can never drift apart again."""

from __future__ import annotations

# Volume correction factor and a typical UK calorific value (MJ/m³); 3.6 converts MJ to kWh.
VOLUME_CORRECTION = 1.02264
DEFAULT_CALORIFIC_VALUE = 40.0

# kWh per m³ at the default calorific value (≈ 11.3627).
GAS_M3_TO_KWH = VOLUME_CORRECTION * DEFAULT_CALORIFIC_VALUE / 3.6


def gas_m3_to_kwh(m3: float, calorific_value: float = DEFAULT_CALORIFIC_VALUE) -> float:
    """Convert a SMETS gas volume in m³ to kWh: m³ × volume correction × CV ÷ 3.6."""
    return m3 * VOLUME_CORRECTION * calorific_value / 3.6
