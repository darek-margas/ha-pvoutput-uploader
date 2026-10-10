"""Turn recorder history into 5 minute samples."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import logging
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    ElectricPotentialConverter,
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
)

from .const import (
    AGG_AVERAGE,
    AGG_ENERGY_TODAY,
    AGG_LAST,
    FIELDS_BY_KEY,
    KIND_ENERGY,
    KIND_POWER,
    KIND_RAW,
    KIND_TEMPERATURE,
    KIND_VOLTAGE,
    LOG_INTERVAL,
)
from .store import PENDING, Sample

_LOGGER = logging.getLogger(__name__)

CONVERTERS: dict[str, tuple[type[BaseUnitConverter], str]] = {
    KIND_ENERGY: (EnergyConverter, UnitOfEnergy.WATT_HOUR),
    KIND_POWER: (PowerConverter, UnitOfPower.WATT),
    KIND_TEMPERATURE: (TemperatureConverter, UnitOfTemperature.CELSIUS),
    KIND_VOLTAGE: (ElectricPotentialConverter, UnitOfElectricPotential.VOLT),
}
STATISTICS_UNITS = {kind: unit for kind, (_, unit) in CONVERTERS.items()}

# A drop of more than this fraction in an energy counter is a meter reset.
RESET_DROP = 0.9


@dataclass(frozen=True, slots=True)
class Source:
    """A sensor feeding one PVOutput field."""

    field: str
    entity_id: str
    aggregate: str
    unit: str | None = None

    @property
    def kind(self) -> str:
        """Return the unit kind the sensor is converted to."""
        if self.aggregate == AGG_ENERGY_TODAY:
            return KIND_ENERGY
        return FIELDS_BY_KEY[self.field].kind


def unit_supported(kind: str, unit: str | None) -> bool:
    """Return whether a sensor unit can be converted for a field kind."""
    if kind == KIND_RAW:
        return True
    converter, _ = CONVERTERS[kind]
    return unit in converter.VALID_UNITS


def _convert(value: float, kind: str, unit: str | None) -> float:
    if kind == KIND_RAW or unit is None:
        return value
    converter, target = CONVERTERS[kind]
    if unit == target:
        return value
    try:
        return converter.convert(value, unit, target)
    except HomeAssistantError:
        return value


class _Series:
    """Step function of one sensor's values over time."""

    def __init__(self, points: list[tuple[float, float | None]], energy: bool) -> None:
        self.ts = [p[0] for p in points]
        self.vals = [p[1] for p in points]
        # For counters, a running total that ignores resets and small dips.
        self.acc: list[float | None] = []
        if energy:
            total = 0.0
            prev: float | None = None
            for v in self.vals:
                if v is not None:
                    if prev is None:
                        prev = v
                    elif v >= prev:
                        total += v - prev
                        prev = v
                    elif v < prev * RESET_DROP:
                        total += v
                        prev = v
                self.acc.append(total if prev is not None else None)

    def _index(self, t: float) -> int:
        return bisect_right(self.ts, t) - 1

    def changed(self, a: float, b: float) -> bool:
        """Return whether the sensor reported a value in (a, b]."""
        i = bisect_right(self.ts, a)
        return i < len(self.ts) and self.ts[i] <= b

    def last(self, b: float) -> float | None:
        """Return the value in force at b; None while the sensor is unavailable."""
        i = self._index(b)
        return self.vals[i] if i >= 0 else None

    def average(self, a: float, b: float) -> float | None:
        """Return the time weighted mean over [a, b)."""
        n = len(self.ts)
        i = max(self._index(a), 0)
        total = covered = 0.0
        while i < n and self.ts[i] < b:
            start = max(self.ts[i], a)
            end = min(self.ts[i + 1], b) if i + 1 < n else b
            v = self.vals[i]
            if v is not None and end > start:
                total += v * (end - start)
                covered += end - start
            i += 1
        return total / covered if covered else None

    def delta(self, a: float, b: float) -> float | None:
        """Return the energy counted over (a, b].

        None when the counter gave no reading in the slot and is unavailable at
        its end: that is a gap, not zero use. The energy of the gap is counted
        when the counter reports again.
        """
        i, j = self._index(a), self._index(b)
        end = self.acc[j] if j >= 0 else None
        if end is None:
            return None
        if self.vals[j] is None and not any(
            v is not None for v in self.vals[i + 1 : j + 1]
        ):
            return None
        start = self.acc[i] if i >= 0 else None
        return end - (start or 0.0)


def _state_ts(state: Any) -> float:
    ts = getattr(state, "last_updated_timestamp", None)
    return ts if ts is not None else state.last_updated.timestamp()


def _unit(hass: HomeAssistant, entity_id: str) -> str | None:
    if (state := hass.states.get(entity_id)) is None:
        return None
    return state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)


async def async_build_from_states(
    hass: HomeAssistant, sources: list[Source], start: int, end: int
) -> list[Sample]:
    """Build samples for the 5 minute slots ending in (start, end].

    A slot gets a row only if at least one sensor reported in it, so periods
    when Home Assistant was down are left empty.
    """
    if not sources or end <= start:
        return []
    entity_ids = sorted({s.entity_id for s in sources})
    states: dict[str, list[Any]] = await get_instance(hass).async_add_executor_job(
        partial(
            history.get_significant_states,
            hass,
            dt_util.utc_from_timestamp(start),
            dt_util.utc_from_timestamp(end),
            entity_ids,
            include_start_time_state=True,
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=True,
        )
    )

    series: dict[Source, _Series] = {}
    for source in sources:
        unit = _unit(hass, source.entity_id) or source.unit
        points: list[tuple[float, float | None]] = []
        for state in states.get(source.entity_id, []):
            value: float | None = None
            if state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                try:
                    value = _convert(float(state.state), source.kind, unit)
                except ValueError:
                    value = None
            points.append((max(_state_ts(state), float(start)), value))
        series[source] = _Series(points, source.aggregate == AGG_ENERGY_TODAY)

    samples: list[Sample] = []
    for b in range(start + LOG_INTERVAL, end + 1, LOG_INTERVAL):
        a = b - LOG_INTERVAL
        if not any(s.changed(a, b) for s in series.values()):
            continue
        values: dict[str, float | None] = {}
        for source, s in series.items():
            if source.aggregate == AGG_ENERGY_TODAY:
                values[source.field] = s.delta(a, b)
            elif source.aggregate == AGG_LAST:
                values[source.field] = s.last(b)
            else:
                values[source.field] = s.average(a, b)
        samples.append(Sample(b, values, PENDING))
    return samples


async def async_build_from_statistics(
    hass: HomeAssistant, sources: list[Source], start: datetime, end: datetime
) -> list[tuple[int, dict[str, float | None]]]:
    """Return hourly values from long term statistics as (hour end, values)."""
    if not sources:
        return []
    ids = {s.entity_id for s in sources}
    stats: dict[str, list[dict[str, Any]]] = await get_instance(
        hass
    ).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        ids,
        "hour",
        STATISTICS_UNITS,
        {"change", "mean", "state"},
    )
    # Statistics with a recorded unit have already been converted by the recorder.
    # Apply the explicit fallback only to statistics whose metadata has no unit.
    overrides = {s.entity_id for s in sources if s.unit}
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids=overrides)
    ) if overrides else {}
    hours: dict[int, dict[str, float | None]] = {}
    for source in sources:
        for row in stats.get(source.entity_id, []):
            begin = row["start"]
            if isinstance(begin, datetime):
                begin = begin.timestamp()
            if source.aggregate == AGG_ENERGY_TODAY:
                value = row.get("change")
            elif source.aggregate == AGG_AVERAGE:
                value = row.get("mean")
                if value is None:
                    value = row.get("state")
            else:
                value = row.get("state")
                if value is None:
                    value = row.get("mean")
            if value is not None:
                meta = metadata.get(source.entity_id)
                if source.unit and meta and not meta[1].get("unit_of_measurement"):
                    value = _convert(value, source.kind, source.unit)
                hours.setdefault(int(begin) + 3600, {})[source.field] = value
    return sorted(hours.items())


def spread_hour(
    hour_end: int, values: dict[str, float | None], aggregates: dict[str, str]
) -> list[Sample]:
    """Split an hourly statistics row into twelve 5 minute samples."""
    slots = 3600 // LOG_INTERVAL
    samples = []
    for n in range(slots):
        ts = hour_end - 3600 + (n + 1) * LOG_INTERVAL
        row: dict[str, float | None] = {}
        for field, value in values.items():
            if value is None:
                row[field] = None
            elif aggregates.get(field) == AGG_ENERGY_TODAY:
                row[field] = value / slots
            else:
                row[field] = value
        samples.append(Sample(ts, row, PENDING))
    return samples
