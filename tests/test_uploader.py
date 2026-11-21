"""Building statuses from the log and sending them."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from functools import partial
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvoutput_uploader import uploader as uploader_module
from custom_components.pvoutput_uploader.api import (
    PVOutputBadRequest,
    PVOutputRateLimited,
    HistoryStatus,
    StatusReading,
)
from custom_components.pvoutput_uploader.const import (
    AGG_AVERAGE,
    AGG_ENERGY_TODAY,
    AGG_LAST,
    CONF_DETECTED_DONOR,
    CONF_DETECTED_INTERVAL,
    CONF_FIELDS,
    CONF_OUTPUT_FALLBACK,
    CONF_SYSTEM_ID,
    CONF_UPLOAD_INTERVAL,
    DOMAIN,
)
from custom_components.pvoutput_uploader.store import (
    DONE,
    EXPIRED,
    FAILED,
    PARTIAL,
    PENDING,
    Sample,
    SampleStore,
)
from custom_components.pvoutput_uploader.uploader import (
    META_LAST_BUILT,
    Uploader,
    _slot_end,
)

from pvo_helpers import local_dt, local_ts


class FakeClient:
    """Records what would be sent; ``fail`` may return an exception to raise."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.singles: list[dict[str, str]] = []
        self.outputs: list[dict[str, str]] = []
        self.batch_outputs: list[list[str]] = []
        self.fail: Callable[[str, Any], Exception | None] | None = None
        self.rate_limit: int | None = None
        self.rate_remaining: int | None = None
        self.rate_reset: float | None = None
        # Times (hh:mm) addbatchstatus.jsp answers with status 0 for.
        self.not_added: set[str] = set()
        self.statuses: list[bool] = []
        # PVOutput history per day (yyyymmdd), and the days asked for.
        self.history: dict[str, list[HistoryStatus]] = {}
        self.history_calls: list[str] = []
        self.reading: StatusReading | None = None

    def _check(self, kind: str, payload: Any) -> None:
        if self.fail and (err := self.fail(kind, payload)):
            raise err

    async def add_batch_status(self, lines: list[str]) -> str:
        self._check("batch", lines)
        self.batches.append(lines)
        # Like PVOutput: date,time,status per status; 1 added, 0 nothing added.
        return ";".join(
            f"{d},{t},{0 if t in self.not_added else 1}"
            for d, t, *_ in (line.split(",") for line in lines)
        )

    async def add_status(self, params: dict[str, str]) -> str:
        self._check("single", params)
        self.singles.append(params)
        return "OK 200: Added Status"

    async def add_output(self, params: dict[str, str]) -> str:
        self._check("output", params)
        self.outputs.append(params)
        return ""

    async def add_batch_output(self, lines: list[str]) -> str:
        self._check("batch_output", lines)
        self.batch_outputs.append(lines)
        return ""

    async def get_status_history(self, day: str, extended: bool) -> list[HistoryStatus]:
        self._check("history", day)
        self.history_calls.append(day)
        return self.history.get(day, [])

    async def get_status(self, extended: bool) -> StatusReading:
        self._check("get_status", extended)
        self.statuses.append(extended)
        assert self.reading is not None
        return self.reading


def line(time: str, day: str = "20261010", **values: str) -> str:
    """Build the batch line PVOutput gets: v1-v12, then b1-b6, trailing empties trimmed."""
    cols = [values.get(k, "") for k in uploader_module.BATCH_KEYS]
    while cols and not cols[-1]:
        cols.pop()
    return ",".join([day, time, *cols])


def field(key: str, entity: str | None = "sensor.x", **extra: Any) -> dict[str, Any]:
    return {"field": key, "entity_id": entity, "active": True} | extra


async def make(
    hass: HomeAssistant,
    tmp_path: Path,
    fields: list[dict[str, Any]],
    donor: bool = False,
    interval: int = 5,
    **options: Any,
) -> Uploader:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_key": "key",
            CONF_SYSTEM_ID: "1",
            CONF_DETECTED_INTERVAL: interval,
            CONF_DETECTED_DONOR: donor,
        },
        options={CONF_FIELDS: fields} | options,
    )
    await hass.async_add_executor_job(partial(tmp_path.mkdir, parents=True, exist_ok=True))
    store = SampleStore(str(tmp_path / "log.db"))
    await hass.async_add_executor_job(store.open)
    up = Uploader(hass, entry, FakeClient(), store)  # type: ignore[arg-type]
    up.request_spacing = 0  # tests do not wait between requests
    await up.async_load()
    return up


async def add(up: Uploader, *samples: Sample, last_built: int | None = None) -> None:
    await up._run(up.store.add_samples, list(samples))
    if last_built is not None:
        await up._run(up.store.set_meta, META_LAST_BUILT, str(last_built))


async def states(up: Uploader, start: int, end: int) -> dict[int, int]:
    rows = await up._run(up.store.samples_between, start, end)
    return {r.ts: r.uploaded for r in rows}


@pytest.fixture
def now(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(local_dt(10, 10, 10, 20))
    return freezer


async def test_slot_end(hass: HomeAssistant) -> None:
    assert _slot_end(local_ts(10, 10, 10, 5), 900) == local_ts(10, 10, 10, 15)
    assert _slot_end(local_ts(10, 10, 10, 15), 900) == local_ts(10, 10, 10, 15)
    assert _slot_end(local_ts(10, 10, 23, 55), 900) == local_ts(10, 11, 0, 0)
    assert _slot_end(local_ts(10, 11, 0, 0), 300) == local_ts(10, 11, 0, 0)


async def test_day_bounds_cover_dst_change(hass: HomeAssistant, tmp_path: Path) -> None:
    await hass.config.async_set_time_zone("US/Pacific")
    up = await make(hass, tmp_path, [])
    start, end = up._day_bounds(date(2026, 11, 1))  # clocks go back: 25 hours
    assert end - start == 25 * 3600
    assert start == local_ts(11, 1) - 300 + 1


async def test_batch_with_energy_since_midnight(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [
            field("v1", aggregate=AGG_ENERGY_TODAY),
            field("v2", aggregate=AGG_AVERAGE),
            field("v5", aggregate=AGG_AVERAGE),
        ],
    )
    await add(
        up,
        # Already sent, and yesterday: must not count towards today's energy.
        Sample(local_ts(10, 9, 23, 55), {"v1": 1000.0}, DONE),
        # The slot ending at 00:00 belongs to the new day.
        Sample(local_ts(10, 10, 0, 0), {"v1": 5.0, "v2": 100.0, "v5": 20.04}),
        Sample(local_ts(10, 10, 10, 5), {"v1": 10.0, "v2": 200.0}),
        Sample(local_ts(10, 10, 10, 10), {"v1": 20.0, "v5": 21.0}),
        last_built=local_ts(10, 10, 10, 10),
    )
    await up._async_upload_statuses()
    # The two current slots go first, then the older one.
    assert up.client.batches == [
        ["20261010,10:05,15,200", "20261010,10:10,35,,,,21"],
        ["20261010,00:00,5,100,,,20"],
    ]
    assert set((await states(up, 0, 2**40)).values()) == {DONE}
    assert up.last_upload is not None

    # Nothing left: no further request.
    await up._async_upload_statuses()
    assert len(up.client.batches) == 2


async def test_fifteen_minute_interval(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [field("v1", aggregate=AGG_ENERGY_TODAY), field("v2"), field("v4", aggregate=AGG_LAST)],
        **{CONF_UPLOAD_INTERVAL: "15"},
    )
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v1": 1.0, "v2": 100.0, "v4": 1.0}),
        Sample(local_ts(10, 10, 10, 10), {"v1": 2.0, "v2": 200.0, "v4": 2.0}),
        Sample(local_ts(10, 10, 10, 15), {"v1": 3.0, "v2": 300.0, "v4": None}),
        # The 10:30 slot is not complete yet.
        Sample(local_ts(10, 10, 10, 20), {"v1": 4.0, "v2": 400.0}),
        last_built=local_ts(10, 10, 10, 20),
    )
    await up._async_upload_statuses()
    # v4 is unavailable at the end of the slot: no last value is sent.
    assert up.client.batches == [["20261010,10:15,6,200"]]
    st = await states(up, 0, 2**40)
    assert st[local_ts(10, 10, 10, 15)] == DONE
    assert st[local_ts(10, 10, 10, 20)] == PENDING


async def test_passive_and_donor_fields_are_not_sent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    fields = [
        field("v2"),
        field("v5", active=False),
        field("v12"),  # extended: donor only
    ]
    row = Sample(local_ts(10, 10, 10, 5), {"v2": 100.0, "v5": 20.0, "v12": 45.0})

    up = await make(hass, tmp_path / "a", fields, donor=False)
    await add(up, row, last_built=row.ts)
    await up._async_upload_statuses()
    assert up.client.batches == [["20261010,10:05,,100"]]

    up = await make(hass, tmp_path / "b", fields, donor=True)
    await add(up, row, last_built=row.ts)
    await up._async_upload_statuses()
    assert up.client.batches == [
        [",".join(["20261010", "10:05", "", "100", *[""] * 9, "45"])]
    ]


async def test_battery_fields_are_batched(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [
            field("v5"),
            field("b1"),
            field("b2", aggregate=AGG_LAST),
            field("b3", entity=None, constant=5544),
        ],
        donor=True,
    )
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v5": 20.0, "b1": -250.4, "b2": 81.25}),
        # Only the constant would be sent: no status.
        Sample(local_ts(10, 10, 10, 10), {}),
        Sample(local_ts(10, 10, 10, 15), {"b1": 100.0}),
        last_built=local_ts(10, 10, 10, 15),
    )
    await up._async_upload_statuses()
    assert up.client.singles == []
    # Battery values go in batches, after v12. 10:15 is current and goes first.
    assert up.client.batches == [
        [line("10:15", b1="100", b3="5544")],
        [line("10:05", v5="20", b1="-250", b2="81.2", b3="5544")],
    ]
    assert up.client.batches[1][0] == "20261010,10:05,,,,,20,,,,,,,,-250,81.2,5544"
    assert set((await states(up, 0, 2**40)).values()) == {DONE}


async def test_rate_limit_pauses_and_resumes(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 100.0}), last_built=ts)
    reset = local_ts(10, 10, 11, 0)
    up.client.fail = lambda kind, payload: PVOutputRateLimited("Exceeded", reset)

    await up._async_upload_statuses()
    assert up.paused_until == reset
    assert (await states(up, 0, 2**40))[ts] == PENDING
    assert await up._run(up.store.get_meta, "paused_until") == str(reset)

    # The pause survives a restart.
    again = Uploader(hass, up.entry, up.client, up.store)
    await again.async_load()
    assert again.paused_until == reset

    up.client.fail = None
    await up._async_upload_statuses()
    assert up.client.batches == []

    now.move_to(local_dt(10, 10, 11, 1))
    await up._async_upload_statuses()
    assert up.client.batches == [["20261010,10:05,,100"]]
    assert up.paused_until is None


async def test_rate_limit_reserve(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 100.0}), last_built=ts)
    up.client.rate_remaining = 2
    up.client.rate_reset = local_ts(10, 10, 11, 0)
    await up._async_upload_statuses()
    assert up.client.batches == []

    now.move_to(local_dt(10, 10, 11, 1))
    await up._async_upload_statuses()
    assert len(up.client.batches) == 1


async def test_rejected_status_is_isolated(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    times = [local_ts(10, 10, 10, m) for m in (5, 10, 15, 20)]
    await add(up, *(Sample(t, {"v2": 100.0}) for t in times), last_built=times[-1])

    def fail(kind: str, lines: list[str]) -> Exception | None:
        if any(",10:10," in line for line in lines):
            return PVOutputBadRequest("Bad request 400: Moon Powered")
        return None

    up.client.fail = fail
    await up._async_upload_statuses()
    st = await states(up, 0, 2**40)
    assert st[times[1]] == FAILED
    assert [st[t] for t in (times[0], times[2], times[3])] == [DONE, DONE, DONE]


async def test_missing_base_status_is_retried(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5")])
    ts = local_ts(10, 10, 10, 15)
    await add(up, Sample(ts, {"v5": 20.0}), last_built=ts)
    up.client.fail = lambda kind, payload: PVOutputBadRequest(
        "Bad request 400: Missing energy and power values"
    )
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[ts] == PENDING

    now.move_to(local_dt(10, 10, 13, 0))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[ts] == FAILED


async def test_old_statuses_expire_into_outputs(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")], **{CONF_OUTPUT_FALLBACK: True})
    old = local_ts(9, 20, 12, 0)  # beyond the 14 day window
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == EXPIRED
    assert await up._run(up.store.queued_outputs, 10) == ["20260920"]
    assert up.client.batches == []


async def test_old_statuses_expire_without_outputs(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    old = local_ts(9, 20, 12, 0)
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == EXPIRED
    assert await up._run(up.store.queued_outputs, 10) == []


async def test_donor_window_is_longer(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")], donor=True)
    old = local_ts(9, 20, 12, 0)
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    await up._async_upload_statuses()
    assert up.client.batches == [["20260920,12:00,,100"]]


async def test_daily_output(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [
            field("v1", aggregate=AGG_ENERGY_TODAY),
            field("v2"),
            field("v3", aggregate=AGG_ENERGY_TODAY),
            field("v5"),
        ],
    )
    await add(
        up,
        Sample(local_ts(10, 1, 12, 0), {"v1": 100.0, "v2": 1000.0, "v3": 50.0, "v5": 15.0}, DONE),
        Sample(local_ts(10, 1, 12, 5), {"v1": 200.0, "v2": 3000.0, "v3": 60.0, "v5": 25.0}, DONE),
    )
    await up._run(up.store.queue_outputs, ["20261001", "20260915"])
    await up._async_upload_outputs()
    # 15 September has no data and is dropped.
    assert up.client.outputs == [
        {"d": "20261001", "g": "300", "c": "110", "pp": "3000", "pt": "12:05", "tm": "15", "tx": "25"}
    ]
    assert await up._run(up.store.queued_outputs, 10) == []


async def test_daily_outputs_batched_for_donors(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)], donor=True)
    await add(
        up,
        Sample(local_ts(10, 1, 12, 0), {"v1": 100.0}, DONE),
        Sample(local_ts(10, 2, 12, 0), {"v1": 200.0}, DONE),
    )
    await up._run(up.store.queue_outputs, ["20261001", "20261002"])
    await up._async_upload_outputs()
    assert up.client.batch_outputs == [["20261001,100", "20261002,200"]]


async def test_reupload(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    recent = local_ts(10, 9, 10, 0)
    old = local_ts(9, 1, 10, 0)
    await add(up, Sample(recent, {"v2": 1.0}, DONE), Sample(old, {"v2": 1.0}, DONE))

    result = await up.async_reupload(local_dt(9, 1), date(2026, 10, 10), "auto")
    # Window: 27 September to 10 October. Before it: 1-26 September as outputs.
    assert result == {"status_days": 14, "output_days": 26}
    st = await states(up, 0, 2**40)
    assert st[recent] == PENDING
    assert st[old] == DONE
    queued = await up._run(up.store.queued_outputs, 100)
    assert queued[0] == "20260901"
    assert queued[-1] == "20260926"


async def test_reupload_outputs_only_skips_today(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    result = await up.async_reupload(local_dt(10, 8), date(2026, 10, 12), "outputs")
    assert result["status_days"] == 0
    assert await up._run(up.store.queued_outputs, 100) == ["20261008", "20261009"]


async def test_import_from_recorder(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)])
    await add(up, last_built=local_ts(10, 10, 10, 15))
    state_row = local_ts(10, 10, 10, 5)

    async def from_states(hass_, sources, start, end, baselines=None):
        return [Sample(state_row, {"v1": 7.0})] if start < state_row <= end else []

    async def from_statistics(hass_, sources, start, end):
        return [
            (local_ts(10, 10, 9, 0), {"v1": 120.0}),
            # After the end of the import: skipped.
            (local_ts(10, 10, 11, 0), {"v1": 999.0}),
        ]

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=from_states),
        patch.object(
            uploader_module, "async_build_from_statistics", side_effect=from_statistics
        ),
    ):
        result = await up.async_import(
            local_dt(10, 10, 8, 0), local_dt(10, 10, 12, 0), overwrite=False, upload=False
        )

    assert result == {"from_states": 1, "from_statistics": 12}
    rows = await up._run(up.store.samples_between, 0, 2**40)
    assert [r.ts for r in rows] == [
        *range(local_ts(10, 10, 8, 5), local_ts(10, 10, 9, 0) + 1, 300),
        state_row,
    ]
    assert rows[0].values["v1"] == pytest.approx(10.0)
    assert {r.uploaded for r in rows} == {DONE}


async def test_reupload_from_a_time(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    times = [local_ts(10, 9, 10, m) for m in (0, 5, 10)]
    await add(up, *(Sample(t, {"v2": 1.0}, DONE) for t in times))
    result = await up.async_reupload(local_dt(10, 9, 10, 5), date(2026, 10, 10), "statuses")
    assert result == {"status_days": 2, "output_days": 0}
    st = await states(up, 0, 2**40)
    assert [st[t] for t in times] == [DONE, PENDING, PENDING]


async def test_first_unsent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    assert await up.async_first_unsent() is None
    await add(
        up,
        Sample(local_ts(10, 9, 10, 0), {"v2": 1.0}, DONE),
        Sample(local_ts(10, 9, 10, 5), {"v2": 1.0}, FAILED),
        Sample(local_ts(10, 9, 10, 10), {"v2": 1.0}, PENDING),
    )
    assert await up.async_first_unsent() == local_ts(10, 9, 10, 5)


async def test_only_confirmed_statuses_count_as_sent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    times = [local_ts(10, 10, 10, m) for m in (5, 10, 15)]
    await add(up, *(Sample(t, {"v2": 100.0}) for t in times), last_built=times[-1])
    up.client.not_added = {"10:10"}

    await up._async_upload_statuses()
    st = await states(up, 0, 2**40)
    assert [st[t] for t in times] == [DONE, PENDING, DONE]
    # The current slots, then the older one; 10:10 is not resent in the same cycle.
    assert up.client.batches == [
        ["20261010,10:10,,100", "20261010,10:15,,100"],
        ["20261010,10:05,,100"],
    ]

    # Retried in later cycles, then given up on.
    await up._async_upload_statuses()
    assert up.client.batches[-1] == ["20261010,10:10,,100"]
    assert (await states(up, 0, 2**40))[times[1]] == PENDING
    now.move_to(local_dt(10, 10, 12, 30))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[times[1]] == FAILED


async def test_unavailable_values_are_not_sent_as_current(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [field("v1", aggregate=AGG_ENERGY_TODAY), field("v2"), field("v4", aggregate=AGG_LAST)],
        **{CONF_UPLOAD_INTERVAL: "15"},
    )
    gap: dict[str, float | None] = {"v1": None, "v2": None, "v4": None}
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v1": 1.0, "v2": 100.0, "v4": 100.0}),
        Sample(local_ts(10, 10, 10, 10), gap),
        Sample(local_ts(10, 10, 10, 15), gap),
        # Only power reports in the next slot; the energy counter is unavailable.
        Sample(local_ts(10, 10, 10, 20), {"v1": None, "v2": 200.0, "v4": None}),
        Sample(local_ts(10, 10, 10, 25), gap),
        Sample(local_ts(10, 10, 10, 30), gap),
        last_built=local_ts(10, 10, 10, 30),
    )
    await up._async_upload_statuses()
    assert up.client.batches == [
        [
            # v4 read 100 but was unavailable at 10:15: not sent as current.
            "20261010,10:15,1,100",
            # No counter reading in this slot: today's total is not repeated.
            "20261010,10:30,,200",
        ]
    ]


async def test_old_statuses_get_the_full_retry_window(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    old = local_ts(10, 9, 10, 5)  # yesterday, e.g. imported or reuploaded
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    up.client.not_added = {"10:05"}

    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == PENDING

    now.move_to(local_dt(10, 10, 11, 20))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == PENDING

    now.move_to(local_dt(10, 10, 12, 30))
    await up._async_upload_statuses()
    assert (await states(up, 0, 2**40))[old] == FAILED


async def test_retry_window_survives_restart(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    up.client.not_added = {"10:05"}
    await up._async_upload_statuses()  # first unconfirmed attempt at 10:20

    # Home Assistant restarts (or the options are saved) at 11:30.
    now.move_to(local_dt(10, 10, 11, 30))
    again = Uploader(hass, up.entry, up.client, up.store)
    again.request_spacing = 0
    await again.async_load()
    await again._async_upload_statuses()
    assert (await states(again, 0, 2**40))[ts] == PENDING

    # Two hours after the first attempt, not after the restart.
    now.move_to(local_dt(10, 10, 12, 25))
    await again._async_upload_statuses()
    assert (await states(again, 0, 2**40))[ts] == FAILED


async def test_rejected_daily_output_is_kept(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)])
    await add(up, Sample(local_ts(10, 1, 12, 0), {"v1": 100.0}, DONE))
    await up._run(up.store.queue_outputs, ["20261001"])
    up.client.fail = lambda kind, payload: PVOutputBadRequest("Bad request 400: Invalid")

    await up._async_upload_outputs()
    assert await up._run(up.store.queued_outputs, 10) == []
    assert await up._run(up.store.rejected_outputs) == [("20261001", "Bad request 400: Invalid")]
    assert up.last_upload is None
    assert "20261001" in (up.last_error or "")

    # A reupload queues it again, and it is sent once PVOutput takes it.
    up.client.fail = None
    await up.async_reupload(local_dt(10, 1), date(2026, 10, 1), "outputs")
    await up._async_upload_outputs()
    assert [p["d"] for p in up.client.outputs] == ["20261001"]
    assert await up._run(up.store.rejected_outputs) == []
    assert up.last_upload is not None


async def test_one_bad_day_in_a_batch_of_outputs(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)], donor=True)
    await add(
        up,
        Sample(local_ts(10, 1, 12, 0), {"v1": 100.0}, DONE),
        Sample(local_ts(10, 2, 12, 0), {"v1": 200.0}, DONE),
    )
    await up._run(up.store.queue_outputs, ["20261001", "20261002"])

    def fail(kind: str, payload: Any) -> Exception | None:
        text = ";".join(payload) if isinstance(payload, list) else payload["d"]
        return PVOutputBadRequest("Bad request 400: Invalid") if "20261002" in text else None

    up.client.fail = fail
    await up._async_upload_outputs()
    assert [p["d"] for p in up.client.outputs] == ["20261001"]
    assert await up._run(up.store.queued_outputs, 10) == []
    assert [d for d, _ in await up._run(up.store.rejected_outputs)] == ["20261002"]


def _counting(fail: Callable[[list[str]], Exception | None]) -> tuple[list[int], Any]:
    attempts = [0]

    def check(kind: str, lines: list[str]) -> Exception | None:
        attempts[0] += 1
        return fail(lines)

    return attempts, check


async def test_rejected_value_is_left_out_once(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 99999.0, "v5": 20.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest(
            "Bad request 400: Power value 99999 too high for system size 13320"
        )
        if any(",99999" in line for line in lines)
        else None
    )
    await up._async_upload_statuses()
    # The temperature still reaches PVOutput, without the rejected power.
    assert up.client.batches == [["20261010,10:05,,,,,20"]]
    assert attempts[0] == 2
    assert (await states(up, 0, 2**40))[ts] == PARTIAL
    # Partly sent counts as unsent, for a reupload once the value is fixed.
    assert await up.async_first_unsent() == ts


async def test_unknown_rejection_is_not_retried(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 500.0, "v5": 20.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest("Bad request 400: Something else")
    )
    await up._async_upload_statuses()
    assert attempts[0] == 1
    assert (await states(up, 0, 2**40))[ts] == FAILED


async def test_rejected_again_without_the_value(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY), field("v5")])
    ts = local_ts(10, 10, 2, 0)
    await add(up, Sample(ts, {"v1": 50.0, "v5": 20.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest("Bad request 400: Moon Powered")
    )
    await up._async_upload_statuses()
    assert attempts[0] == 2  # the full status, then once without v1
    assert up.client.batches == []
    assert (await states(up, 0, 2**40))[ts] == FAILED


async def test_no_retry_when_only_fixed_values_would_be_left(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v6", entity=None, constant=230)])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 99999.0}), last_built=ts)
    attempts, up.client.fail = _counting(
        lambda lines: PVOutputBadRequest("Bad request 400: Power value 99999 too high")
    )
    await up._async_upload_statuses()
    assert attempts[0] == 1
    assert (await states(up, 0, 2**40))[ts] == FAILED


async def test_requests_are_spaced(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5"), field("b1")], donor=True)
    times = [local_ts(10, 10, 10, m) for m in (5, 10, 15)]
    await add(up, *(Sample(t, {"v5": 20.0, "b1": 100.0}) for t in times), last_built=times[-1])
    up.request_spacing = 5
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    with patch.object(uploader_module, "_sleep", side_effect=fake_sleep):
        await up._async_upload_statuses()
    # The two current statuses in one batch, then the older one: the second waits.
    assert len(up.client.batches) == 2
    assert len(waits) == 1
    assert all(4 < w <= 5 for w in waits)


async def test_battery_fields_need_b1(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    fields = [field("v5"), field("b2", aggregate=AGG_LAST), field("b3", entity=None, constant=5544)]
    up = await make(hass, tmp_path, fields, donor=True)
    # PVOutput would ignore b2 and b3 without b1, so they are not sent.
    assert [m.field for m in up.active_mappings] == ["v5"]
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v5": 20.0, "b2": 80.0}), last_built=ts)
    await up._async_upload_statuses()
    assert up.client.batches == [["20261010,10:05,,,,,20"]]
    assert up.client.singles == []

    # An inactive b1 does not count either.
    up = await make(hass, tmp_path / "b", [*fields, field("b1", active=False)], donor=True)
    assert [m.field for m in up.active_mappings] == ["v5"]


async def test_fixed_battery_state_is_sent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass, tmp_path, [field("b1"), field("b6", entity=None, constant=3)], donor=True
    )
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"b1": -250.0}), last_built=ts)
    await up._async_upload_statuses()
    assert up.client.batches == [[line("10:05", b1="-250", b6="3")]]
    assert up.client.singles == []


async def test_backlog_without_battery(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5"), field("b1")], donor=True)
    # History from an hour ago; nothing current is waiting.
    times = [local_ts(10, 10, 9, m) for m in (5, 10, 15)]
    await add(
        up,
        Sample(times[0], {"v5": 20.0, "b1": -100.0}, DONE),
        Sample(times[1], {"v5": 21.0, "b1": -200.0}, DONE),
        Sample(times[2], {"b1": -300.0}, DONE),  # battery only
        last_built=local_ts(10, 10, 10, 15),
    )
    await up.async_reupload(local_dt(10, 10), date(2026, 10, 10), "statuses", without_battery=True)
    await up._async_upload_statuses()

    # One batch, no battery values; the battery-only status is skipped.
    assert up.client.batches == [["20261010,09:05,,,,,20", "20261010,09:10,,,,,21"]]
    assert up.client.singles == []
    assert set((await states(up, 0, 2**40)).values()) == {DONE}
    assert await up._run(up.store.get_meta, "no_battery_until") is None

    # New statuses keep their battery values.
    ts = local_ts(10, 10, 10, 25)
    await add(up, Sample(ts, {"v5": 22.0, "b1": -400.0}), last_built=ts)
    now.move_to(local_dt(10, 10, 10, 30))
    await up._async_upload_statuses()
    assert up.client.batches[-1] == [line("10:25", v5="22", b1="-400")]
    assert up.client.singles == []


async def test_reupload_with_battery_clears_the_batch_mode(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5"), field("b1")], donor=True)
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v5": 20.0, "b1": -100.0}, DONE), last_built=ts)
    await up.async_reupload(local_dt(10, 10), date(2026, 10, 10), "statuses", without_battery=True)
    # Changed your mind: a normal reupload sends the battery values again.
    await up.async_reupload(local_dt(10, 10), date(2026, 10, 10), "statuses")
    await up._async_upload_statuses()
    assert up.client.batches == [[line("10:05", v5="20", b1="-100")]]
    assert up.client.singles == []


async def test_without_battery_needs_battery_fields(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5")], donor=True)
    assert up.sends_battery is False
    await up.async_reupload(local_dt(10, 10), date(2026, 10, 10), "statuses", without_battery=True)
    assert await up._run(up.store.get_meta, "no_battery_until") is None


async def test_five_requests_are_kept_in_reserve(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    ts = local_ts(10, 10, 10, 5)
    await add(up, Sample(ts, {"v2": 100.0}), last_built=ts)
    up.client.rate_reset = local_ts(10, 10, 11, 0)

    up.client.rate_remaining = 5
    await up._async_upload_statuses()
    assert up.client.batches == []

    up.client.rate_remaining = 6
    await up._async_upload_statuses()
    assert len(up.client.batches) == 1


async def test_current_statuses_go_before_history(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5"), field("b1")], donor=True)
    old = [local_ts(10, 9, 12, m) for m in (0, 5, 10)]
    current = local_ts(10, 10, 10, 15)
    await add(
        up,
        *(Sample(t, {"v5": 20.0, "b1": -100.0}) for t in old),
        Sample(current, {"v5": 21.0, "b1": -200.0}),
        last_built=current,
    )
    # 10 requests left: below the history reserve (5 + 8 statuses due before
    # 11:00 + 1 = 14), above the reserve for other uploaders (5).
    up.client.rate_remaining = 10
    up.client.rate_reset = local_ts(10, 10, 11, 0)
    await up._async_upload_statuses()

    # Only the current status goes; history waits for the next hour.
    assert up.client.batches == [[line("10:15", v5="21", b1="-200")]]
    st = await states(up, 0, 2**40)
    assert [st[t] for t in old] == [PENDING, PENDING, PENDING]


async def test_history_keeps_its_own_reserve(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    old = local_ts(10, 9, 12, 0)
    await add(up, Sample(old, {"v2": 100.0}), last_built=local_ts(10, 10, 10, 15))
    up.client.rate_reset = local_ts(10, 10, 11, 0)

    # 40 minutes to the reset at 11:00: 8 current statuses due, so history
    # leaves 5 + 8 + 1 = 14 requests.
    up.client.rate_remaining = 14
    await up._async_upload_statuses()
    assert up.client.batches == []

    up.client.rate_remaining = 15
    await up._async_upload_statuses()
    assert up.client.batches == [["20261009,12:00,,100"]]


async def test_history_reserve_follows_the_reset(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])  # 5 minute interval; now 10:20
    # Not known yet: a full hour, 12 statuses.
    assert up._history_reserve() == 5 + 12 + 1
    up.client.rate_reset = local_ts(10, 10, 11, 0)
    assert up._history_reserve() == 5 + 8 + 1
    now.move_to(local_dt(10, 10, 10, 58))
    assert up._history_reserve() == 5 + 1 + 1
    # Past the reset: a new hour.
    now.move_to(local_dt(10, 10, 11, 1))
    assert up._history_reserve() == 5 + 12 + 1

    up = await make(hass, tmp_path / "b", [field("v2")], **{CONF_UPLOAD_INTERVAL: "15"})
    up.client.rate_reset = local_ts(10, 10, 11, 31)
    assert up._history_reserve() == 5 + 2 + 1  # 30 minutes, 15 minute interval


async def test_clear_queue_keeps_current_statuses(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    old = [local_ts(10, 9, 12, m) for m in (0, 5)]
    current = local_ts(10, 10, 10, 15)
    await add(
        up,
        *(Sample(t, {"v2": 1.0}) for t in old),
        Sample(current, {"v2": 2.0}),
        last_built=current,
    )
    await up._run(up.store.queue_outputs, ["20261001"])
    await up._run(up.store.set_meta, "no_battery_until", "1")

    assert await up.async_clear_queue() == {"samples": 2, "daily_outputs": 1}
    assert await up._run(up.store.get_meta, "no_battery_until") is None
    assert up.summary["cleared"] == 2
    await up._async_upload_statuses()
    await up._async_upload_outputs()
    # Only the current status is sent.
    assert up.client.batches == [["20261010,10:15,,2"]]
    assert up.client.outputs == []

    # Cleared history can be sent again since the first unsent status.
    assert await up.async_first_unsent() == old[0]
    await up.async_reupload(local_dt(10, 9, 12, 0), date(2026, 10, 10), "statuses")
    await up._async_upload_statuses()
    assert up.client.batches[-1] == ["20261009,12:00,,1", "20261009,12:05,,1"]


async def test_reupload_up_to_a_time(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    times = [local_ts(10, 9, 10, m) for m in (0, 5, 10, 15)]
    await add(up, *(Sample(t, {"v2": 1.0}, DONE) for t in times))
    result = await up.async_reupload(
        local_dt(10, 9, 10, 5), local_dt(10, 9, 10, 10), "statuses"
    )
    assert result == {"status_days": 1, "output_days": 0}
    st = await states(up, 0, 2**40)
    # From 10:05 to 10:10, both included.
    assert [st[t] for t in times] == [DONE, PENDING, PENDING, DONE]

    # A time in the future stops at now.
    await up.async_reupload(local_dt(10, 9, 10, 0), local_dt(10, 12, 0, 0), "statuses")
    st = await states(up, 0, 2**40)
    assert [st[t] for t in times] == [PENDING] * 4


async def test_reupload_sends_chosen_passive_fields(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5", active=False)])
    assert [m.field for m in up.passive_mappings] == ["v5"]
    old = [local_ts(10, 9, 12, m) for m in (0, 5)]
    current = local_ts(10, 10, 10, 15)
    await add(
        up,
        *(Sample(t, {"v2": 100.0, "v5": 20.0}, DONE) for t in old),
        Sample(current, {"v2": 200.0, "v5": 21.0}),
        last_built=current,
    )
    await up.async_reupload(
        local_dt(10, 9, 12, 0), local_dt(10, 9, 12, 5), "statuses", passive_fields=["v5"]
    )
    await up._async_upload_statuses()
    # The current status keeps the passive field off; the reuploaded period sends it.
    assert up.client.batches == [
        [line("10:15", v2="200")],
        [line("12:00", day="20261009", v2="100", v5="20"),
         line("12:05", day="20261009", v2="100", v5="20")],
    ]

    # A reupload without passive fields sends the active ones only again.
    await up.async_reupload(local_dt(10, 9, 12, 0), local_dt(10, 9, 12, 5), "statuses")
    # It replaces the earlier choice for that period: no passive fields there.
    assert [w["fields"] for w in await up._passive_windows()] == [[]]
    await up._async_upload_statuses()
    assert up.client.batches[-1] == [
        line("12:00", day="20261009", v2="100"),
        line("12:05", day="20261009", v2="100"),
    ]


async def test_passive_fields_keep_the_rules(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    # Standard account: a passive extended field cannot be sent.
    up = await make(hass, tmp_path, [field("v2"), field("v12", active=False)])
    assert [m.field for m in up._sendable(["v12"])] == ["v2"]
    # Donor: battery fields need b1, active or chosen too.
    up = await make(
        hass, tmp_path / "b", [field("v2"), field("b1", active=False), field("b2", active=False)],
        donor=True,
    )
    assert [m.field for m in up._sendable(["b2"])] == ["v2"]
    assert [m.field for m in up._sendable(["b1", "b2"])] == ["v2", "b1", "b2"]
    # Fields that are not passive are ignored.
    await up.async_reupload(local_dt(10, 10), date(2026, 10, 10), "statuses", passive_fields=["v9"])
    assert all(not w["fields"] for w in await up._passive_windows())


async def test_import_fills_a_partial_hour(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY), field("v2")]
    )
    # One recorded slot in the hour 9:00-10:00, with energy but no power.
    recorded = local_ts(10, 10, 9, 5)
    await add(up, Sample(recorded, {"v1": 5.0}, DONE), last_built=local_ts(10, 10, 10, 15))

    async def from_states(hass_, sources, start, end, baselines=None):
        return []

    async def from_statistics(hass_, sources, start, end):
        return [(local_ts(10, 10, 10, 0), {"v1": 60.0, "v2": 300.0})]

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=from_states),
        patch.object(
            uploader_module, "async_build_from_statistics", side_effect=from_statistics
        ),
    ):
        result = await up.async_import(
            local_dt(10, 10, 9, 0), local_dt(10, 10, 10, 0), overwrite=False, upload=False
        )

    # 11 missing slots added, and the recorded one gets its missing power.
    assert result == {"from_states": 0, "from_statistics": 12}
    rows = await up._run(up.store.samples_between, local_ts(10, 10, 9, 0) + 1, local_ts(10, 10, 10, 0) + 1)
    assert len(rows) == 12
    by_ts = {r.ts: r.values for r in rows}
    assert by_ts[recorded]["v1"] == 5.0  # recorded value kept
    assert by_ts[recorded]["v2"] == 300.0
    # The hour's 60 Wh: 5 recorded, the other 55 shared by the 11 missing slots.
    assert sum(v["v1"] for v in by_ts.values()) == pytest.approx(60.0)
    assert all(v["v1"] == pytest.approx(5.0) for ts, v in by_ts.items() if ts != recorded)
    assert all(v["v2"] == 300.0 for v in by_ts.values())


async def test_recorder_failure_sends_only_what_was_read(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    built = local_ts(10, 10, 10, 0)
    await add(up, Sample(built, {"v2": 100.0}), last_built=built)

    with patch.object(
        uploader_module, "async_build_from_states", side_effect=RuntimeError("db locked")
    ):
        await up.async_cycle()
    # What was read before is sent; nothing past it is invented.
    assert await up._run(up.store.get_meta, "last_built") == str(built)
    assert up.client.batches == [["20261010,10:00,,100"]]
    assert up.record_error == "Recorder history could not be read: db locked"

    # Reading works again: the error goes, recording catches up.
    with patch.object(uploader_module, "async_build_from_states", return_value=[]):
        await up.async_cycle()
    assert up.record_error is None
    assert int(await up._run(up.store.get_meta, "last_built")) > built


async def test_statuses_sent_without_battery_are_flagged(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5"), field("b1")], donor=True)
    times = [local_ts(10, 10, 9, m) for m in (5, 10)]
    await add(
        up,
        *(Sample(t, {"v5": 20.0, "b1": -100.0}, DONE) for t in times),
        last_built=local_ts(10, 10, 10, 15),
    )
    await up.async_reupload(local_dt(10, 10), date(2026, 10, 10), "statuses", without_battery=True)
    await up._async_upload_statuses()
    assert (await up._run(up.store.summary))["without_battery"] == 2

    # A normal reupload sends them complete and clears the flag.
    await up.async_reupload(local_dt(10, 10), date(2026, 10, 10), "statuses")
    await up._async_upload_statuses()
    assert up.client.batches[-1] == [
        line("09:05", v5="20", b1="-100"),
        line("09:10", v5="20", b1="-100"),
    ]
    assert (await up._run(up.store.summary))["without_battery"] == 0


async def test_import_keeps_recorded_energy_above_statistics(
    hass: HomeAssistant,
    tmp_path: Path,
    now: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)])
    recorded = [local_ts(10, 10, 9, m) for m in (5, 10)]
    await add(
        up,
        *(Sample(t, {"v1": 475.0}, DONE) for t in recorded),  # 950 Wh recorded
        last_built=local_ts(10, 10, 10, 15),
    )

    async def from_states(hass_, sources, start, end, baselines=None):
        return []

    async def from_statistics(hass_, sources, start, end):
        return [(local_ts(10, 10, 10, 0), {"v1": 900.0})]  # statistics say 900

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=from_states),
        patch.object(
            uploader_module, "async_build_from_statistics", side_effect=from_statistics
        ),
    ):
        await up.async_import(
            local_dt(10, 10, 9, 0), local_dt(10, 10, 10, 0), overwrite=False, upload=False
        )

    rows = await up._run(
        up.store.samples_between, local_ts(10, 10, 9, 0) + 1, local_ts(10, 10, 10, 0) + 1
    )
    by_ts = {r.ts: r.values["v1"] for r in rows}
    # Recorded readings are kept, the missing slots get 0: the hour holds 950.
    assert [by_ts[t] for t in recorded] == [475.0, 475.0]
    assert sum(by_ts.values()) == pytest.approx(950.0)
    assert len(by_ts) == 12
    assert "exceeds the long term statistics" in caplog.text


async def test_import_sends_chosen_passive_fields(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5", active=False)])
    # Already in the log and sent, but without the passive v5.
    times = [local_ts(10, 9, 12, m) for m in (0, 5)]
    await add(
        up,
        *(Sample(t, {"v2": 100.0, "v5": 20.0}, DONE) for t in times),
        last_built=local_ts(10, 10, 10, 15),
    )

    async def nothing(*args):
        return []

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=nothing),
        patch.object(uploader_module, "async_build_from_statistics", side_effect=nothing),
    ):
        await up.async_import(
            local_dt(10, 9, 12, 0), local_dt(10, 9, 12, 5),
            overwrite=False, upload=True, passive_fields=["v5"],
        )
    await up._async_upload_statuses()
    # Sent again, now with v5.
    assert up.client.batches == [
        [line("12:00", day="20261009", v2="100", v5="20"),
         line("12:05", day="20261009", v2="100", v5="20")],
    ]

    # Without upload, the import leaves the choice and the queue alone.
    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=nothing),
        patch.object(uploader_module, "async_build_from_statistics", side_effect=nothing),
    ):
        await up.async_import(
            local_dt(10, 9, 12, 0), local_dt(10, 9, 12, 5),
            overwrite=False, upload=False, passive_fields=["v5"],
        )
    st = await states(up, 0, 2**40)
    assert [st[t] for t in times] == [DONE, DONE]


async def test_two_operations_keep_their_passive_fields(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass, tmp_path, [field("v2"), field("v5", active=False), field("v6", active=False)]
    )
    a = [local_ts(10, 9, 12, m) for m in (0, 5)]
    b = [local_ts(10, 9, 14, m) for m in (0, 5)]
    await add(
        up,
        *(Sample(t, {"v2": 100.0, "v5": 20.0, "v6": 230.0}, DONE) for t in a + b),
        last_built=local_ts(10, 10, 10, 15),
    )
    # A reupload with v5, then, before it is sent, another one with v6.
    await up.async_reupload(local_dt(10, 9, 12, 0), local_dt(10, 9, 12, 5), "statuses", passive_fields=["v5"])
    await up.async_reupload(local_dt(10, 9, 14, 0), local_dt(10, 9, 14, 5), "statuses", passive_fields=["v6"])
    await up._async_upload_statuses()
    assert up.client.batches == [
        [
            line("12:00", day="20261009", v2="100", v5="20"),
            line("12:05", day="20261009", v2="100", v5="20"),
            line("14:00", day="20261009", v2="100", v6="230"),
            line("14:05", day="20261009", v2="100", v6="230"),
        ]
    ]
    # Everything sent: the windows are dropped on the next cycle.
    await up._async_upload_statuses()
    assert await up._passive_windows() == []


async def test_newest_choice_wins_where_periods_overlap(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5", active=False)])
    a = local_ts(10, 9, 12, 0)
    b = local_ts(10, 9, 14, 0)
    await add(
        up,
        *(Sample(t, {"v2": 100.0, "v5": 20.0}, DONE) for t in (a, b)),
        last_built=local_ts(10, 10, 10, 15),
    )
    await up.async_reupload(local_dt(10, 9, 12, 0), local_dt(10, 9, 14, 0), "statuses", passive_fields=["v5"])
    # The later period again, now without passive fields.
    await up.async_reupload(local_dt(10, 9, 13, 0), local_dt(10, 9, 14, 0), "statuses")
    await up._async_upload_statuses()
    assert up.client.batches == [
        [
            line("12:00", day="20261009", v2="100", v5="20"),
            line("14:00", day="20261009", v2="100"),
        ]
    ]


async def test_passive_only_site_sends_imported_passive_fields(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v5", active=False)])
    assert up.active_mappings == []
    times = [local_ts(10, 9, 12, m) for m in (0, 5)]
    await add(
        up,
        *(Sample(t, {"v5": 20.0}, DONE) for t in times),
        last_built=local_ts(10, 10, 10, 15),
    )

    async def nothing(*args):
        return []

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=nothing),
        patch.object(uploader_module, "async_build_from_statistics", side_effect=nothing),
    ):
        await up.async_import(
            local_dt(10, 9, 12, 0), local_dt(10, 9, 12, 5),
            overwrite=False, upload=True, passive_fields=["v5"],
        )
    await up._async_upload_statuses()
    assert up.client.batches == [
        [line("12:00", day="20261009", v5="20"), line("12:05", day="20261009", v5="20")]
    ]


async def test_a_finished_newer_choice_is_not_handed_back(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    """A: 10:00-12:00 with v5; B: 11:00-11:30 with v6; B finishes first."""
    up = await make(
        hass, tmp_path, [field("v2"), field("v5", active=False), field("v6", active=False)]
    )
    rows = [local_ts(10, 9, h, m) for h, m in ((10, 0), (11, 0), (11, 15), (12, 0))]
    await add(
        up,
        *(Sample(t, {"v2": 1.0, "v5": 2.0, "v6": 3.0}, DONE) for t in rows),
        last_built=local_ts(10, 10, 10, 15),
    )
    await up.async_reupload(local_dt(10, 9, 10, 0), local_dt(10, 9, 12, 0), "statuses", passive_fields=["v5"])
    await up.async_reupload(local_dt(10, 9, 11, 0), local_dt(10, 9, 11, 30), "statuses", passive_fields=["v6"])

    # A is split around B: the windows no longer overlap.
    windows = await up._passive_windows()
    assert [(w["fields"], w["from"], w["to"]) for w in windows] == [
        (["v5"], rows[0], rows[1] - 1),
        (["v5"], local_ts(10, 9, 11, 30) + 1, rows[3]),
        (["v6"], rows[1], local_ts(10, 9, 11, 30)),
    ]

    # B's statuses are sent; A still has 10:00 waiting.
    await up._run(up.store.mark_range, rows[1], rows[3] + 1, DONE)
    windows = await up._prune_passive_windows()
    assert [w["fields"] for w in windows] == [["v5"]]  # only A's part before B
    # B's period does not fall back to A's v5.
    assert up._passive_for(windows, rows[2], 300) == []
    assert up._passive_for(windows, rows[1], 300) == []
    assert up._passive_for(windows, rows[0], 300) == ["v5"]


async def test_window_edge_is_row_exact(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    window = {"from": local_ts(10, 9, 10, 0), "to": local_ts(10, 9, 11, 0) - 1, "fields": ["v5"]}
    # The 5 minute status at 11:00 has no row in a window ending at 10:59:59.
    assert up._passive_for([window], local_ts(10, 9, 11, 0), 300) == []
    assert up._passive_for([window], local_ts(10, 9, 10, 55), 300) == ["v5"]
    # A 15 minute status ending 11:00 has rows at 10:50 and 10:55 in it.
    assert up._passive_for([window], local_ts(10, 9, 11, 0), 900) == ["v5"]


async def test_clear_queue_clears_passive_choices(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2"), field("v5", active=False)])
    ts = local_ts(10, 9, 12, 0)
    await add(up, Sample(ts, {"v2": 1.0, "v5": 2.0}, DONE), last_built=local_ts(10, 10, 10, 15))
    await up.async_reupload(local_dt(10, 9, 12, 0), local_dt(10, 9, 12, 0), "statuses", passive_fields=["v5"])
    assert await up._passive_windows() != []
    await up.async_clear_queue()
    assert await up._passive_windows() == []


async def test_long_status_sends_passive_fields_of_all_its_windows(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2")])
    a = {"from": local_ts(10, 9, 10, 5), "to": local_ts(10, 9, 10, 10), "fields": ["v5"]}
    b = {"from": local_ts(10, 9, 10, 15), "to": local_ts(10, 9, 10, 15), "fields": ["v6"]}
    end = local_ts(10, 9, 10, 15)
    # A 15 minute status ending 10:15 holds rows of both windows: both fields.
    assert up._passive_for([a, b], end, 900) == ["v5", "v6"]
    # A 5 minute status holds one row: one window.
    assert up._passive_for([a, b], end, 300) == ["v6"]
    assert up._passive_for([a, b], local_ts(10, 9, 10, 10), 300) == ["v5"]


async def test_download_fields_are_not_sent(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass,
        tmp_path,
        [
            field("v2"),
            # Downloading: its sensor is kept (and recorded) for switching back.
            field("v5", direction="download"),
            # Added from PVOutput: no source at all.
            field("v6", entity=None, direction="download"),
        ],
    )
    assert [m.mode for m in up.mappings] == ["send", "download", "download"]
    assert [m.field for m in up.download_mappings] == ["v5", "v6"]
    assert up.passive_mappings == []
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v2": 100.0, "v5": 20.0}),
        Sample(local_ts(10, 10, 10, 10), {"v2": 200.0, "v5": 21.0}),
        last_built=local_ts(10, 10, 10, 10),
    )
    await up._async_upload_statuses()
    assert up.client.batches == [["20261010,10:05,,100", "20261010,10:10,,200"]]


async def test_download_once_per_interval(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2", entity=None, direction="download")])
    up.client.reading = StatusReading("20261010", "10:15", {"v2": 450.0}, 0.1)
    await up._async_download()
    assert up.reading == up.client.reading
    assert up.client.statuses == [False]

    # Not again within the upload interval.
    now.tick(120)
    await up._async_download()
    assert up.client.statuses == [False]
    now.tick(200)
    await up._async_download()
    assert up.client.statuses == [False, False]


async def test_download_extended_for_donors_and_errors(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass, tmp_path, [field("v7", entity=None, direction="download")], donor=True
    )
    up.client.fail = lambda kind, _: PVOutputBadRequest("No status found")
    await up._async_download()
    assert up.reading is None
    assert up.paused_until is None

    now.tick(600)
    up.client.fail = lambda kind, _: PVOutputRateLimited("exceeded", time.time() + 900)
    await up._async_download()
    assert up.paused_until is not None

    # No download fields: nothing is requested, and no stale reading stays.
    up.reading = StatusReading("20261010", "10:00", {}, None)
    up.mappings = []
    await up._async_download()
    assert up.reading is None


async def test_download_goes_stale(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2", entity=None, direction="download")])
    up.client.reading = StatusReading("20261010", "10:15", {"v2": 800.0}, 0.1)
    await up._async_download()
    assert up.fresh_reading == up.client.reading

    # Reads fail: the last value counts for three upload intervals, then not.
    up.client.fail = lambda kind, _: PVOutputRateLimited("exceeded", time.time() + 3600)
    now.tick(600)
    await up._async_download()
    assert up.fresh_reading is not None
    now.tick(301)
    assert up.fresh_reading is None
    # Kept, for the attributes and for when reads work again.
    assert up.reading is not None

    # PVOutput has no status (e.g. a new day): the old reading no longer applies.
    up.client.fail = lambda kind, _: PVOutputBadRequest("No status found")
    up.paused_until = None
    await up._async_download()
    assert up.reading is None
    assert up.reading_at is None


async def test_download_other_rejection_is_an_error(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v2", entity=None, direction="download")])
    up.client.reading = StatusReading("20261010", "10:15", {"v2": 800.0}, 0.1)
    await up._async_download()

    # Not "no status": an error, and the reading stays until it goes stale.
    tries: list[str] = []
    up.client.fail = lambda kind, _: tries.append(kind) or PVOutputBadRequest(
        "Bad request 400: Invalid parameter"
    )
    now.tick(301)
    with pytest.raises(PVOutputBadRequest):
        await up._async_download()
    assert up.fresh_reading is not None
    # Not retried before the next interval.
    now.tick(60)
    await up._async_download()
    assert tries == ["get_status"]


async def test_last_upload_and_download_survive_a_reload(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(
        hass, tmp_path, [field("v2"), field("v5", entity=None, direction="download")]
    )
    await add(
        up,
        Sample(local_ts(10, 10, 10, 5), {"v2": 100.0}),
        last_built=local_ts(10, 10, 10, 5),
    )
    await up._async_upload_statuses()
    up.client.reading = StatusReading("20261010", "10:15", {"v5": 20.0}, None)
    await up._async_download()
    assert up.last_upload is not None
    assert up.last_download is not None

    # E.g. after a restart, or a field's select changed (the site reloads).
    again = Uploader(hass, up.entry, FakeClient(), up.store)  # type: ignore[arg-type]
    await again.async_load()
    assert again.last_upload == up.last_upload
    assert again.last_download == up.last_download


async def test_recording_carries_counters_between_reads(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    up = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)])
    await up._run(up.store.set_meta, META_LAST_BUILT, str(local_ts(10, 10, 10, 0)))
    seen = []

    async def from_states(hass_, sources, start, end, baselines=None):
        seen.append(dict(baselines))
        baselines["v1|sensor.x|"] = 1000.0 + len(seen)
        return []

    with patch.object(uploader_module, "async_build_from_states", side_effect=from_states):
        await up._async_record()
        now.tick(300)
        await up._async_record()
        # Kept across a restart, in the log database.
        again = await make(hass, tmp_path, [field("v1", aggregate=AGG_ENERGY_TODAY)])
        now.tick(300)
        await again._async_record()
    assert seen == [{}, {"v1|sensor.x|": 1001.0}, {"v1|sensor.x|": 1002.0}]


async def test_recording_waits_while_the_recorder_is_behind(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    """A backup's database lock: nothing is read until the recorder has caught up."""
    up = await make(hass, tmp_path, [field("v2")])
    start = local_ts(10, 10, 10, 0)
    await up._run(up.store.set_meta, META_LAST_BUILT, str(start))
    reads = []

    async def from_states(hass_, sources, start_, end, baselines=None):
        reads.append((start_, end))
        return [Sample(end, {"v2": 100.0})]

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=from_states),
        patch.object(uploader_module, "recorder_backlog", return_value=5000),
    ):
        await up._async_record()
    assert reads == []
    assert await up._run(up.store.get_meta, META_LAST_BUILT) == str(start)

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=from_states),
        patch.object(uploader_module, "recorder_backlog", return_value=3),
    ):
        await up._async_record()
    assert reads[0][0] == start
    assert int(await up._run(up.store.get_meta, META_LAST_BUILT)) > start


async def test_read_that_waited_through_a_lock_is_dropped(
    hass: HomeAssistant, tmp_path: Path, now: FrozenDateTimeFactory
) -> None:
    """The read returns as the lock ends, with the recorder still writing its
    queue: those samples would be incomplete, so they are not kept."""
    up = await make(hass, tmp_path, [field("v2")])
    start = local_ts(10, 10, 10, 0)
    await up._run(up.store.set_meta, META_LAST_BUILT, str(start))
    backlog = [0]

    async def from_states(hass_, sources, start_, end, baselines=None):
        backlog[0] = 60000          # the lock held the read; the queue is full
        return [Sample(start_ + 300, {"v2": None})]

    with (
        patch.object(uploader_module, "async_build_from_states", side_effect=from_states),
        patch.object(uploader_module, "recorder_backlog", side_effect=lambda h: backlog[0]),
    ):
        await up._async_record()
    assert await up._run(up.store.get_meta, META_LAST_BUILT) == str(start)
    assert await up._run(up.store.samples_between, start, start + 3600) == []
