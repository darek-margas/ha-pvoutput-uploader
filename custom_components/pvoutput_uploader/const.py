"""Constants for the PVOutput Uploader integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

DOMAIN: Final = "pvoutput_uploader"

CONF_SYSTEM_ID: Final = "system_id"
CONF_SYSTEM_NAME: Final = "system_name"
CONF_DETECTED_INTERVAL: Final = "detected_interval"
CONF_DETECTED_DONOR: Final = "detected_donor"

# Options
CONF_FIELDS: Final = "fields"
CONF_DONOR_MODE: Final = "donor_mode"
CONF_UPLOAD_INTERVAL: Final = "upload_interval"
CONF_RETENTION_DAYS: Final = "retention_days"
CONF_OUTPUT_FALLBACK: Final = "output_fallback"

# Keys of one field mapping in CONF_FIELDS
MAP_FIELD: Final = "field"
MAP_ENTITY: Final = "entity_id"
MAP_CONSTANT: Final = "constant"
MAP_NAME: Final = "name"
MAP_ACTIVE: Final = "active"
MAP_AGGREGATE: Final = "aggregate"
# Fallback when a sensor does not report a unit; display unit for raw constants.
MAP_UNIT: Final = "source_unit"

DONOR_AUTO: Final = "auto"
DONOR_YES: Final = "donor"
DONOR_NO: Final = "standard"

INTERVAL_AUTO: Final = "auto"
UPLOAD_INTERVALS: Final = (5, 10, 15)

DEFAULT_RETENTION_DAYS: Final = 91
DEFAULT_INTERVAL_MINUTES: Final = 5

# The history log always holds 5 minute rows, whatever the upload interval.
LOG_INTERVAL: Final = 300

KIND_ENERGY: Final = "energy"
KIND_POWER: Final = "power"
KIND_TEMPERATURE: Final = "temperature"
KIND_VOLTAGE: Final = "voltage"
KIND_RAW: Final = "raw"

AGG_ENERGY_TODAY: Final = "energy_today"
AGG_AVERAGE: Final = "average"
AGG_LAST: Final = "last"


@dataclass(frozen=True, slots=True)
class FieldDef:
    """A PVOutput status parameter."""

    key: str
    name: str
    kind: str
    aggregates: tuple[str, ...]
    donor: bool = False
    battery: bool = False
    decimals: int = 0

    @property
    def default_aggregate(self) -> str:
        """Return the aggregate used unless the user picks another."""
        return self.aggregates[0]


_AVG_LAST = (AGG_AVERAGE, AGG_LAST)
_LAST_AVG = (AGG_LAST, AGG_AVERAGE)

# Every status parameter, in PVOutput's order.
FIELD_CATALOG: Final = (
    FieldDef("v1", "Energy Generation", KIND_ENERGY, (AGG_ENERGY_TODAY,)),
    FieldDef("v2", "Power Generation", KIND_POWER, _AVG_LAST),
    FieldDef("v3", "Energy Consumption", KIND_ENERGY, (AGG_ENERGY_TODAY,)),
    FieldDef("v4", "Power Consumption", KIND_POWER, _AVG_LAST),
    FieldDef("v5", "Temperature", KIND_TEMPERATURE, _AVG_LAST, decimals=1),
    FieldDef("v6", "Voltage", KIND_VOLTAGE, _AVG_LAST, decimals=1),
    *(
        FieldDef(
            f"v{n}",
            f"Extended {n}",
            KIND_RAW,
            (AGG_AVERAGE, AGG_LAST, AGG_ENERGY_TODAY),
            donor=True,
            decimals=3,
        )
        for n in range(7, 13)
    ),
    FieldDef("b1", "Battery Power", KIND_POWER, _AVG_LAST, donor=True, battery=True),
    FieldDef(
        "b2", "Battery State of Charge", KIND_RAW, _LAST_AVG,
        donor=True, battery=True, decimals=1,
    ),
    FieldDef("b3", "Battery Size", KIND_ENERGY, (AGG_LAST,), donor=True, battery=True),
    FieldDef(
        "b4", "Battery Lifetime Charge", KIND_ENERGY, (AGG_LAST,),
        donor=True, battery=True,
    ),
    FieldDef(
        "b5", "Battery Lifetime Discharge", KIND_ENERGY, (AGG_LAST,),
        donor=True, battery=True,
    ),
    FieldDef("b6", "Battery State", KIND_RAW, (AGG_LAST,), donor=True, battery=True),
)
FIELDS_BY_KEY: Final = {f.key: f for f in FIELD_CATALOG}
FIELD_KEYS: Final = tuple(f.key for f in FIELD_CATALOG)

STATUS_DAYS_STANDARD: Final = 14
STATUS_DAYS_DONOR: Final = 90
STATUS_BATCH_STANDARD: Final = 30
STATUS_BATCH_DONOR: Final = 100
OUTPUT_BATCH_DONOR: Final = 30

# Wait this long after a 5 minute slot closes before reading it from the recorder.
SETTLE_SECONDS: Final = 60
# Recorder history is read in chunks of this many seconds.
HISTORY_CHUNK_SECONDS: Final = 6 * 3600
MAX_CHUNKS_PER_CYCLE: Final = 8
MAX_REQUESTS_PER_CYCLE: Final = 10
# Requests kept in hand from the hourly limit.
RATE_RESERVE: Final = 2
# A status PVOutput did not confirm (batch status 0, or rejected for lacking v1-v4
# while another uploader such as IoTaWatt has not created it yet) is retried in
# later cycles for this long before it counts as rejected.
UNCONFIRMED_RETRY_SECONDS: Final = 2 * 3600


PVOUTPUT_UNITS: Final = {
    KIND_ENERGY: "Wh",
    KIND_POWER: "W",
    KIND_TEMPERATURE: "°C",
    KIND_VOLTAGE: "V",
}


def upload_unit(field: str, aggregate: str | None = None, source_unit: str | None = None) -> str | None:
    """PVOutput unit; extended fields keep their user-defined unit."""
    if aggregate == AGG_ENERGY_TODAY:
        return "Wh"
    if field == "b2":
        return "%"
    if field == "b6":
        return None
    return PVOUTPUT_UNITS.get(FIELDS_BY_KEY[field].kind, source_unit)
