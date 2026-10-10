"""Config and options flow for PVOutput Uploader."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    DateTimeSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .api import PVOutputAuthError, PVOutputClient, PVOutputError
from .builder import unit_supported
from .const import (
    AGG_ENERGY_TODAY,
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_DONOR_MODE,
    CONF_FIELDS,
    CONF_OUTPUT_FALLBACK,
    CONF_RETENTION_DAYS,
    CONF_SYSTEM_ID,
    CONF_SYSTEM_NAME,
    CONF_UPLOAD_INTERVAL,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_RETENTION_DAYS,
    DOMAIN,
    DONOR_AUTO,
    DONOR_NO,
    DONOR_YES,
    FIELD_CATALOG,
    FIELD_KEYS,
    FIELDS_BY_KEY,
    INTERVAL_AUTO,
    KIND_ENERGY,
    MAP_ACTIVE,
    MAP_AGGREGATE,
    MAP_CONSTANT,
    MAP_ENTITY,
    MAP_FIELD,
    MAP_NAME,
    MAP_UNIT,
    upload_unit,
    UPLOAD_INTERVALS,
)
from .services import (
    ATTR_END,
    ATTR_METHOD,
    ATTR_OVERWRITE,
    ATTR_START,
    ATTR_UPLOAD,
)
from .uploader import REUPLOAD_AUTO, REUPLOAD_OUTPUTS, REUPLOAD_STATUSES, Uploader

ATTR_FROM = "from"
FROM_FAILURE = "since_failure"
FROM_TIME = "from_time"

ENTITY_SELECTOR = EntitySelector(EntitySelectorConfig(domain=["sensor", "input_number"]))
CONSTANT_SELECTOR = NumberSelector(
    NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any")
)

DEFAULT_OPTIONS: dict[str, Any] = {
    CONF_FIELDS: [],
    CONF_DONOR_MODE: DONOR_AUTO,
    CONF_UPLOAD_INTERVAL: INTERVAL_AUTO,
    CONF_RETENTION_DAYS: DEFAULT_RETENTION_DAYS,
    CONF_OUTPUT_FALLBACK: False,
}


class PVOutputUploaderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the API key and system ID."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the first step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            system_id = str(user_input[CONF_SYSTEM_ID]).strip()
            await self.async_set_unique_id(system_id)
            self._abort_if_unique_id_configured()
            client = PVOutputClient(
                async_get_clientsession(self.hass), user_input[CONF_API_KEY], system_id
            )
            try:
                system = await client.get_system()
            except PVOutputAuthError:
                errors["base"] = "invalid_auth"
            except PVOutputError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=system.name or f"PVOutput {system_id}",
                    data={
                        CONF_API_KEY: user_input[CONF_API_KEY],
                        CONF_SYSTEM_ID: system_id,
                        CONF_SYSTEM_NAME: system.name,
                        CONF_DETECTED_INTERVAL: system.interval,
                        CONF_DETECTED_DONOR: system.donor,
                    },
                    options=deepcopy(DEFAULT_OPTIONS),
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(),
                    vol.Required(CONF_SYSTEM_ID): TextSelector(),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return PVOutputUploaderOptionsFlow()


class PVOutputUploaderOptionsFlow(OptionsFlow):
    """Fields, history actions and settings, picked from a menu.

    Every action saves and closes as soon as it is submitted.
    """

    def __init__(self) -> None:
        """Initialize the flow."""
        self._options: dict[str, Any] = {}
        self._new: dict[str, Any] = {}
        self._edit_field: str | None = None

    @property
    def _fields(self) -> list[dict[str, Any]]:
        return self._options[CONF_FIELDS]

    def _donor(self) -> bool:
        mode = self._options.get(CONF_DONOR_MODE, DONOR_AUTO)
        if mode == DONOR_AUTO:
            return bool(self.config_entry.data.get(CONF_DETECTED_DONOR))
        return mode == DONOR_YES

    def _mapping(self, key: str) -> dict[str, Any]:
        return next(m for m in self._fields if m[MAP_FIELD] == key)

    def _field_options(self, keys: list[str]) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=k, label=f"{k} · {FIELDS_BY_KEY[k].name}" + (
                f" ({upload_unit(k)})" if upload_unit(k) else ""
            ))
            for k in keys
        ]

    def _used_fields(self) -> list[str]:
        used = {m[MAP_FIELD] for m in self._fields}
        return [f.key for f in FIELD_CATALOG if f.key in used]

    def _free_fields(self) -> list[str]:
        """Return the fields not used yet that the account may send."""
        used = {m[MAP_FIELD] for m in self._fields}
        donor = self._donor()
        return [f.key for f in FIELD_CATALOG if f.key not in used and (donor or not f.donor)]

    def _field_selector(self, keys: list[str], mode: SelectSelectorMode) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(options=self._field_options(keys), mode=mode)
        )

    def _describe(self) -> str:
        donor = self._donor()
        lines = []
        for m in sorted(self._fields, key=lambda m: FIELD_KEYS.index(m[MAP_FIELD])):
            field = FIELDS_BY_KEY[m[MAP_FIELD]]
            if m.get(MAP_ENTITY):
                source = f"`{m[MAP_ENTITY]}`, {m.get(MAP_AGGREGATE) or field.default_aggregate}"
            else:
                source = f"constant {m.get(MAP_CONSTANT)}"
            unit = upload_unit(field.key, m.get(MAP_AGGREGATE), m.get(MAP_UNIT))
            if unit:
                source += f" {unit}"
            state = "active" if m.get(MAP_ACTIVE, True) else "passive"
            if field.donor and not donor:
                state += ", needs a donor account so not sent"
            lines.append(
                f"- **{field.key}** {m.get(MAP_NAME) or field.name}: {source} ({state})"
            )
        return "\n".join(lines) or "_No fields yet. Start with **Add field**._"

    def _unit_error(self, field: str, entity_id: str, aggregate: str, fallback: str | None = None) -> str | None:
        """Return an error key if the sensor's unit does not fit the field."""
        kind = KIND_ENERGY if aggregate == AGG_ENERGY_TODAY else FIELDS_BY_KEY[field].kind
        state = self.hass.states.get(entity_id)
        unit = (state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None) or fallback
        if not unit and kind != "raw":
            return "unit_required"
        return None if unit_supported(kind, unit) else "unit_mismatch"

    def _constant_selector(self, field: str) -> NumberSelector:
        """Label fixed values with the known PVOutput unit."""
        config = NumberSelectorConfig(mode=NumberSelectorMode.BOX, step="any")
        if unit := upload_unit(field):
            config["unit_of_measurement"] = unit
        return NumberSelector(config)

    def _details_schema(self, field: str, has_entity: bool) -> dict[Any, Any]:
        """Name, active flag and, for sensors, how values are combined."""
        fdef = FIELDS_BY_KEY[field]
        schema: dict[Any, Any] = {
            vol.Required(MAP_NAME): TextSelector(),
            vol.Required(MAP_ACTIVE): BooleanSelector(),
        }
        if has_entity or fdef.kind == "raw":
            schema[vol.Optional(MAP_UNIT)] = TextSelector()
        if has_entity and len(fdef.aggregates) > 1:
            schema[vol.Required(MAP_AGGREGATE)] = SelectSelector(
                SelectSelectorConfig(
                    options=list(fdef.aggregates),
                    translation_key="aggregate",
                )
            )
        return schema

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)

    def _uploader(self) -> Uploader | None:
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return None
        return self.config_entry.runtime_data.uploader

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the main menu."""
        self._options = deepcopy(DEFAULT_OPTIONS) | deepcopy(dict(self.config_entry.options))
        menu = []
        if self._free_fields():
            menu.append("add_field")
        if self._fields:
            menu += ["edit_field", "remove_field"]
        menu += ["reupload", "import_history", "settings"]
        detected = self.config_entry.data.get(CONF_DETECTED_INTERVAL, DEFAULT_INTERVAL_MINUTES)
        return self.async_show_menu(
            step_id="init",
            menu_options=menu,
            description_placeholders={
                "fields": self._describe(),
                "account": "donor" if self._donor() else "standard",
                "detected_interval": str(detected),
            },
        )

    # Add

    async def async_step_add_field(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose where the value comes from."""
        return self.async_show_menu(
            step_id="add_field", menu_options=["add_sensor", "add_constant"]
        )

    async def async_step_add_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a sensor and the PVOutput field it feeds."""
        errors: dict[str, str] = {}
        if user_input is not None:
            field = user_input[MAP_FIELD]
            entity_id = user_input[MAP_ENTITY]
            # Extended fields start as "average"; energy since midnight is checked later.
            if (error := self._unit_error(field, entity_id, FIELDS_BY_KEY[field].default_aggregate)) == "unit_mismatch":
                errors["base"] = error
            else:
                self._new = {MAP_FIELD: field, MAP_ENTITY: entity_id, MAP_CONSTANT: None}
                return await self.async_step_sensor_details()
        schema = vol.Schema(
            {
                vol.Required(MAP_ENTITY): ENTITY_SELECTOR,
                vol.Required(MAP_FIELD): self._field_selector(
                    self._free_fields(), SelectSelectorMode.DROPDOWN
                ),
            }
        )
        return self.async_show_form(
            step_id="add_sensor",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_sensor_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the field and choose whether it is sent."""
        field = self._new[MAP_FIELD]
        fdef = FIELDS_BY_KEY[field]
        errors: dict[str, str] = {}
        if user_input is not None:
            aggregate = user_input.get(MAP_AGGREGATE, fdef.default_aggregate)
            if error := self._unit_error(field, self._new[MAP_ENTITY], aggregate, user_input.get(MAP_UNIT, "").strip() or None):
                errors["base"] = error
            else:
                self._fields.append(
                    self._new
                    | {
                        MAP_NAME: user_input[MAP_NAME],
                        MAP_ACTIVE: user_input[MAP_ACTIVE],
                        MAP_AGGREGATE: aggregate,
                    }
                )
                if unit := user_input.get(MAP_UNIT, "").strip():
                    self._fields[-1][MAP_UNIT] = unit
                return self._save()
        defaults = user_input or {
            MAP_NAME: fdef.name,
            MAP_ACTIVE: True,
            MAP_AGGREGATE: fdef.default_aggregate,
        }
        return self.async_show_form(
            step_id="sensor_details",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(self._details_schema(field, True)), defaults
            ),
            description_placeholders={
                "field": f"{field} · {fdef.name}",
                "entity": self._new[MAP_ENTITY],
            },
            errors=errors,
        )

    async def async_step_add_constant(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A fixed value sent with every status, such as the battery size."""
        if user_input is not None:
            field = user_input[MAP_FIELD]
            self._fields.append(
                {
                    MAP_FIELD: field,
                    MAP_ENTITY: None,
                    MAP_CONSTANT: user_input[MAP_CONSTANT],
                    MAP_NAME: user_input.get(MAP_NAME) or FIELDS_BY_KEY[field].name,
                    MAP_ACTIVE: user_input[MAP_ACTIVE],
                    MAP_AGGREGATE: FIELDS_BY_KEY[field].default_aggregate,
                }
            )
            if unit := user_input.get(MAP_UNIT, "").strip():
                self._fields[-1][MAP_UNIT] = unit
            return self._save()
        schema = vol.Schema(
            {
                vol.Required(MAP_FIELD): self._field_selector(
                    self._free_fields(), SelectSelectorMode.DROPDOWN
                ),
                vol.Required(MAP_CONSTANT): CONSTANT_SELECTOR,
                vol.Optional(MAP_NAME): TextSelector(),
                vol.Optional(MAP_UNIT): TextSelector(),
                vol.Required(MAP_ACTIVE, default=True): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="add_constant", data_schema=schema)

    # Edit and remove

    async def async_step_edit_field(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the field to change."""
        if user_input is not None:
            self._edit_field = user_input[MAP_FIELD]
            return await self.async_step_edit_details()
        return self.async_show_form(
            step_id="edit_field",
            data_schema=vol.Schema(
                {
                    vol.Required(MAP_FIELD): self._field_selector(
                        self._used_fields(), SelectSelectorMode.LIST
                    )
                }
            ),
        )

    async def async_step_edit_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change a field, showing the inputs of its kind: sensor or constant."""
        assert self._edit_field is not None
        field = self._edit_field
        fdef = FIELDS_BY_KEY[field]
        mapping = self._mapping(field)
        is_sensor = bool(mapping.get(MAP_ENTITY))
        errors: dict[str, str] = {}
        if user_input is not None:
            aggregate = user_input.get(
                MAP_AGGREGATE, mapping.get(MAP_AGGREGATE) or fdef.default_aggregate
            )
            if is_sensor and (
                error := self._unit_error(field, user_input[MAP_ENTITY], aggregate, user_input.get(MAP_UNIT, "").strip() or None)
            ):
                errors["base"] = error
            else:
                mapping.update(
                    {
                        MAP_NAME: user_input[MAP_NAME],
                        MAP_ACTIVE: user_input[MAP_ACTIVE],
                        MAP_AGGREGATE: aggregate,
                    }
                )
                if unit := user_input.get(MAP_UNIT, "").strip():
                    mapping[MAP_UNIT] = unit
                else:
                    mapping.pop(MAP_UNIT, None)
                if is_sensor:
                    mapping[MAP_ENTITY] = user_input[MAP_ENTITY]
                else:
                    mapping[MAP_CONSTANT] = user_input[MAP_CONSTANT]
                return self._save()
        source: dict[Any, Any] = (
            {vol.Required(MAP_ENTITY): ENTITY_SELECTOR}
            if is_sensor
            else {vol.Required(MAP_CONSTANT): self._constant_selector(field)}
        )
        schema = vol.Schema(source | self._details_schema(field, is_sensor))
        defaults = user_input or {
            k: v for k, v in mapping.items() if v is not None and k != MAP_FIELD
        }
        return self.async_show_form(
            step_id="edit_details",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            description_placeholders={"field": f"{field} · {fdef.name}"},
            errors=errors,
        )

    async def async_step_remove_field(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove fields."""
        if user_input is not None:
            drop = set(user_input[CONF_FIELDS])
            self._options[CONF_FIELDS] = [m for m in self._fields if m[MAP_FIELD] not in drop]
            return self._save()
        return self.async_show_form(
            step_id="remove_field",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FIELDS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._field_options(self._used_fields()),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # History

    async def async_step_reupload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send the history log again, since the first failure or from a time."""
        if (uploader := self._uploader()) is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        today = dt_util.now().date()
        first_unsent = await uploader.async_first_unsent()
        if user_input is not None:
            end = date.fromisoformat(user_input.get(ATTR_END) or today.isoformat())
            start: datetime | None = None
            if user_input[ATTR_FROM] == FROM_FAILURE:
                if first_unsent is None:
                    errors["base"] = "nothing_unsent"
                else:
                    start = dt_util.as_local(dt_util.utc_from_timestamp(first_unsent))
            elif not user_input.get(ATTR_START):
                errors[ATTR_START] = "start_required"
            else:
                start = _local_datetime(user_input[ATTR_START])
            if start is not None and end < start.date():
                errors["base"] = "end_before_start"
            if start is not None and not errors:
                result = await uploader.async_reupload(start, end, user_input[ATTR_METHOD])
                await self.config_entry.runtime_data.async_request_refresh()
                return self.async_abort(
                    reason="reupload_queued",
                    description_placeholders={
                        "status_days": str(result["status_days"]),
                        "output_days": str(result["output_days"]),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(ATTR_FROM): SelectSelector(
                    SelectSelectorConfig(
                        options=[FROM_FAILURE, FROM_TIME],
                        translation_key="reupload_from",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(ATTR_START): DateTimeSelector(),
                vol.Optional(ATTR_END): DateSelector(),
                vol.Required(ATTR_METHOD): SelectSelector(
                    SelectSelectorConfig(
                        options=[REUPLOAD_AUTO, REUPLOAD_STATUSES, REUPLOAD_OUTPUTS],
                        translation_key="reupload_method",
                    )
                ),
            }
        )
        defaults = user_input or {
            ATTR_FROM: FROM_FAILURE if first_unsent is not None else FROM_TIME,
            ATTR_START: (today - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
            ATTR_END: today.isoformat(),
            ATTR_METHOD: REUPLOAD_AUTO,
        }
        summary = uploader.summary
        return self.async_show_form(
            step_id="reupload",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            description_placeholders={
                "status_days": str(uploader.status_days),
                "first_unsent": dt_util.as_local(
                    dt_util.utc_from_timestamp(first_unsent)
                ).strftime("%Y-%m-%d %H:%M")
                if first_unsent is not None
                else "none, everything recorded was sent",
                "pending": str(summary.get("pending", 0)),
                "failed": str(summary.get("failed", 0)),
                "partial": str(summary.get("partial", 0)),
                "expired": str(summary.get("expired", 0)),
            },
            errors=errors,
        )

    async def async_step_import_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fill the history log from Home Assistant's recorder."""
        if (uploader := self._uploader()) is None:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        now = dt_util.now()
        if user_input is not None:
            start = _local_datetime(user_input[ATTR_START])
            end = _local_datetime(user_input[ATTR_END]) if user_input.get(ATTR_END) else now
            if end <= start:
                errors["base"] = "end_before_start"
            else:
                result = await uploader.async_import(
                    start, end, user_input[ATTR_OVERWRITE], user_input[ATTR_UPLOAD]
                )
                await self.config_entry.runtime_data.async_request_refresh()
                return self.async_abort(
                    reason="import_done",
                    description_placeholders={
                        "from_states": str(result["from_states"]),
                        "from_statistics": str(result["from_statistics"]),
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(ATTR_START): DateTimeSelector(),
                vol.Optional(ATTR_END): DateTimeSelector(),
                vol.Required(ATTR_OVERWRITE, default=False): BooleanSelector(),
                vol.Required(ATTR_UPLOAD, default=False): BooleanSelector(),
            }
        )
        defaults = user_input or {
            ATTR_START: (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"),
        }
        return self.async_show_form(
            step_id="import_history",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            errors=errors,
        )

    # Settings

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Account type, upload interval and history settings."""
        if user_input is not None:
            user_input[CONF_RETENTION_DAYS] = int(user_input[CONF_RETENTION_DAYS])
            self._options.update(user_input)
            return self._save()
        schema = vol.Schema(
            {
                vol.Required(CONF_DONOR_MODE): SelectSelector(
                    SelectSelectorConfig(
                        options=[DONOR_AUTO, DONOR_YES, DONOR_NO],
                        translation_key="donor_mode",
                    )
                ),
                vol.Required(CONF_UPLOAD_INTERVAL): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=INTERVAL_AUTO, label="From PVOutput"),
                            *(
                                SelectOptionDict(value=str(m), label=f"{m} minutes")
                                for m in UPLOAD_INTERVALS
                            ),
                        ]
                    )
                ),
                vol.Required(CONF_RETENTION_DAYS): NumberSelector(
                    NumberSelectorConfig(
                        min=30, max=3650, step=1, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
                vol.Required(CONF_OUTPUT_FALLBACK): BooleanSelector(),
            }
        )
        current = {k: self._options.get(k, v) for k, v in DEFAULT_OPTIONS.items()}
        current[CONF_UPLOAD_INTERVAL] = str(current[CONF_UPLOAD_INTERVAL])
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            description_placeholders={
                "detected_interval": str(
                    self.config_entry.data.get(CONF_DETECTED_INTERVAL, DEFAULT_INTERVAL_MINUTES)
                ),
                "detected_account": "donor"
                if self.config_entry.data.get(CONF_DETECTED_DONOR)
                else "standard",
            },
        )


def _local_datetime(value: str) -> datetime:
    """Parse a datetime selector value as local time."""
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise vol.Invalid(f"Invalid date and time: {value}")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.get_default_time_zone())
