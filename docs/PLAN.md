# Implementation plan

A rig service that sets a Doodle Labs Mesh Rider radio's transmit power (with configurable
startup and shutdown values) and reports radio and link status as standard ROS 2 diagnostics.
It follows the structure of the `ouster` rig service: a launcher over one config, a supervisor
that is the only writer, a Unix-socket control client, and a diagnostics publisher.

Radio facts this plan depends on are in [RADIO_API.md](RADIO_API.md); radio preparation is in
[RADIO_SETUP.md](RADIO_SETUP.md).

## Decisions

| Topic | Decision |
|---|---|
| Firmware | 2025-06 and later; tested on 2026-03.1 |
| API | JSON-RPC over HTTPS (`/ubus`) as `user` (never root). The password is a config value — under rig a vehicle-local var, `"{{doodle_radio_password}}"`, provisioned into `/etc/rig/vehicle.local.yaml` — that the launcher hands to the container as a compose secret, never in the rendered compose or a `rig bake` artifact |
| Writes | Live only (`iw … set txpower fixed`, `network.wireless down/up`). No UCI commits, nothing written to flash |
| Operational-state verbs | Not in v1 (`standby`/`activate`/`state` are not declared in `rigging.yaml`) |
| Default startup/shutdown | `unchanged` when omitted — the service only observes |
| `off` | Allowed in YAML and the launcher verb; refused over ROS (a remote command could silence the link it arrived on) |
| Levels | `standby` = lowest supported, `high`/`max` = highest supported (capped by `max_dbm`), `low`/`medium` = thirds between, rounded down to a supported value, overridable |
| Automatic power control | Left as configured. Levels are exact except at very close range; diagnostics show the per-peer and ACK/broadcast values |
| Validation | Requests outside the radio's supported range are rejected, not clamped (the radio clamps silently) |

On the tested radio (19–32 dBm): `standby` 19, `low` 23, `medium` 27, `high`/`max` 32.

## Configuration (`sensors/doodle.example.yaml`)

```yaml
service: doodle
name: mesh
ros: {namespace: mesh}
connection:
  type: ubus
  ubus:
    host: 10.223.18.52
    scheme: https
    username: user
    password: "{{doodle_radio_password}}"   # rig vehicle-local var
    tls_fingerprint_sha256: ""
    device: wlan0
    uci_radio: radio0
tx_power:
  startup: unchanged       # standby|low|medium|high|max|off|<dBm>|unchanged
  shutdown: unchanged
  levels: {}               # optional {low: <dBm>, medium: <dBm>}
  max_dbm: null            # optional operator cap
health: {interval_s: 1, poll_interval_s: 5, stale_after_s: 15, limits: {}}
zenoh: {}
```

The launcher validates everything before creating or replacing a container; the runtime
validates again with the same module (`docker/entrypoints/doodle_config.py`).

## Behavior

- **Target precedence:** a runtime request saved in the container (`/var/lib/doodle/target.json`,
  survives Docker restarts) > `tx_power.startup` > `unchanged`. Recreating the container returns
  to the configured startup value.
- **Reconcile every 5 s:** resolve the target against the radio's current range, compare with the
  effective power and RF state, and correct. This re-applies the target after a radio reboot,
  after RF comes back on, and after any wifi restart. `unchanged` never writes.
- **Shutdown** (`rig down`, Docker stop, Ctrl-C): apply `tx_power.shutdown` unless `unchanged`,
  read it back, and exit non-zero if unconfirmed so `doodle-up down` reports it. The saved runtime
  request is not overwritten.
- **Runtime control:** `doodle-up <cfg> tx-power` prints the status JSON;
  `doodle-up <cfg> tx-power <value>` sets a new target (`startup` returns to the configured value).
  Over ROS: `ros2 param set /<ns>/radio tx_power <value>` — same values except `off`. The reply
  comes after the change is read back (or fails with the reason); a radio that is unreachable
  accepts the target and the reconcile loop applies it later. The parameter shows the current
  target, including changes made with `doodle-up … tx-power`.
- **Concurrency:** one supervisor owns all writes; requests are serialized; a request blocked
  behind another for 20 s fails with a retry message. Health reads use their own API session.
- **Shutdown order:** stop accepting requests, apply the shutdown power, then tear down ROS, so a
  slow ROS shutdown can never cost the radio its shutdown setting.

## Health

`diagnostic_msgs/DiagnosticArray` on `/diagnostics` following [SERVICE_HEALTH.md](SERVICE_HEALTH.md),
from the node `/<ns>/radio`. Heartbeat every `health.interval_s` (1 s) from in-memory state; the
radio is polled every `health.poll_interval_s` (5 s) on a separate API session with 2 s request
timeouts. Status names are `<name>: <component>`; the hardware ID is the radio's sub-model and
hostname.

| Component | Source | Level |
|---|---|---|
| `service` | supervisor state: target vs. effective power, RF state, range, change in progress, last error | ERROR on an error, WARN while not at target, STALE before the first observation |
| `radio` | `iwinfo info`, `iwinfo survey`, `linkstate`, `uci wireless`, ath9k debugfs: power, channel, frequency, bandwidth, noise/peak, channel activity, transmit/receive airtime, LNA, automatic power control, ACK/broadcast power | WARN when ACK/broadcast power exceeds the level, or when a supported measurement is temporarily unavailable |
| `links` | `linkstate` peer counts | OK (use `limits`, e.g. `links.stations: {warn_below: 1}`) |
| `link/<mac>`, `mesh/<mac>` | one `sta_stats` / `mesh_stats` entry, every field as `link.*` / `mesh.*`, plus the vendor-neutral `link.signal_dbm` (`rssi`), `link.snr_db` (reported, or signal − noise floor), `mesh.quality_pct` (`tq`), `link.loss_pct` (`pl_ratio`) | OK; a departed peer stays listed with `link.present: false` (use `limits`, e.g. `link.signal_dbm: {warn_below: -80}`) |
| `system` | `system info` / `system board`: uptime, load, memory, firmware, model | WARN when a value is missing |
| `temperature` | none — the radio has no sensor | OK, explicitly `unsupported` |

Every measurement that could not be read is null with `health.metric.<key>.state`
(`unavailable` or `unsupported`), never zero. An outage keeps the last values marked
`retrying`, then STALE after `stale_after_s`. `health.limits` applies thresholds to any numeric
key. The per-peer entry format has not been seen on hardware, so per-peer components copy the
radio's fields as they are; thresholds on them can be added once a second radio shows the field
names. The key list is in SERVICE_HEALTH.md (Revision 2, to be copied to the dashboard,
camera-service and ouster repos).

## Phases

1. **Rig skeleton** — `rigging.yaml`, `doodle-up`, `tools/render_config.py`, compose, runtime
   image, build script, example config, CI (certify, build contract, shellcheck, tests).
2. **Radio client and power supervisor** — `radio_api.py`, `power.py`, `supervisor.py`,
   `radioctl.sh`, healthcheck; tests against a simulated radio built from recorded replies.
3. **Health and ROS** — `health.py`, `health_ros.py`, the `tx_power` parameter, in-image tests
   with rclpy, the spec revision. Done; awaiting hardware checks (below).
4. **Release** — README, registry-release workflow, hardware acceptance (including the tests
   that need a second radio).

## Hardware checks for Phase 3

On the radio, with the service up:

1. `ros2 topic echo /diagnostics` shows six components; `radio` reads the same power as
   `doodle-up … tx-power`.
2. `rf.ack_power_dbm` is a number (debugfs readable through the API) or `unsupported`; either
   way `radio` is OK with no peers.
3. `rf.noise_dbm` and `links.*` are present (the link report is readable through the API).
4. `ros2 param set /<ns>/radio tx_power low` changes the power; `off` is refused.
5. Unplug the radio's Ethernet: components go `retrying`, then STALE; they recover on reconnect.

## Waiting on a second radio

Per-peer data formats (including whether `rssi`, `pl_ratio` and `tq` are the field names behind the
vendor-neutral link keys, and their units); automatic power control against a fixed level at close range; reported
vs. radiated power; whether a live power change interrupts the link.
