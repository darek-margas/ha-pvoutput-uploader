"""Recorder history into 5 minute samples."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.pvoutput_uploader import builder
from custom_components.pvoutput_uploader.builder import (
    Source,
    _convert,
    _Series,
    async_build_from_states,
    async_build_from_statistics,
    spread_hour,
    unit_supported,
)
from custom_components.pvoutput_uploader.const import (
    AGG_AVERAGE,
    AGG_ENERGY_TODAY,
    AGG_LAST,
    KIND_ENERGY,
    KIND_POWER,
    KIND_RAW,
    KIND_TEMPERATURE,
)


def series(points: list[tuple[float, float | None]], energy: bool = False) -> _Series:
    return _Series(points, energy)


def test_average_is_time_weighted() -> None:
    s = series([(0, 100.0), (150, 200.0)])
    assert s.average(0, 300) == pytest.approx(150.0)


def test_average_skips_unavailable_time() -> None:
    s = series([(0, 100.0), (100, None), (200, 400.0)])
    # 100 W for 100 s and 400 W for 100 s; the unavailable 100 s does not count.
    assert s.average(0, 300) == pytest.approx(250.0)


def test_average_starts_at_first_value_inside_slot() -> None:
    s = series([(60, 300.0)])
    assert s.average(0, 300) == pytest.approx(300.0)
    assert s.average(300, 600) == pytest.approx(300.0)


def test_average_without_values_is_none() -> None:
    assert series([(0, None)]).average(0, 300) is None
    assert series([]).average(0, 300) is None


def test_last_and_changed() -> None:
    s = series([(0, 1.0), (100, 2.0), (200, None)])
    assert s.last(150) == 2.0
    assert s.last(250) is None  # unavailable is not a current reading
    assert s.changed(0, 300)
    assert not s.changed(200, 500)
    assert not s.changed(-100, -1)


def test_delta_counts_increase() -> None:
    s = series([(0, 1000.0), (100, 1010.0), (400, 1030.0)], energy=True)
    assert s.delta(0, 300) == pytest.approx(10.0)
    assert s.delta(300, 600) == pytest.approx(20.0)


def test_delta_handles_meter_reset() -> None:
    # A daily counter drops to almost zero at midnight: the new value is new energy.
    s = series([(0, 5000.0), (100, 5010.0), (200, 10.0), (400, 30.0)], energy=True)
    assert s.delta(0, 300) == pytest.approx(20.0)
    assert s.delta(300, 600) == pytest.approx(20.0)


def test_delta_ignores_small_dips() -> None:
    # A float jitter downwards is not a reset and must not be counted twice.
    s = series([(0, 1000.0), (100, 999.9), (200, 1001.0)], energy=True)
    assert s.delta(0, 300) == pytest.approx(1.0)


def test_delta_with_unavailable_counter() -> None:
    s = series([(0, 100.0), (100, None), (200, 130.0)], energy=True)
    assert s.delta(0, 300) == pytest.approx(30.0)
    assert series([(0, None)], energy=True).delta(0, 300) is None
    # Unavailable for a whole slot: a gap, not zero use.
    gap = series([(0, 100.0), (100, None), (400, 130.0)], energy=True)
    assert gap.delta(0, 300) is None  # no reading in the slot, unavailable at its end
    assert gap.delta(300, 350) is None
    assert gap.delta(350, 600) == pytest.approx(30.0)  # counted when it reports again


def test_units() -> None:
    assert unit_supported(KIND_ENERGY, UnitOfEnergy.KILO_WATT_HOUR)
    assert not unit_supported(KIND_ENERGY, "W")
    assert unit_supported(KIND_POWER, "kW")
    assert unit_supported(KIND_TEMPERATURE, "°F")
    assert unit_supported(KIND_RAW, None)
    assert _convert(1.5, KIND_ENERGY, "kWh") == pytest.approx(1500.0)
    assert _convert(2.0, KIND_POWER, "kW") == pytest.approx(2000.0)
    assert _convert(212.0, KIND_TEMPERATURE, "°F") == pytest.approx(100.0)
    assert _convert(7.0, KIND_RAW, "%") == 7.0
    assert _convert(7.0, KIND_ENERGY, None) == 7.0


def test_source_kind() -> None:
    assert Source("v7", "sensor.x", AGG_ENERGY_TODAY).kind == KIND_ENERGY
    assert Source("v7", "sensor.x", AGG_AVERAGE).kind == KIND_RAW
    assert Source("v5", "sensor.x", AGG_AVERAGE).kind == KIND_TEMPERATURE


def test_spread_hour() -> None:
    samples = spread_hour(7200, {"v1": 1200.0, "v2": 500.0, "v5": None}, {"v1": AGG_ENERGY_TODAY, "v2": AGG_AVERAGE})
    assert [s.ts for s in samples] == list(range(3600 + 300, 7200 + 1, 300))
    assert all(s.values["v1"] == pytest.approx(100.0) for s in samples)
    assert all(s.values["v2"] == 500.0 for s in samples)
    assert all(s.values["v5"] is None for s in samples)


class _FakeRecorder:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _state(entity_id: str, value: str, ts: float) -> State:
    return State(entity_id, value, last_updated=dt_util.utc_from_timestamp(ts))


async def test_build_from_states(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.energy", "1.02", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.power", "300", {"unit_of_measurement": "W"})
    t0 = 1_800_000_000 - 1_800_000_000 % 300
    recorded = {
        # The first state is the one in force at the start of the period.
        "sensor.energy": [
            _state("sensor.energy", "1.000", t0 - 10),
            _state("sensor.energy", "1.010", t0 + 60),
            _state("sensor.energy", "1.020", t0 + 360),
        ],
        "sensor.power": [
            _state("sensor.power", "100", t0 - 5),
            _state("sensor.power", "unavailable", t0 + 1000),
            _state("sensor.power", "300", t0 + 150),
        ],
    }
    recorded["sensor.power"].sort(key=lambda s: s.last_updated)

    def fake_history(hass_, start, end, entity_ids, **kwargs):
        assert kwargs["include_start_time_state"] is True
        assert kwargs["significant_changes_only"] is False
        return recorded

    sources = [
        Source("v1", "sensor.energy", AGG_ENERGY_TODAY),
        Source("v2", "sensor.power", AGG_AVERAGE),
        Source("v4", "sensor.power", AGG_LAST),
    ]
    with (
        patch.object(builder, "get_instance", return_value=_FakeRecorder(hass)),
        patch.object(builder.history, "get_significant_states", side_effect=fake_history),
    ):
        samples = await async_build_from_states(hass, sources, t0, t0 + 1200)

    by_ts = {s.ts: s.values for s in samples}
    # Slot 3 (600-900) has no state change: Home Assistant may have been down, so no row.
    assert sorted(by_ts) == [t0 + 300, t0 + 600, t0 + 1200]
    assert by_ts[t0 + 300]["v1"] == pytest.approx(10.0)
    assert by_ts[t0 + 300]["v2"] == pytest.approx(200.0)
    assert by_ts[t0 + 300]["v4"] == 300.0
    assert by_ts[t0 + 600]["v1"] == pytest.approx(10.0)
    assert by_ts[t0 + 600]["v2"] == pytest.approx(300.0)
    # Unavailable from 1000: only the valid 100 s count, and no last value.
    assert by_ts[t0 + 1200]["v1"] == pytest.approx(0.0)
    assert by_ts[t0 + 1200]["v2"] == pytest.approx(300.0)
    assert by_ts[t0 + 1200]["v4"] is None


async def test_build_from_states_without_sources(hass: HomeAssistant) -> None:
    assert await async_build_from_states(hass, [], 0, 600) == []


async def test_build_from_statistics(hass: HomeAssistant) -> None:
    hour = 1_800_000_000 - 1_800_000_000 % 3600

    def fake_stats(hass_, start, end, ids, period, units, types):
        assert period == "hour"
        assert units["energy"] == "Wh"
        return {
            "sensor.energy": [{"start": float(hour), "change": 500.0}],
            "sensor.power": [
                {"start": float(hour), "mean": 250.0, "state": 300.0},
                {"start": float(hour + 3600), "mean": None, "state": 100.0},
            ],
        }

    sources = [
        Source("v1", "sensor.energy", AGG_ENERGY_TODAY),
        Source("v2", "sensor.power", AGG_AVERAGE),
        Source("v4", "sensor.power", AGG_LAST),
    ]
    with (
        patch.object(builder, "get_instance", return_value=_FakeRecorder(hass)),
        patch.object(builder, "statistics_during_period", side_effect=fake_stats),
    ):
        hours = await async_build_from_statistics(
            hass,
            sources,
            dt_util.utc_from_timestamp(hour),
            dt_util.utc_from_timestamp(hour + 7200),
        )
    assert hours == [
        (hour + 3600, {"v1": 500.0, "v2": 250.0, "v4": 300.0}),
        (hour + 7200, {"v2": 100.0, "v4": 100.0}),
    ]


async def test_unitless_source_uses_configured_unit(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.unitless", "68")
    t0 = 1_800_000_000 - 1_800_000_000 % 300
    recorded = {"sensor.unitless": [_state("sensor.unitless", "68", t0 + 60)]}
    with (
        patch.object(builder, "get_instance", return_value=_FakeRecorder(hass)),
        patch.object(builder.history, "get_significant_states", return_value=recorded),
    ):
        samples = await async_build_from_states(hass, [Source("v5", "sensor.unitless", AGG_LAST, "°F")], t0, t0 + 300)
    assert samples[0].values["v5"] == pytest.approx(20.0)


async def test_unitless_statistics_use_fallback_without_double_conversion(hass: HomeAssistant) -> None:
    hour = 1_800_000_000 - 1_800_000_000 % 3600
    sources = [Source("v2", "sensor.no_unit", AGG_AVERAGE, "kW"), Source("v4", "sensor.with_unit", AGG_AVERAGE, "kW")]
    stats = {s.entity_id: [{"start": float(hour), "mean": 2.0 if s.entity_id == "sensor.no_unit" else 2000.0}] for s in sources}
    metadata = {"sensor.no_unit": (1, {"unit_of_measurement": None}), "sensor.with_unit": (2, {"unit_of_measurement": "kW"})}
    with (
        patch.object(builder, "get_instance", return_value=_FakeRecorder(hass)),
        patch.object(builder, "statistics_during_period", return_value=stats),
        patch.object(builder, "get_metadata", return_value=metadata),
    ):
        hours = await async_build_from_statistics(hass, sources, dt_util.utc_from_timestamp(hour), dt_util.utc_from_timestamp(hour + 3600))
    assert hours == [(hour + 3600, {"v2": 2000.0, "v4": 2000.0})]
