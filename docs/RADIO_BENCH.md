# Two-radio bench

`tools/radio_bench.py` answers what the service and the characterization flight need to know from
two real radios. It has only been run against simulated radios whose `chswitch` behavior is
invented, so expect the first real run to teach us something. On a flying vehicle, use it only
when vehicle control runs over a different link: a failed switch costs this link and its data,
not control.

| Question | Stage | Changes the radios | Read in `report.json` |
|---|---|---|---|
| Is everything working? | `check` | Only after the read-only checks pass and you agree | `checks` (also printed as it runs) |
| How does the firmware call `chswitch`? | `discover` | No | `findings.callers` (`found in …`, `none in N files`, or what kept the search from finishing), then `radios.a.search.hits`, `.strings` and `["config/…"]`; `findings.exec` says whether each radio will run `iw` and ping |
| What do peer entries hold, and does the service's mapping survive them? | `capture` | No | `peers`, `findings.not_published` |
| How long does a switch take? Does it touch saved config or TX power? | `hop` | Yes | `findings.timing`, `findings.saved_config_rewritten`, `findings.tx_power_changed` |
| What if one radio misses the switch? | `missed` | Yes | `findings.rejoined_by_itself`, `findings.b_landed_on` |
| Do an RF toggle and a reboot bring a radio home? | `recover` | Yes | `findings.rf_toggle_returns_*_home`, `findings.reboot_returns_home` |

## Quick check from a Mac wired to one radio

Wire the Mac to radio a only. Radio b just needs power and the same mesh settings: the Mac reaches
it through radio a's mesh link. The attenuation, authorization and stopped-service points under
the two-wire setup below apply here too.

Start read-only. It changes nothing:

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> check --read-only
```

It checks, for both radios: API login and the permissions the service needs; firmware 2025-06 or
later, and the same on both; RF on and the same channel and bandwidth; exactly one peer, with a
signal that is neither too strong nor weak; data in both directions across the mesh; whether the
radio will run `iw`, which is how the service sets transmit power; and the service's own code
against each radio.

Real radios refuse `file exec` of a shell for the `user` login, whatever its ACL says, so the
tool never uses one. Programs run directly. Data across the mesh is checked with ping where a
radio will run it; on one wire, radio b's API answering through the mesh is the proof instead.
`discover` searches the firmware through the API's file list and file read, which needs read
access only and can take a few minutes. That means the supervisor's read path and every diagnostic
component the service would publish, including the values the dashboard relies on. Those are the
outstanding hardware checks in [PLAN.md](PLAN.md).

Without `--read-only`, once those all pass, it asks before making changes. Radio a's transmit power
moves one supported step through the supervisor's own code, is read back, the link is checked at
that power, and the power goes back. Radio a is always the wired one.

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> check
```

Add `--to` and `--home` (built from `discover`, as below) and it also sends one `chswitch` request
to radio a, checks that both radios moved and data crosses, then sends one to radio b to bring both
back.

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> check --to '<json>' --home '<json>'
```

On one wire, radio b's RF is never toggled: through the mesh, that would cut it off until a power
cycle. If radio b is left on the test channel, the tool sends radio a there to fetch it. If that
fails too, power-cycle radio b, unless the saved config moved (see "If a radio is stranded").
`discover`, `capture` and `hop` also work on one wire; `missed` and `recover` refuse to run.

The tool reads your routes to tell the setups apart. Two radios behind one local interface count as
one wire, and so does a shared switch, which is not a supported setup: `chswitch` could reach radio
b over Ethernet and look like an over-the-air switch.

## Two-wire setup

`missed` and `recover` need this; everything else also works with the quick check's single wire.

1. **Give each radio its own wired path, never a shared switch.** `message-system` broadcasts on
   `br-mrlan`, the bridge that holds the wired port. On a shared LAN, `chswitch` can reach the other
   radio over Ethernet and look like a successful over-the-air switch, and the bridged mesh makes
   a loop. Use one Ethernet adapter per radio. They are usually on one subnet, so pin a route to
   each, then confirm each with `route -n get`:

   ```bash
   sudo route -n add -host <radio-a> -interface <adapter-a>
   ```

   ```bash
   sudo route -n add -host <radio-b> -interface <adapter-b>
   ```

   The wired path is also how the tool reaches a radio with RF off. Without it, `missed` and
   `recover` leave that radio off until a power cycle.
2. **Keep the link out of saturation.** Doodle's vehicle validation guide calls RSSI at or above
   −30 dBm too strong. In free space with 2 dBi antennas that is inside about 20 m at 32 dBm and
   about 4 m at 19 dBm, so a bench needs attenuation: roughly 40 dB in total puts radios 1 m apart
   at 19 dBm near −60 dBm. Attenuate both antenna chains and terminate every port. The tool warns
   when a peer is too strong.
3. **Use only frequencies you are authorized to transmit on.** The firmware will not limit you to
   them. There is no default test frequency, on purpose.
4. **Make these two radios the whole mesh**, on one firmware, mesh ID, key, channel and bandwidth.
   `chswitch` is mesh-wide, so the tool refuses to move a mesh with a third node.
5. **Stop the service on both radios** (`./doodle-up <config> down`): it re-applies its power target
   while the tool is working.

## Running

Each stage prompts for the password (Enter reuses the first for the second radio).

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> discover
```

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> capture --seconds 60
```

Doodle's page on `chswitch` needs a vendor login, so `--to` and `--home` are passed through exactly
as given and nothing is guessed. Build them from what `discover` found. Saved configs suggest the
shape (`central_acs` holds the sub-model as `freq_band`, a channel number and a bandwidth string,
and a channel number is MHz − 2399 on this radio), but `mode_change` and `channel_idx` are unknown.
If `discover` does not settle them, ask Doodle before experimenting: `model` selects a band on
multi-band radios. Both must name an integer `frequency` so the tool can check where radios landed.

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> hop --to '<json>' --home '<json>'
```

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> missed --to '<json>' --home '<json>'
```

```bash
python3 tools/radio_bench.py --a <radio-a> --b <radio-b> recover --to '<json>' --home '<json>'
```

Active stages refuse to start unless both radios are up on the home channel, hear exactly one peer
and pass a ping across the mesh. Afterwards, even after Ctrl-C or a crash in a stage, they bring
both radios home with `chswitch`, then an RF toggle if that fails, and restore the starting power.

Exit status: 0 done, 1 a failed check or refusal, 3 **a radio was not restored**.

## If a radio is stranded

Exit 3 prints each radio's state. Power-cycle the stranded radio: that reloads its saved config.
If `saved_config_rewritten` was true or `reboot_returns_home` was false, the saved config moved
too, so a power cycle will not help. Set the channel back over the wired path in the web UI.

## What the results decide

| Result | Meaning for the flight |
|---|---|
| `saved_config_rewritten: true`, or `reboot_returns_home: false` | A switch outlives a power cycle, so a lost radio cannot recover that way. Stop and rethink before any flight work. |
| `rf_toggle_returns_*_home: true` | An RF toggle is a usable last-resort return to the home channel. |
| `rejoined_by_itself: null` | The firmware will not reunite a split mesh. Both ends need their own watchdog. |
| `timing.data.max_s` | The settle time to discard after every channel change. |
| `tx_power_changed` not empty | A switch resets power, so the flight needs a numeric target, not `unchanged`. |
| `not_published` not empty | `health.py` does not map the real fields yet. Fix that before relying on `link.*`. |
| `switches_completed` short of the total | The rest of the flight concept depends on this. Find out why first. |

## Results

Each run writes `report.json` and `samples.jsonl` to `var/bench/<UTC time>-<stage>/`, which is
gitignored. Vendor text is scrubbed of anything that looks like a secret, but that is pattern
matching and peer MAC addresses stay in, so read a report before sharing it.
