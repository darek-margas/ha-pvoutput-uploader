"""Record sensor history and upload it to PVOutput."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
import json
import logging
import math
import time
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import (
    PVOutputAuthError,
    PVOutputBadRequest,
    PVOutputClient,
    PVOutputError,
    PVOutputRateLimited,
    StatusReading,
)
from .archive import Archive
from .builder import (
    Source,
    async_build_from_states,
    async_build_from_statistics,
)
from .const import (
    DIRECTION_DOWNLOAD,
    DIRECTION_UPLOAD,
    DOWNLOAD_STALE_INTERVALS,
    MAP_DIRECTION,
    MODE_DOWNLOAD,
    MODE_RECORD,
    MODE_SEND,
    AGG_ENERGY_TODAY,
    AGG_LAST,
    BATTERY_POWER,
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_DONOR_MODE,
    CONF_FIELDS,
    CONF_OUTPUT_FALLBACK,
    CONF_RETENTION_DAYS,
    CONF_UPLOAD_INTERVAL,
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_RETENTION_DAYS,
    DONOR_AUTO,
    DONOR_YES,
    FIELDS_BY_KEY,
    HISTORY_CHUNK_SECONDS,
    INTERVAL_AUTO,
    LOG_INTERVAL,
    MAP_ACTIVE,
    MAP_AGGREGATE,
    MAP_CONSTANT,
    MAP_ENTITY,
    MAP_FIELD,
    MAP_NAME,
    MAP_UNIT,
    MAX_CHUNKS_PER_CYCLE,
    MAX_REQUESTS_PER_CYCLE,
    UNCONFIRMED_RETRY_SECONDS,
    OUTPUT_BATCH_DONOR,
    RATE_RESERVE,
    CURRENT_SLOTS,
    REQUEST_SPACING_SECONDS,
    SETTLE_SECONDS,
    STATUS_BATCH_DONOR,
    STATUS_BATCH_STANDARD,
    STATUS_DAYS_DONOR,
    STATUS_DAYS_STANDARD,
)
from .store import DONE, EXPIRED, FAILED, PARTIAL, PENDING, Sample, SampleStore

_LOGGER = logging.getLogger(__name__)
# Indirection so tests can check the pacing without sleeping.
_sleep = asyncio.sleep
_T = TypeVar("_T")

META_LAST_BUILT = "last_built"
META_PAUSED_UNTIL = "paused_until"
META_LAST_UPLOAD = "last_upload"
# Energy counters' last values, carried between recorder reads (builder).
META_ENERGY_BASELINES = "energy_baselines"
META_LAST_DOWNLOAD = "last_download"
META_LAST_PURGE = "last_purge"
# Statuses up to this slot end are sent in batches without battery values.
META_NO_BATTERY_UNTIL = "no_battery_until"
# Passive fields a reupload also sends, and the period: {"from", "to", "fields"}.
META_PASSIVE_REUPLOAD = "passive_reupload"

REUPLOAD_AUTO = "auto"
REUPLOAD_STATUSES = "statuses"
REUPLOAD_OUTPUTS = "outputs"

# Values a PVOutput error message names, sent again without them once.
REJECTED_VALUE_FIELDS = (
    ("power value", ("v2", "v4")),
    ("energy value", ("v1", "v3")),
    ("moon powered", ("v1", "v2")),
)

# Status fields in addbatchstatus.jsp's column order. The battery fields b1-b6
# follow v12; PVOutput takes them in batches although its documentation lists
# them only for addstatus.jsp.
BATCH_KEYS = (
    *(f"v{n}" for n in range(1, 13)),
    *(f"b{n}" for n in range(1, 7)),
)


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """A configured PVOutput field."""

    field: str
    entity_id: str | None
    constant: float | None
    name: str
    active: bool
    aggregate: str
    unit: str | None = None
    direction: str = DIRECTION_UPLOAD

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldMapping:
        """Build a mapping from options data."""
        field = FIELDS_BY_KEY[data[MAP_FIELD]]
        return cls(
            field=field.key,
            entity_id=data.get(MAP_ENTITY) or None,
            constant=data.get(MAP_CONSTANT),
            name=data.get(MAP_NAME) or field.name,
            active=data.get(MAP_ACTIVE, True),
            aggregate=data.get(MAP_AGGREGATE) or field.default_aggregate,
            unit=data.get(MAP_UNIT) or None,
            direction=data.get(MAP_DIRECTION) or DIRECTION_UPLOAD,
        )

    @property
    def has_source(self) -> bool:
        """Return whether the field has an HA sensor or a fixed value to upload."""
        return self.entity_id is not None or self.constant is not None

    @property
    def download(self) -> bool:
        """Return whether the field is read from PVOutput instead of sent."""
        return self.direction == DIRECTION_DOWNLOAD

    @property
    def mode(self) -> str:
        """Return send, record_only or download."""
        if self.download:
            return MODE_DOWNLOAD
        return MODE_SEND if self.active else MODE_RECORD

    @property
    def decimals(self) -> int:
        """Return the number of decimals sent."""
        if self.aggregate == AGG_ENERGY_TODAY:
            return 0
        return FIELDS_BY_KEY[self.field].decimals


def _fmt(value: float, decimals: int) -> str:
    if decimals == 0:
        return str(int(round(value)))
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")


def _midnight(ts: float) -> int:
    local = dt_util.as_local(dt_util.utc_from_timestamp(ts))
    return int(dt_util.start_of_local_day(local).timestamp())


def _midnight_of(day: date) -> int:
    return int(dt_util.start_of_local_day(day).timestamp())


def _align_down(ts: float, step: int) -> int:
    base = _midnight(ts)
    return base + int((ts - base) // step) * step


def _slot_end(ts: int, step: int) -> int:
    """Return the end of the upload slot holding the 5 minute row ``ts``."""
    base = _midnight(ts)
    return base + -(-(ts - base) // step) * step


def _local(ts: int) -> datetime:
    return dt_util.as_local(dt_util.utc_from_timestamp(ts))


class Uploader:
    """Keeps the history log current and feeds PVOutput from it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PVOutputClient,
        store: SampleStore,
    ) -> None:
        """Initialize the uploader."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.store = store
        options = entry.options
        self.mappings = [FieldMapping.from_dict(m) for m in options.get(CONF_FIELDS, [])]
        # A field set to Download keeps its sensor, and it is still recorded:
        # switching back to Send or Record only then leaves no gap in the log.
        # It is never sent while downloading (see _sendable).
        self.sources = [
            Source(m.field, m.entity_id, m.aggregate, m.unit)
            for m in self.mappings
            if m.entity_id
        ]
        self.upload_enabled = True
        # The Downloading switch: live reads for fields in Download mode.
        self.download_enabled = True
        self.lock = asyncio.Lock()
        self.paused_until: float | None = None
        # Kept in the log database, so a restart or reload does not reset them.
        self.last_upload: datetime | None = None
        self.last_download: datetime | None = None
        self.last_error: str | None = None
        # Kept apart from upload errors: a working upload must not hide it.
        self.record_error: str | None = None
        # The latest status read from PVOutput for download fields, when it was
        # read, and when a read was last tried.
        self.reading: StatusReading | None = None
        self.reading_at: float | None = None
        self._downloaded_at: float | None = None
        self.summary: dict[str, Any] = {}
        # Field key -> (timestamp, value) of its latest recorded value.
        self.latest: dict[str, tuple[int, float]] = {}
        self._batch_limit = self.batch_size
        self.request_spacing = REQUEST_SPACING_SECONDS
        self._last_request = 0.0
        self.archive = Archive(self)

    # Settings

    @property
    def donor(self) -> bool:
        """Return whether donor features are used."""
        mode = self.entry.options.get(CONF_DONOR_MODE, DONOR_AUTO)
        if mode == DONOR_AUTO:
            return bool(self.entry.data.get(CONF_DETECTED_DONOR))
        return mode == DONOR_YES

    @property
    def upload_interval(self) -> int:
        """Return the status interval in seconds."""
        value = self.entry.options.get(CONF_UPLOAD_INTERVAL, INTERVAL_AUTO)
        if value == INTERVAL_AUTO:
            value = self.entry.data.get(CONF_DETECTED_INTERVAL, DEFAULT_INTERVAL_MINUTES)
        return int(value) * 60

    @property
    def status_days(self) -> int:
        """Return how far back PVOutput accepts statuses."""
        return STATUS_DAYS_DONOR if self.donor else STATUS_DAYS_STANDARD

    @property
    def batch_size(self) -> int:
        """Return the statuses allowed in one batch."""
        return STATUS_BATCH_DONOR if self.donor else STATUS_BATCH_STANDARD

    @property
    def active_mappings(self) -> list[FieldMapping]:
        """Return the mappings that are sent."""
        return self._sendable(())

    @property
    def passive_mappings(self) -> list[FieldMapping]:
        """Return upload fields that are recorded but not sent (passive)."""
        return [
            m for m in self.mappings if not m.active and not m.download and m.has_source
        ]

    @property
    def download_mappings(self) -> list[FieldMapping]:
        """Return the fields read from PVOutput."""
        return [m for m in self.mappings if m.download]

    def _sendable(self, extra: Iterable[str]) -> list[FieldMapping]:
        """Return the active mappings plus the passive fields in ``extra``.

        Passive fields chosen for a reupload follow the same rules as active
        ones: donor-only fields need a donor account, and battery fields b1.
        """
        extra = set(extra)
        sent = [
            m
            for m in self.mappings
            if not m.download
            and (m.active or m.field in extra)
            and (self.donor or not FIELDS_BY_KEY[m.field].donor)
            and m.has_source
        ]
        if not any(m.field == BATTERY_POWER for m in sent):
            # PVOutput ignores the other battery values without b1.
            sent = [m for m in sent if not FIELDS_BY_KEY[m.field].battery]
        return sent

    def window_start(self, now: float) -> int:
        """Return the oldest timestamp PVOutput still takes statuses for."""
        today = dt_util.as_local(dt_util.utc_from_timestamp(now)).date()
        return _midnight_of(today - timedelta(days=self.status_days - 1))

    def _day_bounds(self, day: date) -> tuple[int, int]:
        """Return the row range [start, end) whose slots are labelled ``day``.

        The slot ending at 00:00 is labelled with the new day, as the
        IoTaWatt firmware does, so a day starts one slot before midnight.
        """
        step = self.upload_interval
        nxt = day + timedelta(days=1)
        return _midnight_of(day) - step + 1, _midnight_of(nxt) - step + 1

    async def _run(self, func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        return await self.hass.async_add_executor_job(partial(func, *args, **kwargs))

    async def _refresh_summary(self) -> None:
        self.summary = await self._run(self.store.summary)
        self.latest = await self._run(self.store.latest_values)

    async def async_load(self) -> None:
        """Restore state kept in the log database."""
        paused = await self._run(self.store.get_meta, META_PAUSED_UNTIL)
        if paused:
            self.paused_until = float(paused)
        await self.archive.async_load()
        for key, attr in ((META_LAST_UPLOAD, "last_upload"), (META_LAST_DOWNLOAD, "last_download")):
            if stored := await self._run(self.store.get_meta, key):
                setattr(self, attr, dt_util.parse_datetime(stored))
        await self._refresh_summary()

    async def _mark_uploaded(self) -> None:
        """Note a request PVOutput accepted."""
        self.last_upload = dt_util.utcnow()
        await self._run(self.store.set_meta, META_LAST_UPLOAD, self.last_upload.isoformat())

    # Main cycle

    async def async_cycle(self) -> None:
        """Record new slots, upload what is due and tidy the log."""
        async with self.lock:
            try:
                await self._async_record()
                self.record_error = None
            except Exception as err:
                # Uploads go on, but only up to the last slot fully read (last
                # built), so nothing half-recorded is sent; the next cycle retries.
                self.record_error = f"Recorder history could not be read: {err}"
                _LOGGER.exception("Reading recorder history failed")
            if self.upload_enabled:
                try:
                    await self._async_upload_statuses()
                    await self._async_upload_outputs()
                except PVOutputError as err:
                    self.last_error = str(err)
                    _LOGGER.warning("PVOutput upload stopped: %s", err)
            # Downloading does not depend on the Uploading switch.
            try:
                await self._async_download()
            except PVOutputError as err:
                self.last_error = f"Download: {err}"
                _LOGGER.warning("PVOutput download failed: %s", err)
            # PVOutput history (archive): its own switch, and history downloads.
            try:
                await self.archive.async_cycle()
            except PVOutputError as err:
                self.last_error = f"History download: {err}"
                _LOGGER.warning("PVOutput history download failed: %s", err)
            await self._async_housekeeping()
            await self._refresh_summary()

    async def _async_record(self) -> None:
        target = _align_down(time.time() - SETTLE_SECONDS, LOG_INTERVAL)
        stored = await self._run(self.store.get_meta, META_LAST_BUILT)
        last = int(stored) if stored else None
        if not self.sources or last is None or last > target:
            # Nothing to read, or a first start: begin logging from now on.
            await self._run(
                self.store.set_metas,
                {META_LAST_BUILT: str(target), META_ENERGY_BASELINES: None},
            )
            return
        last = _align_down(last, LOG_INTERVAL)
        # Each energy counter's last value, carried from one read to the next so
        # no increment across a read boundary is lost (see the builder).
        raw = await self._run(self.store.get_meta, META_ENERGY_BASELINES)
        baselines: dict[str, Any] = json.loads(raw) if raw else {}
        for _ in range(MAX_CHUNKS_PER_CYCLE):
            if last >= target:
                break
            end = min(target, last + HISTORY_CHUNK_SECONDS)
            samples = await async_build_from_states(
                self.hass, self.sources, last, end, baselines
            )
            await self._run(self.store.add_samples, samples)
            last = end
            # One checkpoint: the time read up to and the counters at that time.
            await self._run(
                self.store.set_metas,
                {META_LAST_BUILT: str(last), META_ENERGY_BASELINES: json.dumps(baselines)},
            )

    # Download

    async def _async_download(self) -> None:
        """Read the latest status for download fields, once per status interval."""
        if not self.download_mappings:
            self.reading = self.reading_at = None
            return
        if not self.download_enabled:
            return
        now = time.time()
        if self._downloaded_at is not None and now - self._downloaded_at < self.upload_interval:
            return
        if not self._rate_ok(RATE_RESERVE):
            return
        await self._pace()
        try:
            reading = await self.client.get_status(extended=self.donor)
        except PVOutputRateLimited as err:
            await self._pause(err.reset, str(err))
            return
        except PVOutputBadRequest as err:
            # Tried this interval either way: a rejected request is not retried
            # every cycle, using up requests.
            self._downloaded_at = now
            if "no status" not in str(err).lower():
                # Any other rejection is an error (shown in Last error); the
                # reading is kept until it goes stale.
                raise
            # "No status found": nothing recorded at PVOutput yet today. The
            # last reading (e.g. yesterday's energy) no longer applies.
            _LOGGER.debug("PVOutput has no status to download: %s", err)
            self.reading = self.reading_at = None
            return
        self.reading = reading
        self.reading_at = self._downloaded_at = now
        self.last_download = dt_util.utc_from_timestamp(now)
        await self._run(self.store.set_meta, META_LAST_DOWNLOAD, self.last_download.isoformat())

    @property
    def fresh_reading(self) -> StatusReading | None:
        """Return the last reading, unless reads have failed for too long.

        After DOWNLOAD_STALE_INTERVALS upload intervals without a successful
        read (PVOutput or the network down, rate limit), the download sensors
        turn unavailable instead of showing an old value as current.
        """
        if self.reading_at is None or (
            time.time() - self.reading_at > DOWNLOAD_STALE_INTERVALS * self.upload_interval
        ):
            return None
        return self.reading

    # Rate limit

    async def _pace(self) -> None:
        """Leave at least ``request_spacing`` seconds between PVOutput requests."""
        wait = self._last_request + self.request_spacing - time.monotonic()
        if wait > 0:
            await _sleep(wait)
        self._last_request = time.monotonic()

    def _history_reserve(self) -> int:
        """Return the requests history (backlog, reuploads, daily totals) leaves.

        Enough for every current status due before PVOutput's hourly limit
        resets, plus one, on top of the reserve for other uploaders. Each upload
        interval needs one request (its current statuses go as one batch,
        battery values included). Before the reset time is known, a full hour
        is assumed.
        """
        now = time.time()
        reset = self.client.rate_reset
        left = reset - now if reset is not None and reset > now else 3600
        return RATE_RESERVE + math.ceil(left / self.upload_interval) + 1

    def _rate_ok(self, reserve: int | None = None) -> bool:
        """Return whether a request may be made, keeping ``reserve`` requests.

        Without ``reserve``, the history reserve applies.
        """
        if reserve is None:
            reserve = self._history_reserve()
        now = time.time()
        if self.paused_until is not None:
            if now < self.paused_until:
                return False
            self.paused_until = None
        c = self.client
        if (
            c.rate_remaining is not None
            and c.rate_remaining <= reserve
            and c.rate_reset is not None
            and now < c.rate_reset
        ):
            return False
        return True

    async def _pause(self, until: float, message: str) -> None:
        self.paused_until = until
        self.last_error = message
        await self._run(self.store.set_meta, META_PAUSED_UNTIL, str(until))
        _LOGGER.info(
            "PVOutput rate limit reached, resuming at %s",
            _local(int(until)).isoformat(),
        )

    # Statuses

    def _slot_values(
        self,
        slot: int,
        rows: list[Sample],
        day_rows: list[Sample],
        extra: Iterable[str] = (),
    ) -> dict[str, str]:
        """Return the formatted values of the fields sent for one slot.

        ``extra`` names passive fields to send too (chosen for a reupload).
        """
        step = self.upload_interval
        in_slot = [r for r in rows if slot - step < r.ts <= slot]
        values: dict[str, str] = {}
        measured = False
        for m in self._sendable(extra):
            value: float | None
            current = [r.values[m.field] for r in in_slot]
            valid = [v for v in current if v is not None]
            if m.entity_id is None:
                value = m.constant
            elif m.aggregate == AGG_ENERGY_TODAY:
                # Only with a counter reading in this interval; otherwise the
                # status would repeat an old total as if it were current.
                if valid:
                    found = [r.values[m.field] for r in day_rows if r.ts <= slot]
                    value = sum(v for v in found if v is not None)
                else:
                    value = None
            elif m.aggregate == AGG_LAST:
                # The reading at the end of the interval; none if unavailable then.
                value = current[-1] if current else None
            else:
                value = sum(valid) / len(valid) if valid else None
            if value is not None:
                values[m.field] = _fmt(value, m.decimals)
                measured = measured or m.entity_id is not None
        # Constants alone (e.g. battery size) do not make a status.
        return values if measured else {}

    async def _async_upload_statuses(self) -> None:
        active = self.active_mappings
        windows = await self._prune_passive_windows()
        # A site with only passive fields still sends what a reupload or import
        # chose for its period.
        if not active and not any(w["fields"] for w in windows):
            return
        step = self.upload_interval
        now = time.time()
        window = self.window_start(now)

        expired = await self._run(self.store.expire_before, window)
        if expired and self.entry.options.get(CONF_OUTPUT_FALLBACK, False):
            await self._queue_days({self._label_day(ts) for ts in expired})

        stored = await self._run(self.store.get_meta, META_LAST_BUILT)
        if not stored:
            return
        last_built = int(stored)
        battery = any(FIELDS_BY_KEY[m.field].battery for m in active)
        per_slot = step // LOG_INTERVAL
        # Statuses up to this slot are sent in batches without battery values,
        # as asked by a reupload (the action's without_battery).
        stored_until = await self._run(self.store.get_meta, META_NO_BATTERY_UNTIL)
        no_battery_until = int(stored_until) if stored_until and battery else None

        # Statuses PVOutput did not confirm are retried next cycle, not straight away.
        deferred: set[int] = set()

        # Slots ending after this are current: sent first, in the normal way, and
        # allowed to use the requests the backlog leaves (down to RATE_RESERVE).
        current_after = max(window, last_built - CURRENT_SLOTS * step + 1)

        for _ in range(MAX_REQUESTS_PER_CYCLE):
            current = await self._pending_slots(
                current_after, last_built, last_built, CURRENT_SLOTS * per_slot, deferred
            )
            if current:
                if not self._rate_ok(RATE_RESERVE):
                    return
                without_battery = False
                single = False
                slots = current[: self._batch_limit]
            else:
                # History: oldest first, and only while the backlog reserve allows.
                if not self._rate_ok(self._history_reserve()):
                    return
                without_battery = False
                if no_battery_until is not None:
                    first = await self._run(self.store.first_pending, window, last_built)
                    if first is not None and _slot_end(first, step) <= no_battery_until:
                        without_battery = True
                    else:
                        # The batch backlog is done; back to the normal way.
                        no_battery_until = None
                        await self._run(self.store.set_meta, META_NO_BATTERY_UNTIL, None)
                single = False
                limit = self._batch_limit
                slots = await self._pending_slots(
                    window,
                    min(last_built, no_battery_until) if without_battery else last_built,
                    last_built,
                    (limit + len(deferred)) * per_slot,
                    deferred,
                )
                slots = slots[:limit]
            if not slots:
                return

            first_day = _local(slots[0]).date()
            rows = await self._run(
                self.store.samples_between,
                self._day_bounds(first_day)[0],
                slots[-1] + 1,
            )
            entries: list[tuple[int, dict[str, str]]] = []
            for slot in slots:
                day_start, _ = self._day_bounds(_local(slot).date())
                day_rows = [r for r in rows if r.ts >= day_start]
                extra = self._passive_for(windows, slot, step)
                values = self._slot_values(slot, rows, day_rows, extra)
                if without_battery:
                    values = self._without_battery(values)
                if values:
                    entries.append((slot, values))
                else:
                    # Nothing to send (in a batch without battery: battery only).
                    await self._mark_slot(slot, DONE)
            if not entries:
                continue

            try:
                confirmed = await self._send(entries, single)
            except PVOutputRateLimited as err:
                await self._pause(err.reset, str(err))
                return
            except PVOutputAuthError as err:
                self.last_error = str(err)
                _LOGGER.error("PVOutput refused the API key: %s", err)
                return
            except PVOutputBadRequest as err:
                await self._handle_rejected(entries, str(err), deferred, single)
                continue

            for slot, _ in entries:
                if slot in confirmed:
                    await self._mark_slot(slot, DONE)
                    if battery:
                        # Remember statuses sent without their battery values, so
                        # it shows which periods a normal reupload would complete.
                        await self._run(
                            self.store.set_no_battery,
                            slot - step + 1,
                            slot + 1,
                            without_battery,
                        )
                else:
                    await self._unconfirmed(slot, "PVOutput did not add the status", deferred)
            if confirmed:
                await self._mark_uploaded()
                self.last_error = None
            self._batch_limit = min(self.batch_size, self._batch_limit * 2)

    async def _pending_slots(
        self, start: int, end: int, last_built: int, rows: int, deferred: set[int]
    ) -> list[int]:
        """Return the ends of complete upload slots, at most ``end``, with pending rows.

        Oldest first, leaving out slots retried later (deferred); ``rows`` limits
        how many 5 minute rows are read from ``start``.
        """
        step = self.upload_interval
        pending = await self._run(self.store.pending_ts, start, last_built, rows)
        slots: list[int] = []
        for ts in pending:
            slot = _slot_end(ts, step)
            # A slot is complete only once all its rows are recorded.
            if slot > end or slot > last_built:
                break
            if slot not in deferred and (not slots or slots[-1] != slot):
                slots.append(slot)
        return slots

    async def _send(
        self, entries: list[tuple[int, dict[str, str]]], single: bool
    ) -> set[int]:
        """Send statuses and return the slots PVOutput confirmed."""
        if single:
            slot, values = entries[0]
            local = _local(slot)
            await self._pace()
            await self.client.add_status(
                {"d": local.strftime("%Y%m%d"), "t": local.strftime("%H:%M")} | values
            )
            return {slot}
        await self._pace()
        answer = await self.client.add_batch_status(
            [self._batch_line(slot, values) for slot, values in entries]
        )
        return self._confirmed(answer, [slot for slot, _ in entries])

    @staticmethod
    def _confirmed(answer: str, slots: list[int]) -> set[int]:
        """Return the slots addbatchstatus.jsp reports as added (status 1).

        The answer holds ``yyyymmdd,hh:mm,status`` per status, separated by ``;``.
        Status 0 means the status did not add or update any data.
        """
        added: set[tuple[str, str]] = set()
        for item in answer.split(";"):
            parts = item.strip().split(",")
            if len(parts) >= 3 and parts[2].strip() == "1":
                added.add((parts[0].strip(), parts[1].strip()))
        return {
            slot
            for slot in slots
            if (_local(slot).strftime("%Y%m%d"), _local(slot).strftime("%H:%M")) in added
        }

    def _without_battery(self, values: dict[str, str]) -> dict[str, str]:
        """Drop battery values; nothing is left if only they were measured."""
        rest = {k: v for k, v in values.items() if not FIELDS_BY_KEY[k].battery}
        sensors = {
            m.field
            for m in self.active_mappings
            if m.entity_id and not FIELDS_BY_KEY[m.field].battery
        }
        return rest if sensors & rest.keys() else {}

    @property
    def sends_battery(self) -> bool:
        """Return whether battery fields are sent."""
        return any(FIELDS_BY_KEY[m.field].battery for m in self.active_mappings)

    def _batch_line(self, slot: int, values: dict[str, str]) -> str:
        local = _local(slot)
        cols = [values.get(k, "") for k in BATCH_KEYS]
        while cols and not cols[-1]:
            cols.pop()
        return ",".join([local.strftime("%Y%m%d"), local.strftime("%H:%M"), *cols])

    async def _unconfirmed(self, slot: int, message: str, deferred: set[int]) -> None:
        """Retry a status in later cycles, giving up after a while.

        The window starts at the first attempt PVOutput did not confirm, so old
        statuses (backlog, reupload, import) get the same retries as new ones.
        """
        now = time.time()
        # Kept in the log, so restarts and reloads do not start the window again.
        first = await self._run(self.store.unconfirmed_since, slot, now)
        if now - first < UNCONFIRMED_RETRY_SECONDS:
            deferred.add(slot)
            self.last_error = f"{_local(slot).isoformat()}: {message}, will retry"
            return
        await self._mark_slot(slot, FAILED)
        self.last_error = f"{_local(slot).isoformat()}: {message}"
        _LOGGER.warning("PVOutput did not take the status for %s: %s", _local(slot), message)

    async def _handle_rejected(
        self,
        entries: list[tuple[int, dict[str, str]]],
        message: str,
        deferred: set[int],
        single: bool,
    ) -> None:
        """Deal with a 400 answer."""
        if len(entries) > 1:
            # Split the batch until the status PVOutput objects to is found.
            self._batch_limit = max(1, len(entries) // 2)
            return
        slot, values = entries[0]
        text = message.lower()
        if "missing energy and power" in text or "v1, v2, v3 or v4" in text:
            # Only extra fields are sent and the uploader of v1-v4 (e.g. IoTaWatt)
            # has not created the status yet.
            await self._unconfirmed(slot, message, deferred)
            return
        if "older than" in text:
            await self._mark_slot(slot, EXPIRED)
            if self.entry.options.get(CONF_OUTPUT_FALLBACK, False):
                await self._queue_days({_local(slot).strftime("%Y%m%d")})
        else:
            retried = await self._send_without_rejected(slot, values, message, single)
            if retried is None:
                return  # rate limited: stays waiting, sent whole after the pause
            if not retried:
                await self._mark_slot(slot, FAILED)
        self.last_error = f"{_local(slot).isoformat()}: {message}"
        _LOGGER.warning("PVOutput rejected the status for %s: %s", _local(slot), message)

    async def _send_without_rejected(
        self, slot: int, values: dict[str, str], message: str, single: bool
    ) -> bool | None:
        """Retry once without the values PVOutput's message names.

        Returns True when the rest was accepted (the slot is marked partly sent,
        so a reupload since the first unsent status sends it whole), False when
        there is nothing to retry or it was rejected again, and None when the
        rate limit stopped it.
        """
        text = message.lower()
        drop = {
            key
            for phrase, keys in REJECTED_VALUE_FIELDS
            if phrase in text
            for key in keys
        } & values.keys()
        rest = {k: v for k, v in values.items() if k not in drop}
        sensors = {m.field for m in self.active_mappings if m.entity_id}
        if not drop or not sensors & rest.keys():
            return False
        try:
            confirmed = await self._send([(slot, rest)], single)
        except PVOutputRateLimited as err:
            await self._pause(err.reset, str(err))
            return None
        except PVOutputBadRequest as err:
            _LOGGER.warning(
                "PVOutput rejected the status for %s also without %s: %s",
                _local(slot), ", ".join(sorted(drop)), err,
            )
            return False
        if slot not in confirmed:
            return False
        await self._mark_slot(slot, PARTIAL)
        _LOGGER.warning(
            "Sent the status for %s without %s, which PVOutput rejected: %s",
            _local(slot), ", ".join(sorted(drop)), message,
        )
        return True

    async def _mark_slot(self, slot: int, state: int) -> None:
        step = self.upload_interval
        await self._run(self.store.mark_range, slot - step + 1, slot + 1, state)

    def _label_day(self, ts: int) -> str:
        return _local(_slot_end(ts, self.upload_interval)).strftime("%Y%m%d")

    # Daily outputs

    async def _queue_days(self, days: set[str]) -> None:
        today = dt_util.now().strftime("%Y%m%d")
        await self._run(self.store.queue_outputs, sorted(d for d in days if d < today))

    async def _day_output(self, day: str) -> dict[str, str] | None:
        """Return addoutput.jsp parameters for one day, or None if empty."""
        when = datetime.strptime(day, "%Y%m%d").date()
        start, end = self._day_bounds(when)
        rows = await self._run(self.store.samples_between, start, end)
        fields = {m.field: m for m in self.active_mappings if m.entity_id}
        out: dict[str, str] = {}

        def total(key: str) -> float | None:
            found = [r.values[key] for r in rows if r.values[key] is not None]
            return sum(found) if found else None

        for key, param in (("v1", "g"), ("v3", "c")):
            if key in fields and (value := total(key)) is not None:
                out[param] = _fmt(value, 0)
        if "v2" in fields:
            peaks = [r for r in rows if r.values["v2"] is not None]
            if peaks:
                top = max(peaks, key=lambda r: r.values["v2"] or 0)
                out["pp"] = _fmt(top.values["v2"] or 0, 0)
                out["pt"] = _local(_slot_end(top.ts, self.upload_interval)).strftime(
                    "%H:%M"
                )
        if "v5" in fields:
            temps = [r.values["v5"] for r in rows if r.values["v5"] is not None]
            if temps:
                out["tm"] = _fmt(min(temps), 1)
                out["tx"] = _fmt(max(temps), 1)
        if not out:
            return None
        return {"d": day} | out

    async def _async_upload_outputs(self) -> None:
        for _ in range(MAX_REQUESTS_PER_CYCLE):
            if not self._rate_ok():
                return
            size = OUTPUT_BATCH_DONOR if self.donor else 1
            days = await self._run(self.store.queued_outputs, size)
            if not days:
                return
            outputs = []
            for day in days:
                params = await self._day_output(day)
                if params is None:
                    await self._run(self.store.dequeue_outputs, [day])
                else:
                    outputs.append(params)
            if not outputs:
                continue
            await self._pace()
            try:
                if len(outputs) == 1:
                    await self.client.add_output(outputs[0])
                else:
                    await self.client.add_batch_output(
                        [self._output_line(p) for p in outputs]
                    )
            except PVOutputRateLimited as err:
                await self._pause(err.reset, str(err))
                return
            except PVOutputBadRequest as err:
                if len(outputs) > 1:
                    # Send this lot one by one to find the bad day.
                    for params in outputs:
                        if not await self._send_single_output(params):
                            return
                    continue
                await self._reject_output(outputs[0]["d"], str(err))
                continue
            await self._run(
                self.store.dequeue_outputs, [p["d"] for p in outputs]
            )
            await self._mark_uploaded()

    async def _send_single_output(self, params: dict[str, str]) -> bool:
        """Send one daily output. Return whether to keep going this cycle."""
        if not self._rate_ok():
            return False
        await self._pace()
        try:
            await self.client.add_output(params)
        except PVOutputRateLimited as err:
            await self._pause(err.reset, str(err))
            return False
        except PVOutputBadRequest as err:
            await self._reject_output(params["d"], str(err))
            return True
        await self._run(self.store.dequeue_outputs, [params["d"]])
        await self._mark_uploaded()
        return True

    async def _reject_output(self, day: str, message: str) -> None:
        """Keep a rejected day, with the reason, until it is reuploaded on purpose."""
        await self._run(self.store.reject_output, day, message)
        self.last_error = f"Daily output {day}: {message}"
        _LOGGER.warning("PVOutput rejected the daily output for %s: %s", day, message)

    @staticmethod
    def _output_line(p: dict[str, str]) -> str:
        # d,g,e,pp,pt,cd,tm,tx,cm,ip,io,is,ih,c
        order = ("d", "g", "e", "pp", "pt", "cd", "tm", "tx", "cm", "ip", "io", "is", "ih", "c")
        cols = [p.get(k, "") for k in order]
        while cols and not cols[-1]:
            cols.pop()
        return ",".join(cols)

    # Housekeeping

    async def _async_housekeeping(self) -> None:
        today = dt_util.now().strftime("%Y%m%d")
        if await self._run(self.store.get_meta, META_LAST_PURGE) == today:
            return
        days = self.entry.options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)
        before = _midnight(time.time()) - int(days) * 86400
        await self._run(self.store.purge, before)
        await self._run(self.store.set_meta, META_LAST_PURGE, today)

    # Services

    async def async_clear_queue(self) -> dict[str, int]:
        """Take the waiting history off the queue; current statuses stay.

        Cleared statuses stay in the log as unsent, so a reupload (also since
        the first unsent status) can send them again. Queued daily outputs are
        dropped, and a "leave out battery fields" run ends.
        """
        async with self.lock:
            stored = await self._run(self.store.get_meta, META_LAST_BUILT)
            last_built = int(stored) if stored else int(time.time())
            current_from = last_built - CURRENT_SLOTS * self.upload_interval + 1
            statuses = await self._run(self.store.clear_pending, current_from)
            outputs = await self._run(self.store.clear_queued_outputs)
            await self._run(self.store.set_meta, META_NO_BATTERY_UNTIL, None)
            # The queued periods' passive-field choices end with them.
            await self._save_passive_windows([])
            await self._refresh_summary()
        _LOGGER.info(
            "Cleared %s waiting samples and %s daily outputs from the queue",
            statuses,
            outputs,
        )
        return {"samples": statuses, "daily_outputs": outputs}

    async def async_first_unsent(self) -> int | None:
        """Return the oldest sample not confirmed by PVOutput (waiting, rejected or expired)."""
        return await self._run(self.store.first_unsent)

    async def async_reupload(
        self,
        start: datetime,
        end: date | datetime,
        method: str,
        without_battery: bool = False,
        passive_fields: Iterable[str] = (),
    ) -> dict[str, int]:
        """Queue history from ``start`` to ``end`` for sending again.

        ``end`` is a time (statuses up to it) or a day (to its end). With
        ``without_battery``, everything waiting up to now is sent without
        battery values; newer statuses keep them. ``passive_fields`` are sent
        too, for the statuses of this period only; a later reupload replaces
        the choice.
        """
        async with self.lock:
            await self._run(
                self.store.set_meta,
                META_NO_BATTERY_UNTIL,
                str(int(time.time())) if without_battery and self.sends_battery else None,
            )
            now = time.time()
            today = dt_util.now().date()
            if isinstance(end, datetime):
                end_ts: int | None = min(int(end.timestamp()), int(now))
                end_day = _local(end_ts).date()
            else:
                end_ts = None
                end_day = min(end, today)
            end = end_day
            window = self.window_start(now)
            window_day = _local(window).date()
            start_ts = int(start.timestamp())
            start_day = dt_util.as_local(start).date()
            status_days = 0
            output_days: set[str] = set()

            if method in (REUPLOAD_AUTO, REUPLOAD_STATUSES):
                lo = max(start_ts, self._day_bounds(window_day)[0])
                # Up to the given time (that row included), or the end of the day.
                hi = end_ts + 1 if end_ts is not None else self._day_bounds(end)[1]
                if lo < hi:
                    await self._run(self.store.mark_range, lo, hi, PENDING)
                    status_days = (end - max(start_day, window_day)).days + 1
                    # This period sends these passive fields (none: active only).
                    await self._set_passive_window(lo, hi - 1, passive_fields)
            if method in (REUPLOAD_AUTO, REUPLOAD_OUTPUTS):
                last = end if method == REUPLOAD_OUTPUTS else min(
                    end, window_day - timedelta(days=1)
                )
                day = start_day
                while day <= last:
                    output_days.add(day.strftime("%Y%m%d"))
                    day += timedelta(days=1)
                await self._queue_days(output_days)
            self._batch_limit = self.batch_size
            await self._refresh_summary()
        return {"status_days": status_days, "output_days": len(output_days)}

    async def async_import(
        self,
        start: datetime,
        end: datetime,
        overwrite: bool,
        upload: bool,
        passive_fields: Iterable[str] = (),
    ) -> dict[str, int]:
        """Fill the log from recorder states, then long term statistics.

        Recorded values are kept: states fill missing rows and empty fields
        (or replace rows with ``overwrite``), and hourly statistics then fill
        whatever 5 minute rows or fields are still missing. An hour's energy is
        shared over the missing slots after what recorded rows already hold, so
        the hour's total matches the statistics.
        """
        async with self.lock:
            stored = await self._run(self.store.get_meta, META_LAST_BUILT)
            limit = int(stored) if stored else _align_down(time.time(), LOG_INTERVAL)
            start_ts = _align_down(start.timestamp(), LOG_INTERVAL)
            end_ts = min(_align_down(end.timestamp(), LOG_INTERVAL), limit)
            state = PENDING if upload else DONE
            from_states = 0
            from_stats = 0

            t = start_ts
            # Counters carried across the day boundaries of this import.
            baselines: dict[str, Any] = {}
            while t < end_ts:
                chunk_end = min(end_ts, t + 86400)
                samples = await async_build_from_states(
                    self.hass, self.sources, t, chunk_end, baselines
                )
                for s in samples:
                    s.uploaded = state
                if overwrite:
                    await self._run(self.store.add_samples, samples, overwrite=True)
                    from_states += len(samples)
                else:
                    from_states += await self._run(
                        self.store.merge_samples, samples, resend=upload
                    )
                t = chunk_end

            hours = await async_build_from_statistics(
                self.hass,
                self.sources,
                dt_util.utc_from_timestamp(start_ts),
                dt_util.utc_from_timestamp(end_ts),
            )
            aggregates = {s.field: s.aggregate for s in self.sources}
            for hour_end, values in hours:
                if hour_end - 3600 < start_ts or hour_end > end_ts:
                    continue
                fill = await self._statistics_fill(hour_end, values, aggregates)
                if fill:
                    from_stats += await self._run(
                        self.store.merge_samples,
                        [Sample(ts, vals, state) for ts, vals in sorted(fill.items())],
                        resend=upload,
                    )
            if upload:
                # Like a reupload: the chosen passive fields are sent too, for
                # the imported period only (the latest choice replaces any other).
                if await self._set_passive_window(start_ts, end_ts, passive_fields):
                    # Rows already sent need sending again to carry them, within
                    # the period PVOutput still takes statuses for.
                    window_day = _local(self.window_start(time.time())).date()
                    lo = max(start_ts, self._day_bounds(window_day)[0])
                    if lo <= end_ts:
                        await self._run(self.store.mark_range, lo, end_ts + 1, PENDING)
            await self._refresh_summary()
        return {"from_states": from_states, "from_statistics": from_stats}

    async def _passive_windows(self) -> list[dict[str, Any]]:
        """Return the passive-field choices of reuploads and imports, oldest first.

        Each is {"from", "to", "fields"}: statuses ending in [from, to] also
        send those passive fields. A newer window wins where windows overlap.
        """
        stored = await self._run(self.store.get_meta, META_PASSIVE_REUPLOAD)
        if not stored:
            return []
        windows = json.loads(stored)
        # Before 0.1.6b10 a single window was stored.
        return windows if isinstance(windows, list) else [windows]

    async def _save_passive_windows(self, windows: list[dict[str, Any]]) -> None:
        await self._run(
            self.store.set_meta,
            META_PASSIVE_REUPLOAD,
            json.dumps(windows) if windows else None,
        )

    async def _set_passive_window(
        self, start: int, end: int, passive_fields: Iterable[str]
    ) -> bool:
        """Record which passive fields statuses in [start, end] also send.

        The new period is cut out of the older windows (an older window around
        it keeps its parts before and after), so windows never overlap: every
        period belongs to the newest choice made for it, and dropping a finished
        window cannot hand its period back to an older choice. Returns whether
        any (still passive) field was chosen.
        """
        fields = sorted(set(passive_fields) & {m.field for m in self.passive_mappings})
        windows: list[dict[str, Any]] = []
        for w in await self._passive_windows():
            if w["to"] < start or w["from"] > end:
                windows.append(w)
                continue
            if w["from"] < start:
                windows.append(w | {"to": start - 1})
            if w["to"] > end:
                windows.append(w | {"from": end + 1})
        windows.append({"from": start, "to": end, "fields": fields})
        await self._save_passive_windows(windows)
        return bool(fields)

    async def _prune_passive_windows(self) -> list[dict[str, Any]]:
        """Drop windows with nothing left to send in their period; return the rest."""
        windows = await self._passive_windows()
        keep = [
            w
            for w in windows
            if await self._run(self.store.first_pending, w["from"], w["to"]) is not None
        ]
        if len(keep) != len(windows):
            await self._save_passive_windows(keep)
        return keep

    @staticmethod
    def _passive_for(windows: list[dict[str, Any]], slot: int, step: int) -> list[str]:
        """Return the passive fields the slot ending at ``slot`` also sends.

        A window applies when one of the slot's 5 minute rows (ending in
        (slot - step, slot]) lies in it. A status longer than 5 minutes can
        hold rows of several windows (e.g. 15 minute statuses): it then sends
        the passive fields of all of them, each aggregated over the whole
        status like any other field. Windows themselves never overlap.
        """
        first_row = slot - step + LOG_INTERVAL
        fields: set[str] = set()
        for w in windows:
            lo = max(w["from"], first_row)
            lo = -(-lo // LOG_INTERVAL) * LOG_INTERVAL  # first row at or after lo
            if lo <= min(w["to"], slot):
                fields.update(w["fields"])
        return sorted(fields)

    async def _statistics_fill(
        self, hour_end: int, values: dict[str, float | None], aggregates: dict[str, str]
    ) -> dict[int, dict[str, float]]:
        """Return the values one statistics hour adds, per 5 minute slot.

        Only slots where a field is missing get it. Energy: the hour's change
        minus what recorded slots already hold, shared evenly by the rest;
        never negative: when recorded slots already exceed the statistics,
        they are kept, the rest get 0 and a warning is logged. Other fields:
        the hour's value.
        """
        stamps = [
            hour_end - 3600 + (n + 1) * LOG_INTERVAL for n in range(3600 // LOG_INTERVAL)
        ]
        rows = {
            s.ts: s
            for s in await self._run(self.store.samples_between, stamps[0], hour_end + 1)
        }
        fill: dict[int, dict[str, float]] = {}
        for field, value in values.items():
            if value is None:
                continue
            missing = [
                ts for ts in stamps if ts not in rows or rows[ts].values.get(field) is None
            ]
            if not missing:
                continue
            if aggregates.get(field) == AGG_ENERGY_TODAY:
                have = sum(
                    v for s in rows.values() if (v := s.values.get(field)) is not None
                )
                if have > value:
                    # Recorded readings are kept as they are; the gaps get nothing,
                    # so this hour holds more than the statistics say.
                    _LOGGER.warning(
                        "Import: recorded %s energy for the hour to %s (%.0f Wh) "
                        "exceeds the long term statistics (%.0f Wh); keeping the "
                        "recorded values, the missing slots get 0",
                        field,
                        _local(hour_end).isoformat(),
                        have,
                        value,
                    )
                share = max(value - have, 0.0) / len(missing)
                for ts in missing:
                    fill.setdefault(ts, {})[field] = share
            else:
                for ts in missing:
                    fill.setdefault(ts, {})[field] = value
        return fill
