# Sensor and service health specification

Revision 3, 2026-10-26 (link quality keys and the radio indicator). This contract lets service authors report measurements, active
conditions, and operational state to the vehicle dashboard. ROS 2 services publish standard
diagnostics; other services publish native Zenoh JSON. Both inputs use the same semantics.
The canonical copy lives in dashboard/docs/SERVICE_HEALTH.md; identical copies accompany
camera-service, ouster and doodle. Update the copies together when changing the contract.
Revision 2 added the radio keys below (`rf.*`, `links.*`, `link.*`, `mesh.*`, `system.*`), written
in doodle. Revision 3 adds vendor-neutral link quality keys any radio can publish, and the
dashboard's radio indicator. Consumers that predate them show these keys as ordinary values.

## Identity and components

A service is identified by vehicle ID and deployment instance, independent of its ROS
namespace. Instance and vehicle IDs are nonempty single Zenoh path segments. A component
has a stable name `<instance>: <component>`, a human message, a hardware ID (empty for
software), a diagnostic level, and flat scalar values. Keep vendor error codes and text.
Use components for independently observable concerns: service, stream, recording, camera,
temperature, power, sensor alerts, or alert/<vendor code>.

Operational states such as active, standby, transitioning, disabled, and down are values,
not diagnostic levels. Confirmed standby is healthy. Only demand streaming when active.
Health collection is observational: it must not configure hardware or initiate recovery.
Bound device requests and keep them off lifecycle/control and dashboard rendering threads.

## Levels and observation quality

Keep ROS DiagnosticStatus levels: 0 OK, 1 WARN, 2 ERROR, 3 STALE. Preserve the raw level.
For operator summaries, ERROR takes precedence over WARN and missing observations; show
missing information alongside the fault. An empty or invalid report is unknown, never OK.

Reserved `health.*` values carry the following metadata. ROS represents them as strings;
native JSON uses numbers/booleans where appropriate. Consumers must also accept ordinary
ROS diagnostics without this metadata using topic/name identity and a configured timeout (15 seconds by default).

| Value | Meaning |
|---|---|
| `health.vehicle_id`, `health.instance`, `health.service` | Fleet identity; required on fleet ROS diagnostic statuses |
| `health.publisher_id` | Random process-start identity; changes on publisher restart |
| `health.sequence` | Increasing publication number within that publisher |
| `health.publish_interval_s` | Nominal heartbeat period; consumers allow at least three intervals of silence |
| `health.sample_id` | Changes only when the component gets a new observation |
| `health.sample_age_s` | Age of the observation at publication, from the producer's monotonic clock |
| `health.stale_after_s` | Positive validity interval for this component |
| `health.availability` | current, retrying, stale, unavailable, unsupported, or paused |
| `health.last_level` | Last observed level when current level is STALE |
| `health.metric.<metric key>.state` | Optional per-metric current/unavailable/unsupported state |
| `health.error` | Failure to obtain the current observation |

A heartbeat does not make cached measurements new. Consumers add elapsed local time to
sample age, use local receipt time for silence, and do not compare unsynchronized wall
clocks for expiry. Use sample IDs to avoid plotting repeated cached values as new samples.
Wall timestamps are for display and recording; Ouster FPGA time is not assumed to be Unix.
On failed reads retain explicitly marked last-known observations until recovery. A missing
metric must never turn into zero. A valid zero is a measurement. Unsupported capabilities
are informational; temporarily unavailable supported measurements need attention.

## Metrics and thresholds

| Key | Unit | Meaning |
|---|---|---|
| `temp.<where>_c` | degrees Celsius | Device temperature at the named location |
| `supply.voltage_v` | V | Input voltage |
| `supply.current_a` | A | Input current |
| `supply.power_w` | W | Input power; derived voltage × current from the same poll unless explicitly measured |
| `fps.delivered`, `fps.expected` | Hz | Observed and expected frame rate |
| `frame_age_s` | s | Age of the most recent data frame |
| `disk.free_gb`, `disk.free_pct` | GB, percent | Recording filesystem capacity |
| `uptime_s`, `ptp.state`, `ptp.offset_ns` | s, string, ns | Device uptime and clock synchronization |

Radio keys (Revision 2, Doodle Labs radios). Power values are what the radio reports at its antenna
port, not a measurement.

| Key | Unit | Meaning |
|---|---|---|
| `rf.state` | on/off | Transmitter state |
| `rf.tx_power_dbm` | dBm | Effective transmit power level |
| `rf.target`, `rf.target_dbm` | string, dBm | Requested level and what it resolves to now |
| `rf.converged` | bool | The radio is at the target |
| `rf.changing` | string | Change in progress, empty otherwise |
| `rf.startup`, `rf.shutdown` | string | Configured startup and shutdown levels |
| `rf.tx_power_min_dbm`, `rf.tx_power_max_dbm` | dBm | Supported range on the current channel |
| `rf.channel`, `rf.frequency_mhz`, `rf.bandwidth_mhz` | -, MHz, MHz | Operating channel |
| `rf.noise_dbm`, `rf.noise_peak_dbm` | dBm | Noise floor (average, peak) |
| `rf.channel_activity_pct` | percent | Channel busy time |
| `rf.lna_status` | string | Vendor LNA state, raw |
| `rf.auto_power_control`, `rf.auto_power_control_aggressive` | bool | Vendor automatic power control |
| `rf.ack_power_dbm`, `rf.broadcast_power_dbm` | dBm | Power of ACK and broadcast frames |
| `rf.ack_power_override`, `rf.broadcast_power_override` | bool | Those differ from the level |
| `links.stations`, `links.mesh_nodes` | count | Peers in the radio's link report |
| `links.unidentified` | count | Peer entries without a recognizable address |
| `link.<field>`, `mesh.<field>` | vendor | One peer's fields as the radio reports them |
| `link.present`, `link.last_seen_s` | bool, s | Whether the peer is in the latest report |
| `system.load_1m`, `system.load_5m`, `system.load_15m` | - | Radio CPU load average |
| `system.memory_total_mb`, `system.memory_free_mb` | MB (10^6 bytes) | Radio memory |
| `system.firmware`, `system.model`, `system.parent_model`, `system.sub_model`, `system.hostname` | string | Radio identity |

Link quality keys (Revision 3, any radio). Vendor-neutral, so the dashboard can show link
strength without knowing the radio. Publish them on each peer's component (`link/<peer>`, or
`mesh/<peer>` with the `mesh.` prefix), next to that peer's vendor fields. A radio that tracks
one link only (or knows which peer carries the operator) may instead put the `links.*` summary on
its `links` component. Convert vendor scales at the producer: an RSSI that is not in dBm is a
vendor field, not `signal_dbm`.

| Key | Unit | Meaning |
|---|---|---|
| `link.signal_dbm` | dBm | Received signal strength from this peer |
| `link.snr_db` | dB | Signal-to-noise ratio of that signal |
| `link.quality_pct` | percent | Vendor link quality on a 0–100 scale (BATMAN TQ × 100 / 255, for example) |
| `link.loss_pct` | percent | Packet loss to this peer |
| `link.rate_mbps` | Mbit/s | Current PHY rate to this peer |
| `links.signal_dbm`, `links.snr_db`, `links.quality_pct` | dBm, dB, percent | The one link a radio reports, or the operator's |
| `links.peer` | string | Which peer that summary describes |

Interference is the radio's noise floor (`rf.noise_dbm`) and channel busy time
(`rf.channel_activity_pct`). Until a producer adopts these keys the dashboard also reads
`<kind>.rssi`, `<kind>.signal` and `<kind>.rssi_dbm` as dBm when they are between −130 and 0, and
`mesh.tq` as quality.

Accept only finite numeric measurements, excluding booleans. Convert source units at the
producer. Ouster mV and mA become V and A; their product divided by 1,000,000 is watts.
Validate telemetry fields independently: missing temperature must not suppress power.
GenICam feature overrides must use SI units (V/A/W). An explicitly configured device power feature takes precedence over derivation. Only derive power when both voltage and current are finite; one absent feature does not imply zero current. Power is an estimate at the sensor input,
not upstream power-supply consumption or a measurement of brief transients.

Thresholds belong in producer configuration and travel as level/message. Do not invent
universal temperature/power limits. Device-origin warnings/errors retain their severity.
Dashboard presentation thresholds do not change the recorded producer verdict.

## Native Zenoh transport

```
fleet/<vehicle>/svc/<instance>/health        liveliness token and latest-snapshot queryable
fleet/<vehicle>/svc/<instance>/health/state  JSON publications
```

Use application/json and strict UTF-8 JSON, with no NaN or Infinity. The existing envelope
remains schema_version 1; metadata additions are backward compatible. Each publication
contains the complete current component set for that instance.

```json
{
  "schema_version": 1,
  "service": "camera-service",
  "instance": "cam_front",
  "stamp_unix_ns": 1791450000000000000,
  "level": 0,
  "status": [{
    "level": 0,
    "name": "cam_front: camera",
    "message": "OK",
    "hardware_id": "camera serial",
    "values": {
      "supply.voltage_v": 12,
      "supply.current_a": 0.5,
      "supply.power_w": 6,
      "health.sample_id": "example-1",
      "health.sample_age_s": 0,
      "health.stale_after_s": 5,
      "health.availability": "current"
    }
  }]
}
```

Token presence advertises a reporter, not healthy hardware. Query replies do not reset
sample age. Duplicate/older sequence numbers must not replace newer observations.

## ROS 2 transport

Publish diagnostic_msgs/msg/DiagnosticArray on `/diagnostics` (or a configured equivalent).
Use a system-clock Header stamp and standard DiagnosticStatus/KeyValue fields. A one-second
publication interval with reliable volatile QoS is recommended. Temperature can additionally
use sensor_msgs/msg/Temperature for typed ROS recording/plotting, publishing only new valid
samples. A custom ROS interface package is not required.

The dashboard discovers DiagnosticArray topics through its existing rmw_zenoh graph and
CDR decoder. ROS traffic must actually reach that router; DDS-only deployments require a
supported bridge or rmw_zenoh configuration. Never construct ROS data keys from a guessed
namespace/hash. Decode every received array through the raw stream pool, merge each status
individually before rendering. Shared `/diagnostics` arrays are not fleet snapshots.
One publisher's data must not erase or refresh another component's observation.

Recognized numeric metadata/metrics are parsed as finite numbers from ROS strings; unknown keys
remain strings. Unknown components and alert codes remain inspectable. Fleet identity
metadata wins; each fleet instance has one authoritative reporter; generic diagnostics remain under their domain/topic/name identity. Never
guess the vehicle when several vehicles or domains are visible. Prefer native health for
an instance when an optional ROS relay duplicates it.

## Alerts and history

Collect Ouster `/api/v1/sensor/alerts` alongside `/api/v1/sensor/telemetry`, outside the lidar
driver so reporting survives standby. Use the complete active list for current truth and
the cursor-bearing log for recent trigger/clear events. WARNING maps to WARN, ERROR to
ERROR, NOTICE to informational OK. Unknown severity must remain visible as WARN.

Publish one summary component and a stable diagnostic per known code, with `alert.code`,
`alert.category`, `alert.active`, `alert.severity`, and `alert.detail`. Explicitly publish
cleared codes as OK, retaining known codes for the reporter lifetime. A failed/malformed
poll cannot clear a condition. Preserve raw sensor event time separately from host time.
The summary carries `alerts.log` (a JSON string with the latest 32 vendor events), `alerts.epoch` (sensor boot identity plus a local cursor-reset generation), and `alerts.history_gap`. This is a fleet convention inside ordinary KeyValue messages, not a new ROS interface. Detect cursor resets/gaps and report incomplete history; never fabricate missing events.
Do not use a high-rate packet telemetry topic as a substitute for the sensor alert API.

The dashboard keeps a bounded session history of observed condition changes, not one event
per heartbeat. History survives tab changes, not a browser reload. Durable vehicle-wide
event storage is a separate extension; the sensor's short log is not durable storage.

## Dashboard and expected services

The Home health widget and dedicated Health tab use one store. Home presents a compact
persistent overview: one verdict per reporter, named temperatures and power in watts when
available, and severity-labeled issue pills. Hover, keyboard focus, or tap reveals the full
message, vendor code/category/detail, source, hardware ID and sample age. Escape or tapping
outside dismisses the detail. Active NOTICE alerts use informational pills; cleared alerts
are omitted. An alert-history gap remains visible as an informational pill even after
all active alerts clear, without turning healthy hardware into a fault. A redundant active-alert summary is hidden only when detailed alerts account
for it and no additional failure, stale observation or incomplete history needs attention.

Metric details include voltage/current and a recent trend with its range. Narrow cards keep
trends in the detail popover; wider cards also show inline sparklines. The Health tab adds
expandable component inspection with all reported electrical values, attention-first sorting,
service/hardware/fault search, an attention filter, and a searchable session event journal.
Temperature presentation thresholds never color watts or change the producer verdict.
Unavailable metrics use a dash, valid zero stays zero, unsupported metrics are omitted from
the compact view. Offline, silent, and disconnected readings are labeled last known and dimmed;
a last observed ERROR retains its severity. Reporter silence invalidates the live presentation
even when a component has a longer observation validity interval.
Keep offline services visible for the browser session. Merge Rig inventory so services
never observed can still appear as not reporting; disabled/down entries show their intent.
Disconnection from the router is a dashboard connection issue, not proof that every sensor
failed. Expected reporting can also be declared by widget service references.

Omitting widget `services` follows discovery and automatically adds future native or ROS
reporters. `services: [vehicle/instance, instance]` supplies an ordered initial selection,
including placeholders for never-seen reporters; qualified references avoid cross-vehicle
ambiguity. The widget's service picker can switch between all reporters and a manual set.
Manual choices persist locally across tab switches/reloads, without filtering the shared
store or the Health tab. New reporters are not auto-added to a manual set. `lock: true`
hides the picker and enforces YAML. `details: true` initially expands component inspection.
Browser choices are scoped by widget label and configured service list; use distinct labels
for independent widgets. Changing the configured list resets that preference scope. Generic
ROS reporters selected in the picker use their stable domain/topic/name key. Retained
selections appear as not reported until rediscovered after a reload.

## Producer adoption and acceptance

1. Select ROS diagnostics or native JSON and establish fleet identity.
2. Implement bounded observation providers, units, availability, sample age and stable names.
3. Publish periodic snapshots including errors, clear transitions and unsupported metrics.
4. Test missing/invalid readings, thresholds, poll failures, recovery and process restart.
5. Validate a real serialized report through the dashboard and recording path.

Acceptance covers mixed ERROR/STALE, empty reports, clock skew, late queries, multiple ROS
publishers in a single rendering interval, explicit clears, unsupported temperature with
valid power, standby, dropped HTTP replies, alert cursor resets, and missing inventory rows.
Hardware acceptance also checks actual camera feature units, Ouster firmware payloads,
ROS distro serialization and QoS, and recovery across disconnects and restarts.

## Implementation decisions and validation

Implemented on `feat/health` in camera-service, ouster, and dashboard; on `feat/radio-service`
in doodle.

- Camera retains schema v1 and existing keys. Each provider adds boot, publication, sample,
  age and validity metadata. Query replies age the cached snapshot on the producer's
  monotonic clock. Failed polls retain measurements and hardware identity, then mark them
  STALE with their last severity. Partial temperature/electrical read failures become null
  measurements with WARN; unsupported camera features remain omitted. GenICam derives
  watts when V and A are available. Boson publishes temperature, not invented power data.
- Ouster runs a `sensor_health` ROS node in the supervisor. Its executor publishes
  diagnostics every 1 s while a separate worker polls telemetry and alerts every 5 s.
  HTTP requests have 2 s timeouts. Components expire after 15 s by default. Polling pauses
  during mode transitions; confirmed standby still supports HTTP health collection.
  The existing `/<namespace>/temperature` topic remains compatible, with only new valid
  samples. Stream health watches actual `ouster_sensor_msgs/Telemetry` arrivals when TLM
  is enabled, allowing 5 s after activation before declaring absent data an ERROR. This
  detects missing telemetry flow; it is not a packet-loss or point-cloud quality monitor.
- Ouster active alerts are reconciled only after validating a complete active list.
  Known codes are explicitly cleared, with vendor text preserved. The recent log allows
  the browser to display faults that triggered and cleared between polls. A gap/reset
  flag remains true for that reporter lifetime after history continuity is lost.
- The dashboard subscribes to all discovered DiagnosticArray topics except
  `*/diagnostics_agg` by default. Select explicit topics/domains to consume an aggregator
  instead. No ROS publishers are required to adopt fleet metadata to be inspectable.
  Unannotated statuses use domain/topic/name identity; identical names on the same topic
  cannot be attributed reliably to distinct publishers. Give those publishers unique
  names or fleet metadata. Missing fleet vehicle identity appears under the ROS domain.
- Raw arrays are never coalesced before merging; component expiry is independent.
  Native reports take precedence while their reporter is alive and recently publishing.
  Browser freshness uses a monotonic receipt clock; wall time is only for labels/trends.
  Duplicate/older sequences, delayed native queries, and retired publisher boots cannot
  refresh or replace newer observations. A new reporter boot starts a new component
  inventory; old faults remain in the event journal. Producer boot IDs must be unique
  per restart.
- Offline entries remain for the browser session. Native token loss is explicit offline;
  ROS publisher/component disappearance is detected as silence/STALE by timeout. Rig
  inventory supplies never-observed rows but does not claim they implement health.
  The Health tab is opt-in, consistent with other dashboard tabs. History is capped at
  200 events and metric trends at 10 minutes / 1,200 samples per metric. Reloading clears
  browser history; this implementation does not add a durable event database.

Validation uses camera unit/adapter tests, simulated Ouster firmware over real HTTP,
real ROS publishers/subscribers in the local Lyrical runtime, and dashboard tests plus a
production build. The dashboard fixture in `app/src/health/ros-fixtures.json` was generated
by Ouster's `tests/generate_health_fixture.py` using rclpy serialization, not a hand-written
CDR encoder. This caught a Lyrical integration issue: DiagnosticStatus's ROS `byte` level
is represented by Python bytes. Use its generated OK/WARN/ERROR/STALE constants; an int
can pass assignment in optimized Python and then abort in generated C serialization.

Checks completed for this revision:

- Camera: 35 health tests, 13 Boson protocol tests, and 26 native Zenoh adapter tests passed.
- Ouster: 69 tests passed in `ouster_driver:health-review`, including installed supervisor
  entrypoints, real ROS delivery and Zenoh typesupport. The launcher suite then passed
  20 tests, including two additional health-config cases (71 distinct tests altogether).
- Dashboard: 431 tests passed and the production build succeeded. Browser inspection
  verified compact summaries, issue details, selection, component disclosure, and narrow
  layout. Widget coverage includes hover/focus/tap/Escape, retained error severity, missing
  versus zero measurements, native/ROS discovery, persistent selection/locking, alert
  summary deduplication, independent component freshness, and connection loss.
  Existing build warnings concern third-party eval and bundle size.

Other lessons: a fresh envelope timestamp does not refresh a cached measurement; a late
query must include updated sample age; an observed arrival gap must never lengthen the
silence timeout; and numeric max(level) can hide ERROR behind STALE. The UI keeps fault
severity and observation quality visible together.

Still required on the target vehicle: confirm actual GenICam feature units/capabilities,
firmware-specific alert/telemetry payloads, supported Ouster hardware revisions, and the
chosen RMW/router connectivity across the real network. Tests use simulated hardware;
no vehicle configuration, firmware, deployed container, or physical operating mode was
changed for this implementation. The local runtime test image reuses the existing compiled
Ouster overlay with the new supervisor files; the full upstream driver was not rebuilt.
- Doodle publishes from the radio supervisor's own ROS node (`/<namespace>/radio`): `service`
  from the supervisor's in-memory state every 1 s; `radio`, `links`, `link/<mac>`, `mesh/<mac>`,
  `system` and `temperature` from a separate 5 s poll on its own radio API session with 2 s
  request timeouts. A failed call blanks only the values it provides (null plus a metric
  state); an outage marks every radio component `retrying`, then STALE. Departed peers stay
  listed for the reporter lifetime as OK with `link.present: false` (a lost peer is not
  automatically a fault; configure `links.*` limits to make it one). Temperature is
  explicitly unsupported (the radio has no sensor). Each present peer also carries the
  Revision 3 link quality keys, mapped from its vendor fields: `link.signal_dbm` from `rssi`,
  `link.snr_db` as reported or derived from the same report's noise floor, `mesh.quality_pct`
  from BATMAN `tq`, and `link.loss_pct` from `pl_ratio` (a percentage; the radio alerts above
  60). Values outside a key's range are not mapped. The per-peer format is not yet observed on
  hardware; these are the fields the radio's linkstate configuration enables. `health.limits`
  can set thresholds on them, for example `link.signal_dbm: {warn_below: -80}` makes a weak
  peer WARN.
- The dashboard header shows a small radio indicator for each radio reporter (one with `rf.*` or
  `links.*` values, or `link/` / `mesh/` components): signal bars and, when the channel is noisy or
  busy, an interference mark. The bars describe the configured peer, else the strongest peer in the
  latest report. They come from SNR (reported, or signal minus the noise floor), else signal strength,
  else link quality; a configured peer that is not heard is zero bars. Hover, focus or tap shows the
  peer, signal, SNR, quality, loss, rate, noise floor, channel busy time, TX power and peer counts.
  The radio's health summary adds the same Signal and Interference readings. Both dim to last known
  when the report is not current; the indicator travels over the link it describes, so a dropped
  link shows as silence, not as zero bars. Thresholds are presentation only and configurable.

## Configuration and rollout

Camera health is already published through its existing native Zenoh session when enabled.
Keep using `health.limits` and `health.providers.genicam.features` in the camera configuration
(see that repository's HEALTH.md for the exact provider layout). Configure only thresholds
justified for the installed hardware; none are added for temperature or power by default.

Ouster sensor YAML accepts this optional block (it is kept out of upstream ROS parameters):

```yaml
health:
  interval_s: 1
  poll_interval_s: 5
  stale_after_s: 15
  limits: {}                    # metric -> warn_above/below, error_above/below
```

The launcher passes it as `OUSTER_HEALTH_CONFIG`; the runtime validates it again. Export
`VEHICLE_ID` to match the vehicle's other service identities. `name` is the deployment
instance; `ros.namespace` may differ. Enable `rmw_zenoh_cpp` or an appropriate DDS bridge
so ROS diagnostics reach the dashboard router. The wrapper's default RMW is unchanged.
The dashboard does not poll sensors directly.

Dashboard instance YAML:

```yaml
tabs: [home, health, cameras, ros, rig]
health:
  ros_topics: [/diagnostics]     # optional exact allowlist; omit for discovery by type
  ros_domains: [0]              # optional domain allowlist
  ros_stale_after_s: 15         # fallback for ordinary ROS diagnostics
  radio:                         # optional: the header radio indicator
    services: [vehicle/mesh]     # radio reporters to show; omit for every one
    peer: "aa:bb:cc:dd:ee:ff"    # the peer carrying the operator link; omit for the strongest
    # header: false              # hide the indicator (the health summary still shows the link)
    # snr_db: [5, 12, 20, 30]    # SNR (dB) for 1–4 bars; also signal_dbm [-90, -80, -70, -60]
    #                            #   and quality_pct [20, 40, 60, 80] when SNR is not known
    # noise_dbm: [-88, -80]      # interference moderate / high above these noise floors
    # busy_pct: [50, 80]         # ... or above these channel busy percentages
home:
  version: 1
  widgets:
    - type: health
      label: Sensor health
      services: [vehicle/cam_front, vehicle/top]  # omit to discover all
      # lock: true               # optional: enforce YAML and hide the picker
      # details: true            # optional: start with full inspection expanded
      history_s: 300             # 0 disables trends; up to 600 seconds
```

Roll out producers and dashboard independently: additions to native schema v1 are optional
metadata, while ROS diagnostics remain standard messages. Old publishers remain readable,
with consumer timeouts and without producer sample identity. Record `/diagnostics` alongside
normal ROS data when durable forensic history is wanted; native camera recording summaries
continue to include its health observations.

## References

- [ROS DiagnosticArray](https://raw.githubusercontent.com/ros2/common_interfaces/lyrical/diagnostic_msgs/msg/DiagnosticArray.msg)
- [ROS DiagnosticStatus](https://raw.githubusercontent.com/ros2/common_interfaces/lyrical/diagnostic_msgs/msg/DiagnosticStatus.msg)
- [Ouster firmware 2.4 manual](https://data.ouster.io/downloads/software-user-manual/firmware-user-manual-v2.4.0.pdf), telemetry and alerts sections
