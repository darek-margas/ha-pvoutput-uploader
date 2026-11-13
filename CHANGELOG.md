# Changelog

## 0.1.10b1

- **Fix: a bad counter reading could double the day's energy.** A drop of more than 10% in an energy counter was taken as a reset at once, so a counter that read 0 (or another wrong low value) for one poll and then came back had its climb back counted as new energy: the day doubled for a daily counter, and a lifetime counter would have added its whole total. A drop now counts as a reset only if the counter stays down; back near its old value within 15 minutes, it was a bad reading and only the real increase is counted. This also holds when the bad reading falls on a boundary between two reads of the recorder. Reported on the PVOutput forum (Sigenergy Local Modbus).
- **Shared setups:** SolarEdge Modbus Multi (from savoy); the extended Sigenergy setup now uses the inverter's lifetime PV counter for v1, as its author changed it.

## 0.1.9

Stable release of the 0.1.9 betas (b1-b3): an energy fix, and export and import of the configuration.

- **Fix: energy could be lost while recording** ([#3](https://github.com/darek-margas/ha-pvoutput-uploader/issues/3)). The log is filled from the recorder in small reads, and an increment across a read boundary was counted by neither read when the energy counter was unavailable there, or when a state reached the recorder after the previous read. Each counter's last value is now carried from one read to the next, saved together with the read position and kept across restarts. Import history carries it across its day boundaries too.
- **Repairing days already short:** **Configure → Import history from Home Assistant** for those days, with *Replace recorded values* and *Send to PVOutput* on, rebuilds them from the recorder (within its keep days) and sends them again.
- **Export and import configuration** ([#4](https://github.com/darek-margas/ha-pvoutput-uploader/issues/4)): **Configure → Export configuration** shows the site's fields and settings as YAML (also the `pvoutput_uploader.export_config` action), never with the API key or system ID. **Configure → Import configuration** adds the missing fields or replaces all, takes settings only when asked, lets you pick your own sensor for any it names that are not here, and checks everything before saving. Donor-only fields on a standard account are imported only after a confirmation.
- **Shared setups:** ready-made mappings for inverter and battery integrations in `setups/` (by pull request, checked by the tests) and on the wiki's Shared setups page.
- **Connection errors name the error:** a timeout now logs e.g. `addstatus.jsp: TimeoutError` instead of nothing.

**Upgrading from 0.1.8:** nothing to do. Days that came out short before can be repaired as above.

Details per beta below.

## 0.1.9b3

Fixes from the review of 0.1.9b2:

- **Import checks values strictly:** `daily_output_fallback` must be a YAML `true` or `false` (a quoted `"false"` used to count as on), and fixed values must be real numbers (`.nan`, `.inf` and `true` are refused).
- **Merging checks only the fields it adds:** an imported field that is already configured here is not used, so its sensor no longer fails the import (e.g. on a unit).
- **Donor-only fields on a standard account** can be imported after a confirmation screen saying they are configured but not sent or downloaded until the account is a donor.
- **Recording checkpoint in one transaction:** the time read up to and the carried energy counters are saved together, so a crash cannot leave them out of step.

## 0.1.9b2

- **Export and import configuration** ([#4](https://github.com/darek-margas/ha-pvoutput-uploader/issues/4)): **Configure → Export configuration** shows the site's fields and settings as YAML to copy (also the `pvoutput_uploader.export_config` action), never with the API key or system ID. **Configure → Import configuration** takes it in: add the missing fields or replace all, settings only when asked, and sensors this installation names differently picked on one screen. Everything is checked before it is saved.
- **Shared setups:** ready-made mappings for inverter and battery integrations in `setups/` (pull requests, checked by the tests) and on the wiki.

## 0.1.9b1

- **Fix: energy could be lost while recording** ([#3](https://github.com/darek-margas/ha-pvoutput-uploader/issues/3)). The log is filled from the recorder in small reads, usually one 5 minute slot a minute, and each read counted energy from the counter value in force at its start. When the counter was unavailable at a read boundary, or a state reached the recorder after the previous read, the increment across the boundary was counted by neither read, so the log, and PVOutput, fell behind the sensor. Each counter's last value is now carried from one read to the next and kept across restarts, so the increment lands in the next slot. Import history carries it across its day boundaries too.
- **Repairing days already short:** **Configure → Import history from Home Assistant** for those days with *Replace recorded values* and *Send to PVOutput* on rebuilds them from the recorder (within its keep days, 10 by default) and sends them again (statuses within 14 days, 90 for donors).
- **Connection errors name the error:** a timeout used to log `PVOutput upload stopped: addstatus.jsp:` with nothing after it; it now says e.g. `addstatus.jsp: TimeoutError`.

## 0.1.8

Stable release of the 0.1.8 betas (b2, b3; b1 was not released): **PVOutput's history in Home Assistant statistics.**

- **Every column PVOutput has** (v1-v12, normalised output) is kept as hourly statistics, *... (PVOutput history)* (`pvoutput_uploader:<system id>_<field>`), whatever the fields are set to: energy as hourly totals, the rest as hourly mean, min and max. Shown in statistics graphs and the Energy dashboard.
- **Daily history download** switch (Controls, on by default): yesterday is added every day after 02:00, and the two days before it again for statuses that reached PVOutput late; up to 30 missed days are caught up after a stop. Each day fetched is checkpointed, so the hourly limit never causes repeats.
- **Configure → Download history from PVOutput** (From, To): checks first how far back PVOutput has statuses and says so when the start has none, then fetches day by day in the background at the pace the hourly limit allows. Progress and result in the **History download days left** sensor; long downloads rebuild the statistics every 30 days and at the end.
- **Newer data wins:** days PVOutput has statuses for replace what was downloaded before; days without keep it.
- **Hours without statuses** (a gap, or midnight to the first status) share the energy counted over them evenly; the day's total stays exact. Daylight saving days (23 and 25 hours) are handled and tested.
- **Download fields get their past:** a *from PVOutput* sensor's statistics are filled from the history for the hours before its own begin, never rewriting recorded hours, with energy totals lined up.
- **Downloading** switch (while a field is in Download mode) stops the live reads.
- **Removing a site** deletes its PVOutput history statistics.
- **Icons:** matching Last upload / Last download, and icons for the new switches and sensors.
- Normalised output from PVOutput is in kW/kW (was labelled kWh/kW); Home Assistant may ask once about the unit change of its statistics.

**Upgrading from 0.1.7:** nothing to do. The daily history download starts by itself (switch it off under Controls if you do not want it); use **Download history from PVOutput** once to bring in the past.

Details per beta below.

## 0.1.8b3

- **Matching icons:** Last upload and Last download are a cloud with an up and a down arrow; the Downloading and Daily history download switches, History download days left and Normalised output have their own icons too.
- **Daily history download checkpoints each day it fetches**, so a pass stopped by the hourly limit does not ask for the same days again, and the two previous days are fetched again only once, when a new day arrives.
- Tests for daylight saving days (Sydney: the 23 hour day in October, the 25 hour day in April): every hour lies in its day, none twice, and energy adds up to PVOutput's day total.

## 0.1.8b2

**PVOutput history in Home Assistant statistics.** First beta of it: 0.1.8b1 failed its checks and was not released, so this includes everything planned for it.

- Every column PVOutput has (v1-v12, normalised output) is kept as hourly statistics, *... (PVOutput history)* (`pvoutput_uploader:<system id>_<field>`), whatever the fields are set to: energy as hourly totals, the rest as hourly mean, min and max. Shown in statistics graphs and the Energy dashboard.
- **Daily history download** switch (Controls, on by default): yesterday is added every day after 02:00, catching up on up to 30 missed days. The two days before yesterday are fetched again too (three requests a day), so statuses that reached PVOutput late (another uploader catching up, a reupload) replace what was downloaded first.
- **Configure → Download history from PVOutput** (From, To): checks first how far back PVOutput has statuses and says so when the start has none; then fetches day by day in the background at the pace the hourly limit allows. Progress and result in the new **History download days left** sensor. Long downloads rebuild the statistics every 30 days with detail and at the end; a pending rebuild survives restarts.
- Days PVOutput has statuses for replace what was downloaded before; days without keep it.
- **Hours without statuses:** energy counted over hours PVOutput has no statuses for (a gap, or midnight to the first status) is shared evenly by those hours and the next hour with a status. The day's total stays exact; the README says which hours are estimates.
- **Download fields get their past**: a *from PVOutput* sensor's statistics are filled from the history for the hours before its own begin, never rewriting recorded hours, with energy totals lined up (no jump).
- **Downloading** switch (while a field is in Download mode) stops the live reads.
- Removing a site deletes its PVOutput history statistics.
- Normalised output from PVOutput is in kW/kW (was labelled kWh/kW).

## 0.1.7

Stable release of the 0.1.7 betas (b1-b4): fields can be read from PVOutput as well as sent.

- **Field modes: Send, Record only or Download**, per field, from a select on the device page or all at once under Configure → Fields. A field keeps its sensor or fixed value and settings in every mode, so switching back restores them; a downloading field's sensor keeps being recorded (never sent), so switching back leaves no gap.
- **Download**: the latest status is read from PVOutput (getstatus.jsp; v7-v12 for donors) once per upload interval, also while Uploading is off, into *<field> <label> from PVOutput* sensors plus Normalised output. Values PVOutput does not have are unknown; after three intervals without a successful read the sensors turn unavailable; only "No status found" clears the reading, other rejections show in Last error. Battery fields cannot be downloaded.
- **Add field → From PVOutput** for download-only fields; setting one to Send or Record only asks for a sensor or fixed value.
- **Mode changes are checked when saved**, against the field and account as they are then.
- **Last upload** is kept across restarts and reloads and shown only while a field can be uploaded; **Last download** while a field downloads.
- README: a **Field modes** section explaining sources, modes, the selects and switching direction.

**Upgrading from 0.1.6:** the per-field *Send <field>* switches are replaced by the field selects and removed on update; automations that switched them need `select.select_option` with `send` or `record_only`. Configure → Active fields is now Configure → Fields. Field settings are kept as they were.

Details per beta below.

## 0.1.7b4

- **Last upload no longer resets to Unknown** after a restart or reload (e.g. changing a field's select): it is kept in the log database.
- **Last download** diagnostic sensor, while any field downloads: when PVOutput was last read successfully. Also kept across restarts.
- **Last upload is shown only while a field can be uploaded** (Send or Record only with a sensor or fixed value); a download-only site has Last download instead.
- README: one **Field modes** section explaining sources, the three modes, which options the selects offer and why, and switching between upload and download.

## 0.1.7b3

- **Only "No status found" clears the download reading.** Any other PVOutput rejection of getstatus is reported in Last error, keeps the reading until it goes stale, and is retried the next interval (not every cycle).
- **v7-v12 download fields on a standard account** show why they stay unknown: `not_downloaded_because: needs a PVOutput donor account`.

## 0.1.7b2

- **Download sensors no longer show a stale value as current.** After three upload intervals without a successful read (PVOutput or network down, rate limit) they turn unavailable; a new `downloaded_at` attribute shows when the value was read. When PVOutput has no status (e.g. a new day), the old reading is dropped, so yesterday's energy is not shown as today's.
- **Mode changes are checked when saved:** the field selects and Configure → Fields check the choice against the field and account as saved now (e.g. Send without a source, or Download of v7-v12 after the account stopped being a donor) instead of trusting the options shown.
- Documented that a downloading field's Home Assistant sensor keeps being recorded (never sent), so switching back leaves no gap.

## 0.1.7b1

First stage of download: fields can be read from PVOutput as well as sent.

- **Per-field select: Send, Record only or Download** (Configuration on the device page), replacing the *Send <field>* switches, which are removed on update. Configure → Active fields becomes **Fields**, with the same three choices per field on one page.
- **Download** reads the latest status from PVOutput (getstatus.jsp, v7-v12 for donors) once per upload interval, also while Uploading is off, into a sensor per field, *<field> <label> from PVOutput*, plus **Normalised output from PVOutput**. Unknown when PVOutput has no value. Battery fields cannot be downloaded (PVOutput does not return them).
- **Both directions kept per field:** switching to Download keeps the sensor or fixed value and settings (and keeps recording the sensor), so switching back restores them with no gap.
- **Add field → From PVOutput** adds a download-only field without a Home Assistant source. Setting it to Send or Record only under Fields asks for a sensor or fixed value next; Edit field can set one too.

## 0.1.6

Stable release of the 0.1.6 betas (b1-b12). Highlights since 0.1.5:

- **Battery fields go in batches** (b1-b6 after v12): undocumented by PVOutput but tested and confirmed. Weeks of history go out in minutes.
- **Live data first:** current statuses are sent before any history, and history stops early enough to leave each hour's live statuses their requests (worked out from PVOutput's reset time), with 5 requests always kept for other uploaders. Requests are at least 0.3 s apart.
- **Clear queued history** button (Controls): takes the waiting backlog off the queue without deleting anything.
- **Active or passive per field** from a switch on the device page, or all fields on one page under Configure → Active fields.
- **Passive fields on reupload and import:** Also send passive fields sends them for the chosen period only; each operation keeps its own choice per period, and sites with only passive fields send too.
- **Reupload** To takes a time as well as a day.
- **Import fills partial hours** per 5 minute row and field, keeps recorded values, shares an hour's energy over the gaps, and logs when recorded energy exceeds the statistics.
- **Diagnostics:** Cleared samples, Sent without battery; recorder read errors stay in Last error until reading works again.
- Removed a stray `config_flow.py.bak` shipped since 0.1.5.

Details per beta below.

## 0.1.6b12

- **Statuses longer than 5 minutes across two passive-field choices** (e.g. 15 minute statuses where a reupload or import starts or ends inside one) send the passive fields of every choice that holds one of their rows, each aggregated over the whole status like any other field. Before, only one choice applied. 5 minute statuses are unchanged.

## 0.1.6b11

- **Passive-field windows no longer overlap.** A newer reupload or import is cut out of older windows (an older one around it keeps the parts before and after), so when the newer one finishes first its period cannot fall back to the older choice. A status uses a window only when one of its actual 5 minute rows lies in it.
- **Clear queued history** also clears the passive-field choices of the cleared periods.

## 0.1.6b10

- **Passive-field choices no longer overwrite each other.** Each reupload or import keeps its own passive fields for its own period, also while an earlier one is still being sent; where periods overlap, the newest choice wins. Choices are dropped once nothing in their period is waiting.
- **Sites with only passive fields** now send what a reupload or import chose; before, nothing was sent when no field was active.

## 0.1.6b9

- **Import history → Also send passive fields**, like on Reupload history: with Send to PVOutput on, the ticked passive fields (all by default; shown only when the site has passive fields) are sent too, for the imported period only. Statuses PVOutput already has for that period are sent again to add them. The `import_from_recorder` action takes `passive_fields`.
- The import form's description now matches what it does: statistics fill whatever is still missing, and values already in the log are kept.

## 0.1.6b8

- **Import: recorded energy above the statistics.** When the recorded 5 minute rows of an hour already hold more energy than the hourly statistics, the recorded values are kept, the missing slots get 0, and a warning in the log names the hour and both totals, instead of the hour silently not matching. README corrected.
- **Clear queued history** moved from Configuration to Controls, next to the Uploading switch, so the Configuration card holds only the per-field switches.

## 0.1.6b7

- **Import history fills partial hours.** Statistics were skipped for a whole hour when any 5 minute row of it was already in the log, leaving the other slots empty for good. Now missing rows and missing fields of existing rows are filled per slot and per field, recorded values are never overwritten, and an hour's energy is shared over the missing slots after what recorded rows hold, so the hour still matches the statistics.
- **Statuses sent without battery values are flagged**, and counted in the new **Sent without battery** diagnostic sensor; sending them complete later clears the flag.
- **Recorder errors stay visible.** A failure to read recorder history shows in **Last error** until reading works again (an upload no longer clears it). Uploads still stop at the last fully read slot.
- **Active fields** applies only the ticks changed on that page, so a field switched elsewhere meanwhile keeps that change.
- Defined and tested: an energy counter that is available but reported nothing new in a slot counts 0 (the recorder stores only changes); an unavailable one leaves a gap. Comments about one request per battery status updated.

## 0.1.6b6

- **Make fields active or passive in one go.** Each field has a **Send <field>** switch on the PV site's device page (under Configuration), and **Configure → Active fields** lists every field on one page with a tick for active. Both save the field's Active setting; the site reloads to apply it.
- The cleanup of removed fields' sensors now only looks at sensors.

## 0.1.6b5

- **Reupload history → Also send passive fields.** Passive fields are recorded but not sent; a reupload can now send the ones you tick (all by default) for the statuses of the chosen period only, while live statuses keep them off. Donor and battery rules still apply, and the next reupload replaces the choice. Shown only when the site has passive fields; the `reupload` action takes `passive_fields`.

## 0.1.6b4

- **Reupload history → To** takes a time as well as a day, like From: statuses up to that time are sent (default now). The `reupload` action's `end` takes a date and time, or a day as before (up to its end).
- Battery values in batches are confirmed: PVOutput stores them (a few days of history with battery values went out in a moment). README updated.

## 0.1.6b3

- **Battery fields go in batches.** Statuses with b1-b6 are sent in batches like the others (up to 100 per request for donors), as six columns after `v12`, instead of one request per status: a 3-week backlog takes about 61 requests instead of about 6,000. PVOutput documents battery fields only for single statuses; a test batch with them was accepted. If battery values are missing in PVOutput after an upload, please report it. The **Leave out battery fields** tick box is removed from Reupload history as it no longer saves time (`without_battery` on the action still works).
- **Clear queued history** button on the PV site: takes the waiting history off the queue (statuses older than the last two upload intervals, and queued daily totals) and ends a "leave out battery fields" run, while current statuses keep going. Nothing is deleted: cleared statuses stay in the log as unsent, counted in the new **Cleared samples** sensor, and **Reupload history** (also since the first unsent status) sends them again.
- Removed a stray `config_flow.py.bak` that had been shipped since 0.1.5.

## 0.1.6b2

- **The history reserve is worked out, not fixed.** History stops when the requests left would only just cover every current status due before PVOutput's hourly limit resets, plus one, on top of the reserve of 5 for other uploaders. A 5 minute site with 40 minutes to go keeps 14; near the end of the hour, 7. Before the reset time is known, a full hour is assumed (18 at 5 minutes). Replaces the fixed 30 from 0.1.6b1, so a backlog gets almost the whole hour.

## 0.1.6b1

- **Current statuses go first.** The statuses of the last two upload intervals are sent before any history, so live data no longer waits behind a long backlog (it was oldest first).
- **History stops earlier than live data.** Backlog, reuploads and daily totals stop while 30 requests are left in the hour; current statuses can go on down to the reserve of 5. 25 requests an hour cover a 5 minute site sending battery fields, even two.
- The last 5 requests of each hour (was 2) are kept in reserve. PVOutput's hourly limit belongs to the API key, so other uploaders using the same key, such as IoTaWatt, keep room while a backlog is sent.

## 0.1.5

- **Reupload history → Leave out battery fields (much faster)**, shown only when battery fields are sent: everything waiting up to the moment you submit is sent in batches of 100 without battery values (b1-b6) instead of one request per status, so weeks of history go out in minutes rather than a day. Statuses holding only battery values are skipped, new statuses keep their battery values, and reuploading the period again without the tick adds the battery values. The `reupload` action takes `without_battery`.

## 0.1.4

- Adding a field checks again on submit, against the configuration as saved at that moment, that the PVOutput field is still free. A field added meanwhile in another Configure window (or from a stale form) is no longer added twice, and saving a new field no longer drops one another window saved. The field lists already showed only free fields.
- When every field the account can send is configured, **Add field** is not shown, and the menu says why (on a standard account: all of v1-v6, as extended and battery fields need a donor account). If the last free field is taken in another window after the menu opened, adding stops with that message instead of showing an empty list.
- README: each field can be used once; how the free-field lists and the checks work.

## 0.1.3

- **b1 (battery power) is mandatory for battery data**, as PVOutput ignores b2-b6 without it: **Add field** offers b2-b6 once b1 is configured, b1 cannot be removed while other battery fields remain, and without an active b1 the other battery fields are not sent. Their field sensors explain why.
- **b6 (battery state) as a fixed value is picked from a list** of PVOutput's states (0 Invalid, 1 Standby, 2 Thermal management, 3 Enabled, 4 Fault, 5-9 user defined), when adding and when editing. Its field sensor shows the state's name. As a sensor, b6 works like the other fields.
- README: how to work out a b6 state for a battery that reports none (e.g. a Victron SmartShunt), from battery power and its alarms.

## 0.1.2

- Prepare for inclusion in the default HACS catalogue: enable all HACS validation checks without exceptions.
- No change to uploader behaviour compared with 0.1.1.

## 0.1.1

- PVOutput requests are sent at least 0.3 seconds apart, so a backlog (for example after uploading was switched off) goes out at a steady pace instead of in a burst. The hourly request limit still sets how fast a backlog clears.

## 0.1.0 — First stable release

PVOutput Uploader sends your Home Assistant solar, consumption and battery readings to PVOutput, with field mapping and history controls in the Home Assistant UI.

### Set up your PV site

Use **Add PV Site**, enter your PVOutput API key and system ID, then open **Configure** to add sensor fields or fixed values. The integration reads your account's donor status and upload interval. Donor accounts can also use extended and battery fields.

Each field on the PVOutput device page shows the sensor's actual friendly name and the upload unit, such as **Outside °C** or **Battery Power W**. Fixed values show their units too—for example, **5544 Wh** for battery capacity. For sensors without a reported unit, you can specify a source unit in Add field or Edit field; supported units are validated and converted for PVOutput.

### Keep and recover your data

- Readings are recorded into a local history log every five minutes. Uploading can be switched off while recording continues; passive fields are recorded for later use.
- Network failures, PVOutput outages and rate limits leave data queued for automatic upload when sending can resume. The queue and retry timing survive restarts.
- Rejected samples and daily outputs remain visible. If PVOutput identifies a rejected value, the uploader makes one attempt without that value so the other fields can still arrive; it records the result as partly sent and keeps the complete sample available for reupload.
- **Reupload history** can start from the first unsent status or a selected date and time. **Import history from Home Assistant** uses recorded states where available, then hourly long-term statistics for older periods.
- Status history is sent within PVOutput's account limits. Older days can be sent as daily totals. Recovery detail depends on the history still available in the integration and Home Assistant; unavailable sensor readings leave gaps rather than fabricated values.

The device page includes upload, backlog, rejection and rate-limit diagnostics. The default local history retention is 91 days and can be changed in Configure.

### Upgrading from a beta

Update through HACS and restart Home Assistant. Existing configuration and history are retained. This release includes all changes from b6 plus the **Add PV Site** button and setup title.

Unit changes apply to new readings or history you explicitly rebuild; existing logged samples are not automatically rewritten.

## 0.0.1b6 — Clearer field display and support for sensors without units

The PVOutput device page now shows readable source names and the units PVOutput receives, making it easier to check your field mappings at a glance.

### Friendly names and visible units

- Sensor-backed fields use the selected Home Assistant entity's actual friendly name instead of its entity ID. The original ID remains available in the `source_entity` attribute.
- Field states include the upload unit: for example, **Outside °C**, **Battery Power W**, and a fixed battery capacity of **5544 Wh**. Battery state of charge shows **%**.
- The `pvoutput_unit` and `source_unit` attributes distinguish the upload unit from the sensor's unit. Supported source units are converted to the units PVOutput expects.

### Sensors without a unit

- **Add field** and **Edit field** now let you specify a **Source / custom unit** when a sensor does not report one, such as **kW**, **kWh**, or **°F**. The sensor's own unit takes precedence when present.
- Units are checked before saving standard fields. A unitless temperature sensor configured as °F is converted to °C; a unitless power sensor configured as kW is converted to W.
- Explicit source units are also respected when importing history whose statistics metadata has no unit, without converting already-normalized statistics twice.
- Extended fields can use custom units. Fixed values for standard fields are entered directly in PVOutput's expected unit—for example, battery capacity in **Wh**.

### Upgrading

Update through HACS and restart Home Assistant. Existing mappings remain compatible; use **Configure → Edit field** to supply a missing sensor unit if needed.

Unit changes apply to newly recorded or explicitly rebuilt history. Samples already stored in the integration's history log are not automatically rewritten.

## 0.0.1b5

- When PVOutput rejects a status for a value it names (power or energy too high, generation at night), the status is sent once more without that value, so the other fields still arrive. It counts as **Partly sent** (new diagnostic sensor) and stays unsent, so **Reupload history → since the first unsent status** sends it whole once fixed. Other rejections are not retried.
- Field sensor attribute `sent` renamed to `configured_to_send` (and `not_sent_because` to `not_configured_to_send_because`): it means the field is active and allowed by the account type, not that uploading is on or that PVOutput accepted it.

## 0.0.1b4

- One sensor per configured field on the PVOutput system's device: named after the PVOutput field and its label, showing the Home Assistant sensor it reads (or the fixed value), whether it is sent, how values are combined and the last recorded value. The status sensors moved to Diagnostic, so the device's Sensors card lists exactly what is uploaded.
- A daily output PVOutput rejects stays in the log as rejected, shown in the new **Rejected daily outputs** sensor, instead of disappearing from the queue; reuploading the day sends it again. A rejected output no longer counts as an upload.
- The start of the two hour retry window for unconfirmed statuses is kept in the history log, so restarts and option changes do not start it again.
- README: daily totals are only sent for days too old for statuses; PVOutput works out the totals of other days from the statuses.

## 0.0.1b3

- The history log is kept for 91 days by default (was 400): the 90 days donors can send statuses for, plus today. Existing installs keep their setting (Configure → Settings → Keep history for).
- A "last value" field is not sent when the sensor was unavailable at the end of the interval, and energy since midnight is only sent for intervals with a counter reading, so old values are not repeated as current.
- The two hour retry for unconfirmed statuses counts from the first unconfirmed attempt, so reuploaded and imported history gets it too.
- README: where the data lives (the history log, the recorder, long term statistics) and which disruptions need nothing from you. The default limits (history log 91 days, recorder 10 days, statistics forever) and how to change them are described in the README and under the setting.

## 0.0.1b2

- Configure menu reworked: **Add field** asks first whether the value comes from a sensor or is a fixed value; editing a field shows only the inputs of its kind; every action saves when submitted (no separate save step); explanations on every screen.
- **Reupload history** and **Import history from Home Assistant** are in the Configure menu, besides the actions.
- Reupload starts **since the first unsent status** (waiting, rejected or too old) or **from a date and time**, like IoTaWatt's reload; the action takes `since_failure` or a date or datetime `start`.
- Integration icon (shown by Home Assistant 2026.3+), entity and action icons.
- Only statuses PVOutput confirms (`1` in the batch answer) count as sent; unconfirmed ones, and statuses refused with "Missing energy and power values" while another uploader has not created them yet, are retried for two hours.
- Batches of daily outputs go to `addoutput.jsp` (there is no `addbatchoutput.jsp`).
- An unavailable sensor leaves a gap: no last value and no energy for a slot without a counter reading, instead of repeating an old reading or logging zero.
- Tests for the builder, store, API client, uploader, flows, setup and actions; releases now wait for them, hassfest and the HACS checks.

## 0.0.1b1

First beta.

- Field mapping from the UI: add fields one at a time (sensor or constant to a PVOutput field), name them as in PVOutput, mark them active or passive. Donor-only fields (v7-v12, b1-b6) are offered only to donor accounts.
- 5 minute history log in SQLite, kept 400 days by default, built from the recorder.
- Uploads at the PVOutput status interval (or 5/10/15 minutes), oldest first, with batching, and single `addstatus` requests while battery fields are active.
- Rate limit handling: pauses at the hourly limit and resumes after the reset, also after restarts.
- `reupload` action (statuses inside the window, daily outputs before it) and `import_from_recorder` action (recorded states, then hourly statistics).
- Uploading switch and status sensors.
