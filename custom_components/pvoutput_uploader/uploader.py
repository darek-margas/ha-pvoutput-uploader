"""Record sensor history and upload it to PVOutput."""

from __future__ import annotations

import asyncio
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
import logging
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
)
from .builder import (
    Source,
    async_build_from_states,
    async_build_from_statistics,
    spread_hour,
)
from .const import (
    AGG_ENERGY_TODAY,
    AGG_LAST,
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
    SETTLE_SECONDS,
    STATUS_BATCH_DONOR,
    STATUS_BATCH_STANDARD,
    STATUS_DAYS_DONOR,
    STATUS_DAYS_STANDARD,
)
from .store import DONE, EXPIRED, FAILED, PARTIAL, PENDING, Sample, SampleStore

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

META_LAST_BUILT = "last_built"
META_PAUSED_UNTIL = "paused_until"
META_LAST_PURGE = "last_purge"

REUPLOAD_AUTO = "auto"
REUPLOAD_STATUSES = "statuses"
REUPLOAD_OUTPUTS = "outputs"

# Values a PVOutput error message names, sent again without them once.
REJECTED_VALUE_FIELDS = (
    ("power value", ("v2", "v4")),
    ("energy value", ("v1", "v3")),
    ("moon powered", ("v1", "v2")),
)

# Status fields addbatchstatus.jsp accepts, in its column order.
BATCH_KEYS = tuple(f"v{n}" for n in range(1, 13))


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
        )

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
        self.sources = [
            Source(m.field, m.entity_id, m.aggregate, m.unit)
            for m in self.mappings
            if m.entity_id
        ]
        self.upload_enabled = True
        self.lock = asyncio.Lock()
        self.paused_until: float | None = None
        self.last_upload: datetime | None = None
        self.last_error: str | None = None
        self.summary: dict[str, Any] = {}
        # Field key -> (timestamp, value) of its latest recorded value.
        self.latest: dict[str, tuple[int, float]] = {}
        self._batch_limit = self.batch_size

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
        return [
            m
            for m in self.mappings
            if m.active
            and (self.donor or not FIELDS_BY_KEY[m.field].donor)
            and (m.entity_id or m.constant is not None)
        ]

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
        await self._refresh_summary()

    # Main cycle

    async def async_cycle(self) -> None:
        """Record new slots, upload what is due and tidy the log."""
        async with self.lock:
            try:
                await self._async_record()
            except Exception:
                _LOGGER.exception("Reading recorder history failed")
            if self.upload_enabled:
                try:
                    await self._async_upload_statuses()
                    await self._async_upload_outputs()
                except PVOutputError as err:
                    self.last_error = str(err)
                    _LOGGER.warning("PVOutput upload stopped: %s", err)
            await self._async_housekeeping()
            await self._refresh_summary()

    async def _async_record(self) -> None:
        target = _align_down(time.time() - SETTLE_SECONDS, LOG_INTERVAL)
        stored = await self._run(self.store.get_meta, META_LAST_BUILT)
        last = int(stored) if stored else None
        if not self.sources or last is None or last > target:
            # Nothing to read, or a first start: begin logging from now on.
            await self._run(self.store.set_meta, META_LAST_BUILT, str(target))
            return
        last = _align_down(last, LOG_INTERVAL)
        for _ in range(MAX_CHUNKS_PER_CYCLE):
            if last >= target:
                break
            end = min(target, last + HISTORY_CHUNK_SECONDS)
            samples = await async_build_from_states(self.hass, self.sources, last, end)
            await self._run(self.store.add_samples, samples)
            last = end
            await self._run(self.store.set_meta, META_LAST_BUILT, str(last))

    # Rate limit

    def _rate_ok(self) -> bool:
        now = time.time()
        if self.paused_until is not None:
            if now < self.paused_until:
                return False
            self.paused_until = None
        c = self.client
        if (
            c.rate_remaining is not None
            and c.rate_remaining <= RATE_RESERVE
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
        self, slot: int, rows: list[Sample], day_rows: list[Sample]
    ) -> dict[str, str]:
        """Return the formatted values of the active fields for one slot."""
        step = self.upload_interval
        in_slot = [r for r in rows if slot - step < r.ts <= slot]
        values: dict[str, str] = {}
        measured = False
        for m in self.active_mappings:
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
        if not active:
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
        single = any(FIELDS_BY_KEY[m.field].battery for m in active)
        per_slot = step // LOG_INTERVAL

        # Statuses PVOutput did not confirm are retried next cycle, not straight away.
        deferred: set[int] = set()

        for _ in range(MAX_REQUESTS_PER_CYCLE):
            if not self._rate_ok():
                return
            limit = 1 if single else self._batch_limit
            pending = await self._run(
                self.store.pending_ts,
                window,
                last_built,
                (limit + len(deferred)) * per_slot,
            )
            slots: list[int] = []
            for ts in pending:
                slot = _slot_end(ts, step)
                if slot > last_built:
                    break
                if slot not in deferred and (not slots or slots[-1] != slot):
                    slots.append(slot)
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
                values = self._slot_values(slot, rows, day_rows)
                if values:
                    entries.append((slot, values))
                else:
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
                else:
                    await self._unconfirmed(slot, "PVOutput did not add the status", deferred)
            if confirmed:
                self.last_upload = dt_util.utcnow()
                self.last_error = None
            self._batch_limit = min(self.batch_size, self._batch_limit * 2)

    async def _send(
        self, entries: list[tuple[int, dict[str, str]]], single: bool
    ) -> set[int]:
        """Send statuses and return the slots PVOutput confirmed."""
        if single:
            slot, values = entries[0]
            local = _local(slot)
            await self.client.add_status(
                {"d": local.strftime("%Y%m%d"), "t": local.strftime("%H:%M")} | values
            )
            return {slot}
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
            self.last_upload = dt_util.utcnow()

    async def _send_single_output(self, params: dict[str, str]) -> bool:
        """Send one daily output. Return whether to keep going this cycle."""
        if not self._rate_ok():
            return False
        try:
            await self.client.add_output(params)
        except PVOutputRateLimited as err:
            await self._pause(err.reset, str(err))
            return False
        except PVOutputBadRequest as err:
            await self._reject_output(params["d"], str(err))
            return True
        await self._run(self.store.dequeue_outputs, [params["d"]])
        self.last_upload = dt_util.utcnow()
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

    async def async_first_unsent(self) -> int | None:
        """Return the oldest sample not confirmed by PVOutput (waiting, rejected or expired)."""
        return await self._run(self.store.first_unsent)

    async def async_reupload(
        self, start: datetime, end: date, method: str
    ) -> dict[str, int]:
        """Queue history from ``start`` to the end of day ``end`` for sending again."""
        async with self.lock:
            now = time.time()
            today = dt_util.now().date()
            end = min(end, today)
            window = self.window_start(now)
            window_day = _local(window).date()
            start_ts = int(start.timestamp())
            start_day = dt_util.as_local(start).date()
            status_days = 0
            output_days: set[str] = set()

            if method in (REUPLOAD_AUTO, REUPLOAD_STATUSES):
                lo = max(start_ts, self._day_bounds(window_day)[0])
                _, hi = self._day_bounds(end)
                if lo < hi:
                    await self._run(self.store.mark_range, lo, hi, PENDING)
                    status_days = (end - max(start_day, window_day)).days + 1
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
        self, start: datetime, end: datetime, overwrite: bool, upload: bool
    ) -> dict[str, int]:
        """Fill the log from recorder states and long term statistics."""
        async with self.lock:
            stored = await self._run(self.store.get_meta, META_LAST_BUILT)
            limit = int(stored) if stored else _align_down(time.time(), LOG_INTERVAL)
            start_ts = _align_down(start.timestamp(), LOG_INTERVAL)
            end_ts = min(_align_down(end.timestamp(), LOG_INTERVAL), limit)
            state = PENDING if upload else DONE
            from_states = 0
            from_stats = 0

            t = start_ts
            while t < end_ts:
                chunk_end = min(end_ts, t + 86400)
                samples = await async_build_from_states(
                    self.hass, self.sources, t, chunk_end
                )
                for s in samples:
                    s.uploaded = state
                await self._run(self.store.add_samples, samples, overwrite=overwrite)
                from_states += len(samples)
                t = chunk_end

            hours = await async_build_from_statistics(
                self.hass,
                self.sources,
                dt_util.utc_from_timestamp(start_ts),
                dt_util.utc_from_timestamp(end_ts),
            )
            existing = await self._run(self.store.existing_ts, start_ts, end_ts + 1)
            aggregates = {s.field: s.aggregate for s in self.sources}
            for hour_end, values in hours:
                if hour_end - 3600 < start_ts or hour_end > end_ts:
                    continue
                i = bisect_left(existing, hour_end - 3600 + 1)
                if i < len(existing) and existing[i] <= hour_end:
                    continue
                samples = spread_hour(hour_end, values, aggregates)
                for s in samples:
                    s.uploaded = state
                await self._run(self.store.add_samples, samples)
                from_stats += len(samples)
            await self._refresh_summary()
        return {"from_states": from_states, "from_statistics": from_stats}
