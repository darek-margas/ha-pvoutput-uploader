"""Sensors for PVOutput Uploader: one per configured field, plus status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    AGG_ENERGY_TODAY,
    CONF_SYSTEM_ID,
    FIELDS_BY_KEY,
    KIND_ENERGY,
    KIND_POWER,
    KIND_TEMPERATURE,
    KIND_VOLTAGE,
)
from .coordinator import PVOutputConfigEntry, PVOutputCoordinator
from .entity import PVOutputEntity
from .uploader import FieldMapping, Uploader


def _ts(value: float | None) -> datetime | None:
    return dt_util.utc_from_timestamp(value) if value else None


@dataclass(frozen=True, kw_only=True)
class PVOutputSensorDescription(SensorEntityDescription):
    """Describes a status sensor."""

    value_fn: Callable[[Uploader], Any]


SENSORS = (
    PVOutputSensorDescription(
        key="last_upload",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.last_upload,
    ),
    PVOutputSensorDescription(
        key="pending_samples",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("pending"),
    ),
    PVOutputSensorDescription(
        key="queued_outputs",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("queued_outputs"),
    ),
    PVOutputSensorDescription(
        key="failed_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("failed"),
    ),
    PVOutputSensorDescription(
        key="partial_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("partial"),
    ),
    PVOutputSensorDescription(
        key="failed_outputs",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("failed_outputs"),
    ),
    PVOutputSensorDescription(
        key="history_samples",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.summary.get("total"),
    ),
    PVOutputSensorDescription(
        key="history_oldest",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: _ts(u.summary.get("oldest")),
    ),
    PVOutputSensorDescription(
        key="rate_limit_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: u.client.rate_remaining,
    ),
    PVOutputSensorDescription(
        key="paused_until",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: _ts(u.paused_until)
        if u.paused_until and u.paused_until > dt_util.utcnow().timestamp()
        else None,
    ),
    PVOutputSensorDescription(
        key="account_type",
        device_class=SensorDeviceClass.ENUM,
        options=["donor", "standard"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: "donor" if u.donor else "standard",
    ),
    PVOutputSensorDescription(
        key="last_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda u: (u.last_error or "")[:255] or None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PVOutputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the field and status sensors."""
    coordinator = entry.runtime_data
    fields = [
        FieldSensor(coordinator, mapping) for mapping in coordinator.uploader.mappings
    ]
    # Drop the sensors of fields that were removed.
    wanted = {sensor.unique_id for sensor in fields}
    prefix = f"{entry.data[CONF_SYSTEM_ID]}_field_"
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.unique_id.startswith(prefix) and reg_entry.unique_id not in wanted:
            registry.async_remove(reg_entry.entity_id)
    async_add_entities(
        [
            *fields,
            *(PVOutputSensor(coordinator, description) for description in SENSORS),
        ]
    )


class PVOutputSensor(PVOutputEntity, SensorEntity):
    """A status value of the uploader."""

    entity_description: PVOutputSensorDescription

    def __init__(self, coordinator, description: PVOutputSensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """The uploader keeps its own state, so the sensors stay available."""
        return True

    @property
    def native_value(self) -> Any:
        """Return the value."""
        return self.entity_description.value_fn(self.coordinator.uploader)


# What a field's 5 minute values are recorded in.
RECORDED_UNITS = {
    KIND_ENERGY: "Wh per 5 minutes",
    KIND_POWER: UnitOfPower.WATT,
    KIND_TEMPERATURE: UnitOfTemperature.CELSIUS,
    KIND_VOLTAGE: UnitOfElectricPotential.VOLT,
}


def _number(value: float) -> str:
    return f"{value:g}"


class FieldSensor(PVOutputEntity, SensorEntity):
    """A configured PVOutput field: what is sent, and where it comes from.

    The state is the source sensor's friendly name, or the fixed value.
    """

    def __init__(self, coordinator: PVOutputCoordinator, mapping: FieldMapping) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"field_{mapping.field}")
        self._attr_translation_key = None
        self._attr_name = f"{mapping.field} {mapping.name}"
        self.mapping = mapping

    @property
    def available(self) -> bool:
        """The configuration is always known."""
        return True

    @property
    def _configured_to_send(self) -> bool:
        """Active and allowed by the account type.

        Not whether uploading is switched on, nor whether PVOutput accepted it.
        """
        return self.mapping in self.coordinator.uploader.active_mappings

    @property
    def icon(self) -> str:
        """Show whether the field is configured to be sent."""
        return "mdi:upload" if self._configured_to_send else "mdi:upload-off"

    @property
    def native_value(self) -> str:
        """Return a readable source name, or the fixed value."""
        if entity_id := self.mapping.entity_id:
            if (state := self.hass.states.get(entity_id)) is not None:
                return state.name[:255]
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if registry_entry is not None:
                name = registry_entry.name or registry_entry.original_name
                if name:
                    return name[:255]
            return entity_id.split(".", 1)[-1].replace("_", " ")[:255]
        return _number(self.mapping.constant or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe how the field is sent."""
        m = self.mapping
        field = FIELDS_BY_KEY[m.field]
        uploader = self.coordinator.uploader
        attrs: dict[str, Any] = {
            "pvoutput_field": m.field,
            "pvoutput_field_name": field.name,
            "source": "sensor" if m.entity_id else "fixed value",
            "active": m.active,
            "configured_to_send": self._configured_to_send,
        }
        if m.active and not self._configured_to_send:
            attrs["not_configured_to_send_because"] = "needs a PVOutput donor account"
        if m.entity_id:
            attrs["source_entity"] = m.entity_id
            if (state := self.hass.states.get(m.entity_id)) is not None:
                attrs["source_name"] = state.name
            attrs["value_per_interval"] = m.aggregate
            if (latest := uploader.latest.get(m.field)) is not None:
                ts, value = latest
                attrs["last_recorded"] = dt_util.utc_from_timestamp(ts).isoformat()
                attrs["last_recorded_value"] = round(value, 3)
                kind = KIND_ENERGY if m.aggregate == AGG_ENERGY_TODAY else field.kind
                if (unit := RECORDED_UNITS.get(kind)) is not None:
                    attrs["last_recorded_unit"] = unit
        return attrs
