# Shared setups

Ready-made field mappings for inverter, battery and energy-monitor integrations,
to take in with **Configure → Import configuration**. None of them contains an API key
or system ID: an export never does.

## Using one

1. Open the file for your integration and copy its contents.
2. **Settings → Devices & services → PVOutput Uploader → Configure → Import
   configuration**, paste it, and choose *Add the fields not configured yet* or
   *Replace all fields*. Leave *Also take its settings* off unless you want the
   setup's upload interval and history settings too.
3. Sensors with other names on your installation are listed on the next screen:
   pick yours for each. A field left empty is skipped.

Check the result on the device page: each field sensor shows its source and unit.

## Adding one

The easy way: set up the fields on your site, **Configure → Export configuration**, and
paste the YAML into a **[Share a setup](https://github.com/darek-margas/ha-pvoutput-uploader/issues/new?template=shared_setup.yml)**
issue. The form asks for the integration, its version and your hardware; the setup is
then added here. No fork or pull request needed.

Or, with a pull request:

1. Export the configuration as above.
2. Save it here as `<integration>-<variant>.yaml`, e.g. `sigenergy-modbus.yaml`,
   `huawei-solar.yaml`, `sungrow-modbus.yaml`.
3. Put a comment block at the top: which integration (with a link) and its version,
   the hardware, and anything to know (sensors to enable, units, donor-only fields).
4. Leave `settings:` out unless they matter for the setup.
5. Open a pull request.

Prefer the integration's default entity IDs, so most sensors are found without
picking.

## Format

See [`example.yaml`](example.yaml). Per field:

| Key | Values |
|---|---|
| `field` | `v1`-`v12`, `b1`-`b6` |
| `sensor` or `constant` | an entity ID, or a fixed number; neither for a `download` field |
| `name` | your label for the field |
| `mode` | `send` (default), `record_only`, `download` |
| `value_per_interval` | `average`, `last`, `energy_today` (the field's default if left out) |
| `unit` | only for sensors without a unit of their own |

Extended (`v7`-`v12`) and battery (`b1`-`b6`) fields need a PVOutput donor account;
battery fields need `b1`.
