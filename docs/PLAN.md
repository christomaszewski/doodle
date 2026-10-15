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
| `off` | Allowed in YAML and the launcher verb; refused from ROS (a remote command could silence the link it arrived on) |
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
  Phase 3 adds the ROS parameter `tx_power` on `/<ns>/radio`.
- **Concurrency:** one supervisor owns all writes; requests are serialized; a request blocked
  behind another for 20 s fails with a retry message. Health reads use their own API session.

## Health (Phase 3)

`diagnostic_msgs/DiagnosticArray` on `/diagnostics` following `SERVICE_HEALTH.md` (copied from
ouster), heartbeat 1 s, HTTP polling 5 s:

| Component | Content |
|---|---|
| `service` | target vs. effective power, RF state, last error |
| `radio` | effective power, RF on/off, channel, frequency, bandwidth, noise/peak, channel activity, automatic power control, LNA, ACK/broadcast override (WARN above target) |
| `links` | peer counts |
| `link/<mac>` | per-peer signal both ways, rates, packet loss, per-peer power limit (format pending a second radio) |
| `system` | uptime, radio CPU/memory, firmware, model |
| `temperature` | `unsupported` |

New metric keys (`rf.*`, `link.*`, `links.*`) need a `SERVICE_HEALTH.md` revision in the
dashboard, camera-service and ouster repos.

## Phases

1. **Rig skeleton** — `rigging.yaml`, `doodle-up`, `tools/render_config.py`, compose, runtime
   image, build script, example config, CI (certify, build contract, shellcheck, tests).
2. **Radio client and power supervisor** — `radio_api.py`, `power.py`, `supervisor.py`,
   `radioctl.sh`, healthcheck; tests against a simulated radio built from recorded replies.
3. **Health and ROS** — `health.py`, `health_ros.py`, the `tx_power` parameter, in-image tests
   with rclpy, the spec revision.
4. **Release** — README, registry-release workflow, hardware acceptance (including the tests
   that need a second radio).

## Waiting on a second radio

Per-peer data formats; automatic power control against a fixed level at close range; reported
vs. radiated power; whether a live power change interrupts the link.
