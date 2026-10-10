<img src="docs/images/icon.svg" alt="" width="96" align="right">

# PVOutput Uploader for Home Assistant

Send any Home Assistant sensor to [PVOutput](https://pvoutput.org) **without ever leaving
a gap**.

The integration keeps its **own history log**: every 5 minutes it stores the values of
every configured sensor, and it keeps them for **91 days** (configurable), however short
Home Assistant's recorder history is. Whatever stops the uploads (PVOutput down, the
internet out, the hourly limit, uploading switched off), the data waits in the log and is
sent as soon as PVOutput accepts it, **without you doing anything**. Like the PVOutput
service of the IoTaWatt firmware, any period in the log can also be sent again.

## Where your data lives

| | What it holds | How long | Used for |
|---|---|---|---|
| **History log** (`pvoutput_uploader_<system id>.db` in the configuration folder) | 5 minute values of every configured sensor, active and passive, temperatures included | **91 days** by default (*Settings → Keep history for*): the 90 days donors can send statuses for, plus today | Everything that is sent and reuploaded |
| Home Assistant **recorder** states | Every state change of the sensors | `purge_keep_days` of the recorder (10 days by default) | Feeding the log every minute, and catching up after the integration was not running |
| Home Assistant **long term statistics** | Hourly mean/min/max, or energy change, of sensors that have a `state_class` | Forever | Only **Import history**, for periods the log never recorded |

The log is part of Home Assistant backups, and it survives removing and re-adding the
integration (it is named after the PVOutput system ID).

So the recorder only has to cover the longest time the integration might not be running,
and long term statistics are only needed for time before the integration was installed.

### How long data is kept, and how to change it

**History log: 91 days by default.** That is the 90 days PVOutput accepts statuses from
donor accounts, plus today; standard accounts can send statuses for 14 days. Older rows
are deleted once a day. Change it under **Configure → Settings → Keep history for**
(30 to 3650 days). Keep it longer if you want to be able to resend older days as daily
totals. The log is small: about 105,000 rows a year (288 a day), a few MB.

**Home Assistant recorder: 10 days by default.** This is Home Assistant's
`purge_keep_days`, not a setting of this integration. It decides how long the integration
can be stopped (disabled, broken, being reinstalled) and still fill the log with full 5
minute detail when it runs again. To cover longer stops, raise it in `configuration.yaml`:

```yaml
recorder:
  purge_keep_days: 20
```

A longer recorder history makes Home Assistant's database bigger, for all entities, not
only these sensors.

**Long term statistics: kept forever**, but only for sensors with a `state_class` (most
energy and power sensors have one; some temperature sensors do not). They are used only by
**Import history**. To make sure a sensor gets them from now on, give it a state class:

```yaml
homeassistant:
  customize:
    sensor.outside_temperature:
      state_class: measurement
```

## No gaps: what happens when uploads are disrupted

**You do nothing** when:

| Disruption | What happens |
|---|---|
| PVOutput down, network error, timeout | Recording continues. The backlog is sent oldest first once PVOutput answers again. |
| Hourly request limit reached | Uploading pauses until PVOutput's reset time, then continues where it stopped. The pause survives restarts. |
| **Uploading** switched off | Recording continues. The backlog is sent when it is switched back on. |
| PVOutput does not confirm a status (a batch answers `0`, or the status lacks v1-v4 because IoTaWatt has not created it yet) | Only statuses PVOutput confirms count as sent; the rest are retried for two hours. |
| PVOutput rejects one value of a status (power or energy too high, generation at night) | The status is sent once more without the value PVOutput named, so the other fields still arrive. It is counted as **Partly sent** and stays unsent for a reupload. Other rejections are not retried. |
| Integration disabled, reloaded or reinstalled while Home Assistant runs, for less time than the recorder keeps states | On start, the log is filled from the recorder for the whole missed period with full 5 minute detail (about 2 days of history per minute), and the backlog is sent. |
| Home Assistant itself off | No values exist for that time anywhere. Energy counters carry on, so **energy since midnight stays correct**: the energy of the outage is counted in the first interval after the restart. |

**Daily totals are not sent routinely**: PVOutput works out each day's totals from the
statuses. They are sent only for days too old for statuses, on request or with the
fallback setting below.

**PVOutput's own limit:** it takes statuses only for the last **14 days** (**90** for
donors). A backlog older than that stays in the log; send it as daily totals with
**Reupload history**, or turn on the *daily output fallback* setting to do that
automatically.

**You use the Configure menu** only when:

| Situation | Action |
|---|---|
| PVOutput rejected a status, or part of it (shown in **Rejected samples** and **Partly sent samples**) | Fix the cause (e.g. the sensor), then **Reupload history → since the first unsent status** |
| PVOutput rejected a daily total (shown in **Rejected daily outputs**) | **Reupload history** for that day, sent as daily totals |
| You want a period sent again (e.g. a field was made active, or PVOutput data was edited) | **Reupload history → from a date and time** |
| The integration was not running for longer than the recorder keeps states | **Import history from Home Assistant** fills the rest from hourly statistics (sensors without a `state_class` have none) |
| You want PVOutput filled for the time before installing | **Import history from Home Assistant** with *Send to PVOutput* on |

The **Samples waiting**, **Rejected samples** and **Daily outputs waiting** sensors show
what is outstanding at any time.

## Quick start

1. Add this repository to HACS as a custom repository (type *Integration*), install
   **PVOutput Uploader** and restart Home Assistant.
2. **Settings → Devices & services → Add integration → PVOutput Uploader**, enter the API
   key and system ID from pvoutput.org (*Settings → API Settings*). The status interval
   and donor status are read from your account.
3. Open **Configure → Add field** and add the sensors to send.
4. Optional: **Configure → Import history from Home Assistant** to fill the log for the
   days before installing, and send them.

If you already send to PVOutput with a `rest_command` automation, switch it off once the
fields are added, so statuses are not sent twice.

## Configure menu

Everything is under **Settings → Devices & services → PVOutput Uploader → Configure**.
Each action saves as soon as it is submitted.

- **Add field**: first choose where the value comes from:
  - **A sensor**: pick the sensor and the PVOutput field it fills (only fields not used
    yet, and donor fields only for donor accounts). Then name it as in PVOutput, choose
    whether it is **active**, and, where a field allows it, how the 5 minute values of an
    interval are combined: average, last value, or energy since midnight.
  - **A fixed value**: the same number in every status, e.g. the battery size for `b3`.
    It is not logged and never sends a status on its own.
- **Edit field** / **Remove fields**.
- **Reupload history**: send the log again, since the first unsent status or from a
  date and time.
- **Import history from Home Assistant**: fill the log from the recorder for a past
  period.
- **Settings**: account type, upload interval, history retention, daily output fallback.

### Fields

| Field | Meaning | Sent as |
|---|---|---|
| v1 / v3 | Energy generation / consumption | Wh since local midnight, from an energy sensor (lifetime or daily counters both work) |
| v2 / v4 | Power generation / consumption | W, average or last value |
| v5 | Temperature | °C |
| v6 | Voltage | V |
| v7-v12 | Extended (donor) | average, last value, or energy since midnight |
| b1-b6 | Battery power, state of charge, size, lifetime charge/discharge, state (donor) | |

Sensor units are converted (kWh to Wh, kW to W, °F to °C, ...). Sensors must be
recorded by the Home Assistant recorder (they are unless you excluded them).

**Active and passive.** All fields are recorded; only active ones are sent. A passive
field builds up history in the log, so you can make it active later and reupload its
past.

**Working next to another uploader.** Fields that are not active, or not configured, are
left empty, so PVOutput keeps what another uploader such as IoTaWatt sent for the same
time. PVOutput needs one of v1-v4 in a status; a status holding only extra fields is
retried for up to two hours, waiting for the other uploader to create it.

## Reupload history

**Configure → Reupload history**, or the `pvoutput_uploader.reupload` action in scripts
and automations. The form shows how many samples are waiting, rejected or too old for
statuses, and when the first unsent one is. Start:

- **Since the first unsent status**: from the oldest status that is waiting, was rejected
  or became too old (`since_failure: true` in the action).
- **From a date and time** you choose (`start:` a date or a date and time).

…up to an end day (default today), sent as:

- *Statuses where allowed, daily totals for older days* (default): days inside PVOutput's
  status window (14 days, 90 for donors) as statuses, older days as daily outputs
- *Statuses only*: only the days still inside the status window
- *Daily totals only*: daily outputs (generation, consumption, peak power, min/max
  temperature); these replace the day's totals in PVOutput

Sending runs in the background at the pace the hourly request limit allows (60 requests
an hour, 300 for donors). The **Samples waiting** and **Daily outputs waiting** sensors
show the progress.

## Import history from Home Assistant

**Configure → Import history from Home Assistant**, or the
`pvoutput_uploader.import_from_recorder` action. Fills the log for a past period from the
recorder: 5 minute detail where recorded states still exist (10 days by default), hourly
long term statistics before that, split evenly into 5 minute rows. Periods already in
the log are kept unless *Replace recorded values* is on.

With *Send to PVOutput* on, the imported period is sent like any backlog; with it off it
is only stored, ready for a later reupload.

## How it works

- **History log.** Every minute the integration reads the closed 5 minute slots of all
  configured sensors from the recorder and stores them in
  `pvoutput_uploader_<system id>.db` in the configuration folder (included in Home
  Assistant backups). The log is always 5 minute resolution, whatever the upload
  interval, and is kept for 91 days by default.
- **Uploads.** Statuses are built from the log at the upload interval (the system's
  status interval in PVOutput, or set by hand) and sent oldest first, up to 30 per
  request (100 for donors). Only statuses PVOutput confirms in its answer count as sent.
  A rejected batch is split until the status PVOutput objects to is found, so one bad
  value does not hold up the rest.
- **Unavailable sensors** leave a gap in the log rather than a value: no average, no
  last value, and no energy for a slot with no counter reading. Energy is counted when
  the counter reports again.
- **Rate limit.** The `X-Rate-Limit-*` headers of every answer are tracked, and a few
  requests are kept in hand.
- **Donor features** are detected from the account (or set by hand): extended fields
  v7-v12, battery fields b1-b6, statuses 90 days back and 100 per request.
- **Battery fields** are not accepted by PVOutput's batch service, so while any battery
  field is active each status is sent with its own request.

## Entities

The PVOutput system's device page shows what is uploaded and as what:

- **One sensor per configured field**, named after the PVOutput field and your label (e.g.
  *v5 Ambient*). Its state is the source sensor's friendly name, or the fixed
  value. The source entity ID is available in the `source_entity` attribute. Attributes: the PVOutput field and its standard name, active or passive,
  `configured_to_send` (active and allowed by the account type; donor-only fields on a
  standard account are not), how the 5 minute values are combined, and the last recorded
  value with its time. The icon follows `configured_to_send`. It does not show whether
  uploading is switched on or what PVOutput accepted: see the **Uploading** switch and the
  diagnostic sensors for that.
- **Uploading** switch: stops sending while recording carries on.
- Diagnostic sensors: last upload, samples waiting, daily outputs waiting, rejected
  samples, partly sent samples, rejected daily outputs, history size and start, requests
  left this hour, rate limit pause, account type, last error.

## Settings

- **Account type**: detect from PVOutput, donor or standard.
- **Upload interval**: from PVOutput, 5, 10 or 15 minutes.
- **Keep history for**: days the log is kept (default 91: the 90 days donors can send statuses for, plus today).
- **Daily output fallback**: when statuses become too old to send, send that day's
  totals as a daily output instead. Off by default, because it replaces the day's totals
  in PVOutput.

Debug logging shows every PVOutput request and answer:

```yaml
logger:
  logs:
    custom_components.pvoutput_uploader: debug
```

## Development

Tests run in a test Home Assistant:

```bash
pip install pytest-homeassistant-custom-component
python -m pytest tests
```

The recorder's libraries (SQLAlchemy and others, listed in Home Assistant's
`components/recorder/manifest.json`) must be installed too; CI does this.

Releasing: bump `version` in `manifest.json`, add a `## <version>` section to
`CHANGELOG.md` and push to `main`. When the tests, hassfest and the HACS checks pass,
the release workflow tags `v<version>` and publishes a GitHub release (a pre-release
for versions such as `0.0.1b2`).

The icon's source is [`docs/images/icon.svg`](docs/images/icon.svg); the PNGs in
`custom_components/pvoutput_uploader/brand/` (256 and 512 px) are rendered from it.
Home Assistant 2026.3+ shows them in place of the brands repository.

## License

[GNU General Public License v3.0](LICENSE).

### Field units

Field rows show the source friendly name and the unit sent to PVOutput, e.g. `Outside °C`. Fixed values show their unit, e.g. `5544 Wh` for battery size. Power uses W, energy Wh, voltage V and battery state of charge %; extended fields use your chosen unit.

Sensors with a unit are converted automatically. For a sensor without one, enter its actual **Source / custom unit** when adding or editing the field (e.g. kW or °F); this is used for conversion, not just a label. Leave it empty when the source reports its unit. Fixed values are entered in PVOutput units; a custom unit applies only to extended fields. Changing a unit affects newly recorded or explicitly rebuilt history; existing samples keep their recorded values.
