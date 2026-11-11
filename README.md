<p align="center">
  <img src="https://raw.githubusercontent.com/GraphicHealer/dish-aligner/master/custom_components/dish_aligner/brand/logo.png" alt="Dish Aligner logo" width="384">
</p>
<h1 align="center">Dish Aligner</h1>
<p align="center">
  Custom Home Assistant integration + bundled Lovelace card for aiming a
  motorized directional Wi-Fi dish, auto-aligning on peak signal, and surveying
  visible access points by azimuth.
</p>
<p align="center">
  <a href="AI-DECLARATION.md"><img src="https://img.shields.io/badge/AI--DECLARATION-pair-blue" alt="AI-DECLARATION: pair"></a>
</p>
<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=GraphicHealer&repository=dish-aligner&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open in HACS"></a>
  <a href="https://github.com/GraphicHealer/dish-aligner/actions/workflows/validate.yml"><img src="https://github.com/GraphicHealer/dish-aligner/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
</p>

## Hardware

- **Housing / rotator** — Winegard Dish Playmaker satellite housing with the
  factory internals removed. The azimuth axis is driven by an ESP32
  (LOLIN S2 Mini) + A4988 stepper driver running ESPHome. The dome is sealed,
  so homing is done by creeping past the zero notch until the internal
  separator stalls the motor, then re-zeroing the step count.
- **Radio** — Ubiquiti NanoBeam AC Gen2 mounted on the rotator, running
  OpenWrt 25.12.0 in client/station mode (`wwan` interface). Talked to over
  Ubus (HTTP) as `root` — no SSH required.

## Components

### `esphome/dish-azimuth.yaml`

ESPHome config for the rotator ESP32 (genericized — `# CHANGE` markers flag
the per-install values: pins, static IP, and the azimuth `offset`, which is
calibrated per unit after the first homing). Exposes to Home Assistant:

| Entity | Purpose |
|---|---|
| `sensor.dish_azimuth_current_angle` | Live azimuth (0-359, tracks stepper in real time) |
| `number.dish_azimuth_azimuth_target` | Absolute aim target — set it and the dish moves there |
| `binary_sensor.dish_azimuth_azimuth_moving` | On while the stepper is still travelling |
| `button.dish_azimuth_home_azimuth` | Sensorless re-home: stalls against the separator and re-zeroes |
| `button.dish_azimuth_*` (+/-1/5/15/45/90 deg) | Legacy relative jogs |

Mechanical notes: 4 steps per degree, positions normalized to 0-359 so a
command can never drive through the separator. Position is an open-loop step
count (no encoder), so re-home periodically to clear drift.

### `openwrt/homeassistant.json`

rpcd ACL to install at `/usr/share/rpcd/acl.d/homeassistant.json` on the
NanoBeam, granting `iwinfo` (signal, scan) plus the file/exec paths the
[FaserF/ha-openwrt](https://github.com/FaserF/ha-openwrt) integration needs.
Works for the `root` Ubus login — root's wildcard grants take the union of
all acl.d groups. After copying:

```sh
service rpcd reload
echo /usr/share/rpcd/acl.d/homeassistant.json >> /etc/sysupgrade.conf
```

### `custom_components/dish_aligner`

The integration itself (in progress). Setup is a two-step config flow:

1. **Dependency gate** — aborts early unless the FaserF/ha-openwrt integration
   and an ESPHome device are both loaded, then asks for the two devices only
   (rotator + radio). All entities are resolved automatically by matching the
   firmware/radio entity names — nothing to wire by hand.
2. **Verification** — auto-enables the disabled OpenWrt sensors it needs
   (signal quality / bitrate / noise), then sanity-checks that the signal
   sensor returns a real dBm value, which is the tell that `iwinfo` and the
   rpcd ACL are in place on the radio.

Options flow: wireless interface name, auto-align sweep/step/dwell, survey
step size, re-home-before-survey toggle, and an optional speed-test button
to run after aligning.

Services:

- `dish_aligner.aim` — rotate to an absolute azimuth
- `dish_aligner.auto_align` — sweep a configurable arc (center-out to
  minimise slew), median-sample signal at each stop, park on the dBm peak,
  optionally press a speed-test button to verify
- `dish_aligner.survey` — rotate through the full range running `iwinfo scan`
  at each stop; results deduplicated per BSSID keeping the strongest angle.
  Optionally re-homes first (open-loop position drifts)
- `dish_aligner.join` — aim at an AP's best angle, then rewrite the radio's
  STA iface (`ssid`/`encryption`/`key` via UCI) and bounce `wwan` — yes, this
  makes OpenWrt connect to that AP as a client
- `dish_aligner.home` — stall-home against the separator, re-zero the count
- `dish_aligner.abort` — cancel whatever is running

Sensors: status (+progress attributes), survey (AP list), last alignment.

### Bundled card

`type: custom:dish-aligner-card` auto-loads with the integration (served from
`custom_components/dish_aligner/frontend/`, injected via `add_extra_js_url`).

```yaml
type: custom:dish-aligner-card
angle_entity: sensor.dish_azimuth_current_angle
target_entity: number.dish_azimuth_azimuth_target
status_entity: sensor.dish_aligner_status
survey_entity: sensor.dish_aligner_survey
signal_entity: sensor.<openwrt_ssid>_signal   # your radio's signal sensor
beamwidth: 50
```

The dashed needle is draggable — release and it calls `dish_aligner.aim`.
The wedge is the dish's current pointing. Survey results render as a table
(sorted best-dBm first, deduped per BSSID); **Join** prompts for the password
and calls `dish_aligner.join`.

## How the pieces talk

```
Lovelace card ──> dish_aligner services ──> ESPHome entities (move/wait/home)
                     │
                     └──> OpenWrt ubus: iwinfo signal/scan at each stop
```

The integration waits on `binary_sensor.dish_azimuth_azimuth_moving` between
moves so signal readings are only taken when the dish is settled.
