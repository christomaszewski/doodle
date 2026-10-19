# doodle

Doodle Labs radio status and config rig service: sets a Mesh Rider radio's transmit power
(with configurable startup and shutdown values) and reports radio and link status as standard
ROS 2 diagnostics. Built for the in-house **rig** orchestrator and ROS 2 Lyrical; it does not
depend on rig.

Status: Phases 1–3 of [docs/PLAN.md](docs/PLAN.md) — launcher, image, CI, radio client, power
supervisor, diagnostics and the ROS `tx_power` parameter. Tested against a simulated radio;
hardware checks are listed in the plan.

## How it works

| Piece | Role |
|---|---|
| `doodle-up` | Launcher: `up`/`down`/`status`/`logs`/`config` over one radio config, plus `tx-power` |
| `tools/render_config.py` + `docker/entrypoints/doodle_config.py` | One validator for the launcher and the container |
| `docker/entrypoints/supervisor.py` | The only writer: applies the target, re-applies it after radio reboots and RF toggles, applies the shutdown value |
| `docker/entrypoints/radio_api.py` | The radio's ubus JSON-RPC API over HTTPS; see [docs/RADIO_API.md](docs/RADIO_API.md) |
| `docker/entrypoints/health.py` | Read-only radio, link and system observations as diagnostics ([docs/SERVICE_HEALTH.md](docs/SERVICE_HEALTH.md)) |
| `docker/entrypoints/health_ros.py` | The ROS node `/<namespace>/radio`: `/diagnostics` and the `tx_power` parameter |
| `docker/compose/compose.deploy.yaml` | One container per radio; the password arrives as a compose secret the launcher writes |
| `rigging.yaml` | Tells rig how to drive `doodle-up` |

Every change is live: nothing is written to the radio's flash, so a radio power cycle always
restores its saved configuration.

## Quick start

Prepare the radio first ([docs/RADIO_SETUP.md](docs/RADIO_SETUP.md)): firmware 2025-06 or later,
and a strong password for its `user` login.

```bash
docker build -f docker/Dockerfile.runtime -t doodle_radio:latest .

mkdir -p radios                                   # gitignored
cp sensors/doodle.example.yaml radios/mesh.yaml   # set host, password, tx_power
# Standalone: put the `user` password itself in radios/mesh.yaml.
# Under rig: keep password: "{{doodle_radio_password}}" and run once per vehicle
#   sudo rig provision --var doodle_radio_password='<password>'

./doodle-up radios/mesh.yaml up -d        # waits until the startup power is applied
./doodle-up radios/mesh.yaml tx-power     # {"target": ..., "tx_power_dbm": ..., "converged": ...}
./doodle-up radios/mesh.yaml tx-power low
./doodle-up radios/mesh.yaml down         # applies tx_power.shutdown; nonzero if unconfirmed

ros2 topic echo /diagnostics                     # service, radio, links, link/<mac>, system, …
ros2 param set /mesh/radio tx_power medium      # same values as tx-power, except off
```

`tx_power` values: `standby` (lowest supported), `low`, `medium`, `high`/`max` (highest
supported, capped by `max_dbm`), `off`, whole dBm, or `unchanged` (the default for both startup
and shutdown). Out-of-range requests are rejected; the radio itself clamps silently.

## Tests

```bash
python3 -m unittest discover -s tests -v   # needs PyYAML; Docker Compose for the render test
```

The suites run against a simulated radio (`tests/fake_radio.py`) whose replies follow the
measured API. CI also builds the image, runs the supervisor and ROS suites inside it (real
`/diagnostics` and parameter calls), and runs `rig certify`.

## Portable builds and replay startup

Detached startup allows 10 seconds for first radio reachability, then 120 seconds for power
reconciliation. Set `DOODLE_CONNECT_TIMEOUT` or `DOODLE_SETTLE_TIMEOUT` in the launcher's environment
to change those budgets. Status polling is bounded and reads the supervisor's cached `ready` response;
it does not issue extra radio requests. A timeout returns failure and leaves background retries running.

Rig replay omits this live radio service by default and keeps its recorded bag topics. Use
`rig replay RUN --include-service doodle` (or `--live INSTANCE`) for deliberate live-radio replay.
This needs the updated descriptor/launcher and a rebuilt image containing `radioctl.sh ready`.

The build contract declares one runtime-independent image, `doodle_radio`, on Linux arm64 and
amd64. `jp6m` and `dev` use that same image; tags have no runtime suffix. In a deployment with
Doodle enabled, Rig v0.2.62 or newer selects it as follows:

| Build scope | Doodle image selection |
| --- | --- |
| `rig build` (default `all`) | Both CPUs, including disabled deployment rows |
| `rig build --scope deployment` | Deployment CPU from `vehicle.yaml` or `--arch` |
| `rig build --scope host` | Host Docker CPU; runtime comes from machine provisioning/defaults |
| `rig build --scope deployment,host` | Union of those CPUs, published once at the same tag |
| `rig build --scope replay` | Omitted by default because `replay.start` is false |

For example, with `platform: jp6m` and `arch: arm64` in `vehicle.yaml`, provision the development
machine once with `sudo rig provision --platform dev`, then run
`rig build --scope deployment,host --dry-run` to inspect the selection. An amd64 host adds the
amd64 member; an arm64 host needs only the arm64 member. Plain `rig build` still builds both.
Use a fresh `images.tag` for targeted iteration: Rig refuses to replace a broader existing index
with fewer CPU members. `rig bake --bundle-images` includes only the vehicle's selected CPU.

To include Doodle in a replay build, set this policy in the deployment's `vehicle.yaml`:

```yaml
replay:
  services:
    doodle: {start: true}
```

This makes the replay scope build both CPUs and also opts replay into contacting the live radio.
Normally leave the policy off and replay its recorded diagnostics. A later
`replay --include-service doodle` selects startup only; it does not build missing image members.

Standalone `tools/build_image.sh REGISTRY TAG` retains host-native builds.
`DOODLE_BUILD_PLATFORM=linux/amd64` selects a standalone CPU; Rig's `RIG_BUILD_PLATFORMS` takes
precedence. `PUSH=0` keeps a standalone build local (Buildx loading requires a single CPU).
Rig builds publish with Buildx and append image ref/digest JSONL to `RIG_BUILD_RESULT`.
`RIG_BUILD_NO_CACHE=1` adds `--no-cache --pull`; `RIG_BUILD_PULL=0` suppresses the parent pull.

Run `rig certify --repo . --require-build-check` to verify build forwarding without building
images (Bubblewrap on Linux, sandbox-exec on macOS). CI tests image execution on native arm64 and
amd64 runners, and checks the actual Rig scope planner against this descriptor. Run those scope
checks locally with `RIG_SOURCE=/path/to/rig python3 -m unittest discover -s tests -p 'test_build*.py' -v`.
