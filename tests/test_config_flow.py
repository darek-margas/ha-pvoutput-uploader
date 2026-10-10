"""Config flow and the field menu of the options flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvoutput_uploader.api import (
    PVOutputAuthError,
    PVOutputConnectionError,
    SystemInfo,
)
from custom_components.pvoutput_uploader.config_flow import DEFAULT_OPTIONS
from custom_components.pvoutput_uploader.const import (
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_FIELDS,
    CONF_SYSTEM_ID,
    CONF_SYSTEM_NAME,
    DOMAIN,
)

GET_SYSTEM = "custom_components.pvoutput_uploader.config_flow.PVOutputClient.get_system"
SETUP_ENTRY = "custom_components.pvoutput_uploader.async_setup_entry"


@pytest.fixture
def setup_entry():
    with patch(SETUP_ENTRY, return_value=True) as mock:
        yield mock


@pytest.fixture
def sensors(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outside", "20", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.power", "300", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.energy", "1.5", {"unit_of_measurement": "kWh"})


def options_entry(
    hass: HomeAssistant, donor: bool = False, fields: list[dict[str, Any]] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234",
        title="My System",
        data={
            "api_key": "key",
            CONF_SYSTEM_ID: "1234",
            CONF_SYSTEM_NAME: "My System",
            CONF_DETECTED_INTERVAL: 5,
            CONF_DETECTED_DONOR: donor,
        },
        options=DEFAULT_OPTIONS | {CONF_FIELDS: fields or []},
    )
    entry.add_to_hass(hass)
    return entry


def select_options(result: FlowResult, key: str) -> list[str]:
    for marker, selector in result["data_schema"].schema.items():
        if marker == key:
            return [
                o["value"] if isinstance(o, dict) else o
                for o in selector.config["options"]
            ]
    raise KeyError(key)


async def test_user_flow(recorder_mock, hass: HomeAssistant, setup_entry) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 10, True))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_key": "key", "system_id": " 1234 "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My System"
    assert result["data"] == {
        "api_key": "key",
        CONF_SYSTEM_ID: "1234",
        CONF_SYSTEM_NAME: "My System",
        CONF_DETECTED_INTERVAL: 10,
        CONF_DETECTED_DONOR: True,
    }
    assert result["options"] == DEFAULT_OPTIONS
    assert result["result"].unique_id == "1234"


@pytest.mark.parametrize(
    ("error", "key"),
    [(PVOutputAuthError("no"), "invalid_auth"), (PVOutputConnectionError("down"), "cannot_connect")],
)
async def test_user_flow_errors(
    recorder_mock, hass: HomeAssistant, setup_entry, error: Exception, key: str
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(GET_SYSTEM, AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_key": "key", "system_id": "1234"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}


async def test_user_flow_already_configured(
    recorder_mock, hass: HomeAssistant, setup_entry
) -> None:
    options_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_key": "key", "system_id": "1234"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def _menu(hass: HomeAssistant, entry: MockConfigEntry) -> FlowResult:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return result


async def _choose(hass: HomeAssistant, result: FlowResult, step: str) -> FlowResult:
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def _configure(hass: HomeAssistant, result: FlowResult, data: dict[str, Any]) -> FlowResult:
    return await hass.config_entries.options.async_configure(result["flow_id"], data)


async def _add(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> FlowResult:
    """Open the menu, choose Add field and the kind of source."""
    result = await _choose(hass, await _menu(hass, entry), "add_field")
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["add_sensor", "add_constant"]
    return await _choose(hass, result, kind)


async def test_menu(recorder_mock, hass: HomeAssistant) -> None:
    entry = options_entry(hass)
    result = await _menu(hass, entry)
    assert result["menu_options"] == ["add_field", "reupload", "import_history", "settings"]
    assert "No fields yet" in result["description_placeholders"]["fields"]


async def test_add_sensor_field(recorder_mock, hass: HomeAssistant, sensors) -> None:
    entry = options_entry(hass)
    result = await _add(hass, entry, "add_sensor")
    assert result["type"] is FlowResultType.FORM
    assert select_options(result, "field") == ["v1", "v2", "v3", "v4", "v5", "v6"]  # no donor fields

    result = await _configure(hass, result, {"entity_id": "sensor.outside", "field": "v5"})
    assert result["step_id"] == "sensor_details"
    assert result["description_placeholders"]["entity"] == "sensor.outside"
    result = await _configure(hass, result, {"name": "Outside", "active": True, "aggregate": "average"})
    # Saved at once.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FIELDS] == [
        {
            "field": "v5", "entity_id": "sensor.outside", "constant": None,
            "name": "Outside", "active": True, "aggregate": "average",
        },
    ]

    # The menu now lists it, and the used field is no longer offered.
    result = await _menu(hass, entry)
    assert result["menu_options"] == [
        "add_field", "edit_field", "remove_field", "reupload", "import_history", "settings"
    ]
    assert "Outside" in result["description_placeholders"]["fields"]
    result = await _add(hass, entry, "add_sensor")
    assert "v5" not in select_options(result, "field")
    result = await _configure(hass, result, {"entity_id": "sensor.power", "field": "v2"})
    result = await _configure(hass, result, {"name": "Power", "active": False, "aggregate": "last"})
    assert [m["field"] for m in entry.options[CONF_FIELDS]] == ["v5", "v2"]
    assert entry.options[CONF_FIELDS][1]["active"] is False


async def test_add_constant_field(recorder_mock, hass: HomeAssistant) -> None:
    entry = options_entry(hass, donor=True)
    result = await _add(hass, entry, "add_constant")
    offered = select_options(result, "field")
    assert "v12" in offered
    assert "b3" in offered

    # The name defaults to PVOutput's name for the field.
    result = await _configure(hass, result, {"field": "b3", "constant": 5544, "active": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FIELDS] == [
        {
            "field": "b3", "entity_id": None, "constant": 5544,
            "name": "Battery Size", "active": True, "aggregate": "last",
        }
    ]


async def test_unit_checks(recorder_mock, hass: HomeAssistant, sensors) -> None:
    entry = options_entry(hass)
    result = await _add(hass, entry, "add_sensor")
    result = await _configure(hass, result, {"entity_id": "sensor.power", "field": "v5"})
    assert result["errors"] == {"base": "unit_mismatch"}
    result = await _configure(hass, result, {"entity_id": "sensor.energy", "field": "v1"})
    assert result["step_id"] == "sensor_details"


async def test_extended_energy_needs_energy_unit(
    recorder_mock, hass: HomeAssistant, sensors
) -> None:
    entry = options_entry(hass, donor=True)
    result = await _add(hass, entry, "add_sensor")
    result = await _configure(hass, result, {"entity_id": "sensor.power", "field": "v10"})
    result = await _configure(hass, result, {"name": "V2L", "active": True, "aggregate": "energy_today"})
    assert result["errors"] == {"base": "unit_mismatch"}
    result = await _configure(hass, result, {"name": "V2L", "active": True, "aggregate": "average"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


def _schema_keys(result: FlowResult) -> list[str]:
    return [str(k) for k in result["data_schema"].schema]


async def test_edit_and_remove(recorder_mock, hass: HomeAssistant, sensors) -> None:
    sensor = {
        "field": "v5", "entity_id": "sensor.outside", "constant": None,
        "name": "Outside", "active": True, "aggregate": "average",
    }
    constant = {
        "field": "b3", "entity_id": None, "constant": 5544,
        "name": "Battery Size", "active": True, "aggregate": "last",
    }
    entry = options_entry(hass, donor=True, fields=[sensor, constant])

    # A sensor field shows the sensor, never a value.
    result = await _choose(hass, await _menu(hass, entry), "edit_field")
    result = await _configure(hass, result, {"field": "v5"})
    assert result["step_id"] == "edit_details"
    assert _schema_keys(result) == ["entity_id", "name", "active", "source_unit", "aggregate"]
    result = await _configure(
        hass, result,
        {"entity_id": "sensor.outside", "name": "Ambient", "active": False, "aggregate": "last"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # A fixed value shows the value only.
    result = await _choose(hass, await _menu(hass, entry), "edit_field")
    result = await _configure(hass, result, {"field": "b3"})
    assert _schema_keys(result) == ["constant", "name", "active"]
    result = await _configure(hass, result, {"constant": 10000, "name": "Battery", "active": True})
    assert entry.options[CONF_FIELDS] == [
        sensor | {"name": "Ambient", "active": False, "aggregate": "last"},
        constant | {"constant": 10000, "name": "Battery"},
    ]

    result = await _choose(hass, await _menu(hass, entry), "remove_field")
    result = await _configure(hass, result, {"fields": ["v5", "b3"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FIELDS] == []


async def test_settings_switch_to_donor(recorder_mock, hass: HomeAssistant) -> None:
    entry = options_entry(hass)
    result = await _choose(hass, await _menu(hass, entry), "settings")
    assert result["description_placeholders"]["detected_account"] == "standard"
    result = await _configure(
        hass, result,
        {
            "donor_mode": "donor",
            "upload_interval": "10",
            "retention_days": 200,
            "output_fallback": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["donor_mode"] == "donor"
    assert entry.options["upload_interval"] == "10"
    assert entry.options["retention_days"] == 200
    assert entry.options["output_fallback"] is True

    # Donor fields are now offered.
    result = await _add(hass, entry, "add_sensor")
    assert "v7" in select_options(result, "field")


async def test_history_needs_running_integration(recorder_mock, hass: HomeAssistant) -> None:
    entry = options_entry(hass)
    for step in ("reupload", "import_history"):
        result = await _choose(hass, await _menu(hass, entry), step)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_loaded"


async def test_unitless_sensor_requires_explicit_unit(recorder_mock, hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.unitless", "68")
    entry = options_entry(hass)
    result = await _add(hass, entry, "add_sensor")
    result = await _configure(hass, result, {"entity_id": "sensor.unitless", "field": "v5"})
    assert result["step_id"] == "sensor_details"
    details = {"name": "Temperature", "active": True, "aggregate": "average"}
    result = await _configure(hass, result, details)
    assert result["errors"] == {"base": "unit_required"}
    result = await _configure(hass, result, details | {"source_unit": "W"})
    assert result["errors"] == {"base": "unit_mismatch"}
    result = await _configure(hass, result, details | {"source_unit": "°F"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FIELDS][0]["source_unit"] == "°F"


async def test_edit_extended_constant_custom_unit(recorder_mock, hass: HomeAssistant) -> None:
    mapping = {"field": "v10", "constant": 2, "name": "Custom", "active": True, "aggregate": "average", "source_unit": "kWh"}
    entry = options_entry(hass, donor=True, fields=[mapping])
    result = await _choose(hass, await _menu(hass, entry), "edit_field")
    result = await _configure(hass, result, {"field": "v10"})
    assert result["step_id"] == "edit_details"
    result = await _configure(hass, result, {"constant": 3, "name": "Custom", "active": True, "source_unit": "Wh"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_FIELDS][0]["source_unit"] == "Wh"
