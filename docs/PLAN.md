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
radio is polled every `health.poll_interval_s` (5 s; 1 s for characterization flights) on a fixed
schedule, on a separate API session with 2 s request timeouts, and each poll that brings a new
observation is published at once, so no observation is skipped between heartbeats. Each group
(`radio`, `links`, `system`) is stamped when its own reads returned, so `health.sample_age_s`
dates the reading, not the end of the pass. Status names are `<name>: <component>`; the hardware
ID is the radio's sub-model and hostname.

| Component | Source | Level |
|---|---|---|
| `service` | supervisor state: target vs. effective power, RF state, range, change in progress, last error | ERROR on an error, WARN while not at target, STALE before the first observation |
| `radio` | `iwinfo info`, `iwinfo survey`, `linkstate`, `uci wireless`, ath9k debugfs: power, channel, frequency, bandwidth, noise/peak, channel activity, transmit/receive airtime and busy time, LNA, automatic power control, ACK/broadcast power, country and HT mode, and the configured `distance`, saved power and antennas | WARN when ACK/broadcast power exceeds the level, or when a supported measurement is temporarily unavailable |
| `links` | `linkstate` peer counts, `iwinfo assoclist` station count, the link report's age | WARN while the link report or station table cannot be read (use `limits`, e.g. `links.stations: {warn_below: 1}`) |
| `link/<mac>`, `mesh/<mac>` | one `sta_stats` / `mesh_stats` entry, every field as `link.*` / `mesh.*`; the station's `iwinfo assoclist` entry as `link.iw_*`; the per-station automatic power control files; the vendor-neutral keys (below) | OK; a departed peer stays listed with `link.present: false` (use `limits`, e.g. `link.signal_dbm: {warn_below: -80}`) |
| `system` | `system info` / `system board`: uptime, load, memory, firmware, model | WARN when a value is missing |
| `temperature` | none — the radio has no sensor | OK, explicitly `unsupported` |

Every measurement that could not be read is null with `health.metric.<key>.state`
(`unavailable` or `unsupported`), never zero. An outage keeps the last values marked
`retrying`, then STALE after `stale_after_s`. `health.limits` applies thresholds to any numeric
key. The per-peer entry format of the link report has not been seen on hardware, so per-peer
components copy the radio's fields as they are; nested values are JSON text, or are left out
and flagged `<key>_truncated` when longer than 2 KB. An entry is identified by a known address
field, else by its only MAC-shaped value; entries with several are counted in
`links.unidentified`. The key list is in SERVICE_HEALTH.md (Revision 3); the keys added for
characterization flights (below) are not in it yet.

**Link report freshness.** The radio rewrites `/tmp/linkstate_current.json` every 2 s. A report
that has not changed since the previous poll is not published as a new observation (its sample
ID stays, its age grows); one unchanged for more than 6 s means the radio's linkstate service has
stopped, and its contents are treated as unavailable. `links.report_age_s` is the host-measured
time since the report last changed.

**Station table.** `iwinfo assoclist` is the kernel's live station table (standard OpenWrt rpcd;
not yet confirmed on this firmware, and reported `unsupported` where the method is missing).
Each station's fields become `link.iw_*`, with the rx/tx rate tables flattened
(`link.iw_tx_rate` in kbit/s, `link.iw_tx_mcs`, `link.iw_tx_retries`, `link.iw_tx_failed`,
`link.iw_rx_packets`, `link.iw_rx_bytes`, `link.iw_signal_avg`, `link.iw_inactive`,
`link.iw_mesh_plink`, …): the cumulative counters give throughput and retry rate between polls.
The vendor-neutral keys come from it first: `link.signal_dbm` (average signal, else last frame),
`link.snr_db` (a SNR the link report states itself wins; else signal − the station's noise
reading or the radio's noise floor) and `link.rate_mbps` (transmit PHY rate). Otherwise from the
link report: `link.signal_dbm` (`rssi`), `link.snr_db`, `mesh.quality_pct` (`tq`),
`link.loss_pct` (`pl_ratio`).

**Reverse link.** For up to `health.reverse_link_peers` (4) present stations (named peers first),
the per-station ath9k_tpcd files: `fixed_txpower` as `link.apc_limited` and
`link.apc_limit_dbm` (the per-peer power limit automatic power control set; 255 = none), and
`peer_signal_reply` (how the peer hears us, and the power used) as raw text in
`link.peer_signal_reply` until its format is observed. Two requests per peer per poll; a file
missing for every peer in three passes in a row is `unsupported` until the radio reboots.

**Settings and names.** `rf.country`, `rf.htmode`, `rf.distance_m` (ACK timeout distance),
`rf.saved_tx_power_dbm` (what a power cycle returns to), `rf.antenna_gain_dbi`,
`rf.tx_antennas` / `rf.rx_antennas` when the radio has them; `rf.busy_pct` from the survey
counters over the airtime window. The airtime window starts at the second poll after a start
(never the counters since boot) and spans at least 1 s. `health.peers` names peers by MAC:
`link.name` / `mesh.name` and the component's message.

**Logs.** The supervisor's log records (requests, power changes, re-applies after a radio
reboot, errors, shutdown) also go to `/rosout` from `/<ns>/radio`, so a bag that records
`/rosout` keeps them; records from before ROS started are sent once it has, marked as such.

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
vendor-neutral link keys, and their units); whether `iwinfo assoclist` answers on this firmware
and what it lists for mesh peers; the format of the per-station `peer_signal_reply` and
`fixed_txpower` files read through the API; automatic power control against a fixed level at
close range; reported vs. radiated power; whether a live power change interrupts the link; the
radio's CPU load (`system.load_1m`) with `poll_interval_s: 1`.
