# Radio characterization: setup and preflight

Everything to configure and run before and during a characterization flight, in order. Nothing
here has been run end to end on real radios yet: on the bench, `discover` and `check --read-only`
have; the channel switch, `hop` and characterization mode have not.

Placeholders are in angle brackets. Two machines are involved:

- **The vehicle computer** runs the doodle service against the vehicle radio.
- **The GCS laptop** (a Mac is fine) is wired to the GCS radio only, has this repo, and runs
  `tools/radio_bench.py`. It reaches the vehicle radio through the mesh.

Vehicle control must run over its own radio: a failed channel switch then costs this link and
that leg's data, not control.

## 1. Vehicle radio config

On the vehicle computer, in the vehicle radio's config (standalone: `radios/<vehicle>.yaml`; under
rig: the deployment's config file for this instance). Add or change only these blocks:

```yaml
tx_power:
  startup: 19              # fixed, comparable power for every channel; restore your usual value after
  shutdown: unchanged

characterization:
  enabled: true            # turn off again after the campaign: the GCS poll adds traffic on the link
  poll_interval_s: 1
  gcs:
    host: <gcs-radio-ip>   # reached through the mesh, with this radio's `user` password
```

The GCS radio must accept the same `user` password as the vehicle radio. Snapshots appear on
`/<namespace>/radio/characterization`; `/diagnostics` does not change.

Rebuild the image so it contains characterization mode, then restart the service (under rig, use
`rig build` and `rig up` instead):

```bash
docker build -f docker/Dockerfile.runtime -t doodle_radio:latest .
```

```bash
./doodle-up radios/<vehicle>.yaml up -d
```

## 2. GCS laptop, once per terminal

From the repo, with the branch up to date:

```bash
git pull
```

The password, without it landing in shell history (Enter after typing it):

```bash
read -rs DOODLE_BENCH_PASSWORD && export DOODLE_BENCH_PASSWORD
```

The radios and channels. `MODEL` is the radios' sub-model (`discover` prints it under
`radios.a.board.sub_model`), `HOME_MHZ` the current frequency, `WIDTH` the channel width in MHz.
Use only frequencies you are authorized for; under ISM-band rules a 10 MHz channel's centre must
lie between 2405 and 2478 MHz.

```bash
export GCS=<gcs-radio-ip> VEHICLE=<vehicle-radio-ip> MODEL=<sub-model> HOME_MHZ=<home-mhz> WIDTH=10 TEST_MHZ=<test-mhz>
```

A helper that writes the `chswitch` arguments the firmware itself uses (`scan_bands.sh`):

```bash
sw() { printf '{"count": 5, "model": "%s", "frequency": %s, "bandwidth": "%s", "mode_change": 0}' "$MODEL" "$1" "$WIDTH"; }
```

`mode_change` is 0 for a change of frequency at the same width; the firmware sets 1 only when
moving into or out of 40 MHz.

## 3. Bench preflight

Radios on the bench, attenuated so neither hears the other above −30 dBm. Each run writes
`var/bench/<time>-<stage>/report.json`; keep them.

**3.1 Read-only health.** Everything should be PASS (a WARN about signal strength means more
attenuation).

```bash
python3 tools/radio_bench.py --a $GCS --b $VEHICLE check --read-only
```

**3.2 One power step and one channel switch from each end.** It asks first. Pass: two
`one request to … moves both radios` lines with `data after N s`, and
`PASS  both radios back where they started`.

```bash
python3 tools/radio_bench.py --a $GCS --b $VEHICLE check --to "$(sw $TEST_MHZ)" --home "$(sw $HOME_MHZ)"
```

**3.3 Switch timing and persistence.** Pass: `switches_completed` 6/6,
`saved_config_rewritten` false for both radios, `tx_power_changed` empty. `timing.data.max_s` is
how long to discard after every switch in the analysis.

```bash
python3 tools/radio_bench.py --a $GCS --b $VEHICLE hop --to "$(sw $TEST_MHZ)" --home "$(sw $HOME_MHZ)" --repeats 3 --dwell 10
```

**3.4 Per-peer data, read-only.** Pass: `peers_seen_on_both` true and `not_published` empty.

```bash
python3 tools/radio_bench.py --a $GCS --b $VEHICLE capture --seconds 60
```

**3.5 Characterization mode.** With the service running on the vehicle computer (section 1), on
that computer:

```bash
ros2 topic hz /<namespace>/radio/characterization
```

Expect about 1 Hz. Then one snapshot, which should name both `vehicle` and `gcs` and contain no
`unreachable`:

```bash
ros2 topic echo --once /<namespace>/radio/characterization
```

**3.6 Snapshots follow a switch.** Leave the `ros2 topic echo` running without `--once`, run 3.3
again on the GCS laptop, and check `oper_freq` in both radios' `linkstate` moves to the test
frequency and back.

## 4. Ground test on the vehicle

Vehicle on the ground, radios as they will fly, about the flight distance apart if possible. Run
3.1 twice: motors off, then armed (and spun up, tethered, if you can do that safely). Compare the
SNR lines; record a bag of each so the noise floor can be compared from the snapshots.

## 5. Flight

**Pattern, per channel: a hover-and-rotate ladder.** At 3 altitudes (e.g. 30, 60, 90 m), hover and
yaw a full turn at about 12°/s, then climb or descend at about 1 m/s to the next: about 150 s.
Alternate up and down ladders. Order: home channel climbing, 3–4 test channels, home channel
descending (the two home ladders measure the climb-versus-descent difference). Fly 300–500 m from
the GCS with line of sight, and note the GCS antenna's position and height. Hold at the end of
each ladder for the switch.

**Before takeoff**, set the GCS radio to the same fixed power (live only: a power cycle restores
its saved power):

```bash
python3 -c 'import os,sys; sys.path.insert(0,"docker/entrypoints"); from radio_api import RadioAPI; a=RadioAPI(os.environ["GCS"],"user",os.environ["DOODLE_BENCH_PASSWORD"]); a.set_txpower(19); print(a.wireless()["txpower"], "dBm")'
```

Then confirm both radios, on the ground:

```bash
python3 tools/radio_bench.py --a $GCS --b $VEHICLE check --read-only
```

**In flight**, fly the two home-channel ladders without the tool. For each test channel, while
holding before its ladder, run this and type `yes`. Start the ladder when it prints
`switch to <MHz> … both radios there`; after the dwell it switches both radios home by itself and
prints the result.

```bash
python3 tools/radio_bench.py --a $GCS --b $VEHICLE hop --to "$(sw <channel-mhz>)" --home "$(sw $HOME_MHZ)" --repeats 1 --dwell 150
```

**After landing:**

```bash
python3 tools/radio_bench.py --a $GCS --b $VEHICLE check --read-only
```

Power-cycle the GCS radio to restore its saved power, and set the vehicle config back
(`characterization.enabled: false`, your usual `tx_power.startup`).

## If something goes wrong

- **A switch prints `NOT completed`:** that leg's data is lost; the tool still tries to bring both
  radios home. Let it finish before the next command.
- **Exit status 3, `NOT RESTORED`:** a radio was left on the test channel. On one wire the tool
  first sends the GCS radio to fetch the vehicle radio; if that failed, power-cycle the vehicle
  radio after landing. That reloads its saved channel, unless 3.3 showed
  `saved_config_rewritten: true`, in which case set the channel back in its web UI over a wire.
- **`radio unreachable … BadStatusLine`:** fixed in the client (it reconnects once); if it still
  appears, rerun the command and send the output.

## What to send back

The `var/bench/` run folders from the GCS laptop and the bag from the vehicle. The reports have
secret-looking values scrubbed by pattern matching and keep peer MAC addresses, so read them
before passing them on.
