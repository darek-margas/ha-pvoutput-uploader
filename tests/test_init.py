"""Setting up the integration, its entities and actions."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
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
    CONF_SYSTEM_ID,
    CONF_SYSTEM_NAME,
    DOMAIN,
)

GET_SYSTEM = "custom_components.pvoutput_uploader.api.PVOutputClient.get_system"


@pytest.fixture
def entry(hass: HomeAssistant, tmp_path: Path) -> MockConfigEntry:
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234",
        title="My System",
        data={
            "api_key": "key",
            CONF_SYSTEM_ID: "1234",
            CONF_SYSTEM_NAME: "My System",
            CONF_DETECTED_INTERVAL: 5,
            CONF_DETECTED_DONOR: False,
        },
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_and_unload(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry, tmp_path: Path
) -> None:
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 10, True))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    # What the account reports is kept for later starts.
    assert entry.data[CONF_DETECTED_INTERVAL] == 10
    assert entry.data[CONF_DETECTED_DONOR] is True
    assert (tmp_path / "pvoutput_uploader_1234.db").exists()

    assert hass.states.get("switch.my_system_uploading").state == "on"
    assert hass.states.get("sensor.my_system_account_type").state == "donor"
    assert hass.states.get("sensor.my_system_samples_waiting").state == "0"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.my_system_uploading"}, blocking=True
    )
    assert entry.runtime_data.uploader.upload_enabled is False
    assert hass.states.get("switch.my_system_uploading").state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_offline_uses_saved_settings(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch(GET_SYSTEM, AsyncMock(side_effect=PVOutputConnectionError("down"))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_DETECTED_INTERVAL] == 5
    assert hass.states.get("sensor.my_system_account_type").state == "standard"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_refused_key(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch(GET_SYSTEM, AsyncMock(side_effect=PVOutputAuthError("bad key"))):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_actions(recorder_mock, hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)
    today = dt_util.now().date()

    result = await hass.services.async_call(
        DOMAIN,
        "reupload",
        {"start": (today - timedelta(days=3)).isoformat()},
        blocking=True,
        return_response=True,
    )
    assert result == {"status_days": 4, "output_days": 0}

    result = await hass.services.async_call(
        DOMAIN,
        "reupload",
        {"start": (today - timedelta(days=20)).isoformat(), "method": "outputs"},
        blocking=True,
        return_response=True,
    )
    assert result == {"status_days": 0, "output_days": 21}
    # Today is never queued: its output is not complete.
    assert len(await hass.async_add_executor_job(
        entry.runtime_data.uploader.store.queued_outputs, 100
    )) == 20

    result = await hass.services.async_call(
        DOMAIN,
        "import_from_recorder",
        {"start": (dt_util.now() - timedelta(hours=2)).isoformat()},
        blocking=True,
        return_response=True,
    )
    assert result == {"from_states": 0, "from_statistics": 0}

    # Nothing unsent yet.
    result = await hass.services.async_call(
        DOMAIN, "reupload", {"since_failure": True}, blocking=True, return_response=True
    )
    assert result == {"status_days": 0, "output_days": 0}

    # A date and time works as start too.
    result = await hass.services.async_call(
        DOMAIN,
        "reupload",
        {"start": f"{(today - timedelta(days=1)).isoformat()} 12:00:00"},
        blocking=True,
        return_response=True,
    )
    assert result == {"status_days": 2, "output_days": 0}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "reupload", {}, blocking=True, return_response=True
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "reupload",
            {"start": today.isoformat(), "end": (today - timedelta(days=1)).isoformat()},
            blocking=True,
            return_response=True,
        )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "reupload",
            {"start": today.isoformat(), "config_entry_id": "nope"},
            blocking=True,
            return_response=True,
        )

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_history_from_the_menu(
    recorder_mock, hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)
    today = dt_util.now().date()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "reupload"}
    )
    assert result["step_id"] == "reupload"
    placeholders = result["description_placeholders"]
    assert placeholders["status_days"] == "14"
    assert placeholders["first_unsent"].startswith("none")

    async def submit(data: dict) -> dict:
        return await hass.config_entries.options.async_configure(
            result["flow_id"], {"method": "auto", "end": today.isoformat()} | data
        )

    # Nothing recorded yet, so nothing unsent.
    result = await submit({"from": "since_failure"})
    assert result["errors"] == {"base": "nothing_unsent"}
    result = await submit({"from": "from_time"})
    assert result["errors"] == {"start": "start_required"}
    result = await submit(
        {"from": "from_time", "start": f"{today.isoformat()} 00:00:00",
         "end": (today - timedelta(days=1)).isoformat()}
    )
    assert result["errors"] == {"base": "end_before_start"}
    result = await submit(
        {"from": "from_time", "start": f"{(today - timedelta(days=2)).isoformat()} 06:00:00"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reupload_queued"
    assert result["description_placeholders"] == {"status_days": "3", "output_days": "0"}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "import_history"}
    )
    start = (dt_util.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"start": start, "overwrite": False, "upload": False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "import_done"
    assert result["description_placeholders"] == {"from_states": "0", "from_statistics": "0"}

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_field_sensors(recorder_mock, hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.states.async_set(
        "sensor.outside", "20", {"unit_of_measurement": "°C", "friendly_name": "Outside"}
    )
    fields = [
        {
            "field": "v5", "entity_id": "sensor.outside", "constant": None,
            "name": "Ambient", "active": True, "aggregate": "average",
        },
        {
            "field": "b3", "entity_id": None, "constant": 5544.0,
            "name": "Battery Size", "active": True, "aggregate": "last",
        },
    ]
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fields": fields}
    )
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        await _setup(hass, entry)

    v5 = hass.states.get("sensor.my_system_v5_ambient")
    assert v5.state == "Outside °C"
    assert v5.attributes["pvoutput_unit"] == "°C"
    assert v5.attributes["pvoutput_field"] == "v5"
    assert v5.attributes["pvoutput_field_name"] == "Temperature"
    assert v5.attributes["source"] == "sensor"
    assert v5.attributes["source_entity"] == "sensor.outside"
    assert v5.attributes["source_name"] == "Outside"
    assert v5.attributes["value_per_interval"] == "average"
    assert v5.attributes["configured_to_send"] is True

    b3 = hass.states.get("sensor.my_system_b3_battery_size")
    assert b3.state == "5544 Wh"
    assert b3.attributes["pvoutput_unit"] == "Wh"
    assert b3.attributes["source"] == "fixed value"
    # A battery field on a standard account is kept but not sent.
    assert b3.attributes["configured_to_send"] is False
    assert b3.attributes["not_configured_to_send_because"] == "needs a PVOutput donor account"

    # Removing a field removes its sensor.
    with patch(GET_SYSTEM, AsyncMock(return_value=SystemInfo("My System", 5, False))):
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | {"fields": fields[:1]}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.my_system_b3_battery_size") is None
    assert hass.states.get("sensor.my_system_v5_ambient") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
