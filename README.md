# doodle

Doodle Labs radio status and config rig service: sets a Mesh Rider radio's transmit power
(with configurable startup and shutdown values) and, in Phase 3, reports radio and link status as
standard ROS 2 diagnostics. Built for the in-house **rig** orchestrator and ROS 2 Lyrical; it does
not depend on rig.

Status: Phases 1–2 of [docs/PLAN.md](docs/PLAN.md) — launcher, image, CI, radio client and power
supervisor. Health reporting (Phase 3) is next.

## How it works

| Piece | Role |
|---|---|
| `doodle-up` | Launcher: `up`/`down`/`status`/`logs`/`config` over one radio config, plus `tx-power` |
| `tools/render_config.py` + `docker/entrypoints/doodle_config.py` | One validator for the launcher and the container |
| `docker/entrypoints/supervisor.py` | The only writer: applies the target, re-applies it after radio reboots and RF toggles, applies the shutdown value |
| `docker/entrypoints/radio_api.py` | The radio's ubus JSON-RPC API over HTTPS; see [docs/RADIO_API.md](docs/RADIO_API.md) |
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
```

`tx_power` values: `standby` (lowest supported), `low`, `medium`, `high`/`max` (highest
supported, capped by `max_dbm`), `off`, whole dBm, or `unchanged` (the default for both startup
and shutdown). Out-of-range requests are rejected; the radio itself clamps silently.

## Tests

```bash
python3 -m unittest discover -s tests -v   # needs PyYAML; Docker Compose for the render test
```

The suites run against a simulated radio (`tests/fake_radio.py`) whose replies follow the
measured API. CI also builds the image, runs the supervisor suites inside it, and runs
`rig certify`.
