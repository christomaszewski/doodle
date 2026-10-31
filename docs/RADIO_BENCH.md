# Two-radio bench

`tools/radio_bench.py` answers what the service and the characterization flight need to know from
two real radios. It has only been run against simulated radios whose `chswitch` behavior is
invented, so expect the first real run to teach us something. Bench only: never point it at a
vehicle that can fly.

| Question | Stage | Changes the radios | Read in `report.json` |
|---|---|---|---|
| How does the firmware call `chswitch`? | `discover` | No | `radios.a["vendor/callers"]`, `["vendor/strings"]`, `["config/…"]` |
| What do peer entries hold, and does the service's mapping survive them? | `capture` | No | `peers`, `findings.not_published` |
| How long does a switch take? Does it touch saved config or TX power? | `hop` | Yes | `findings.timing`, `findings.saved_config_rewritten`, `findings.tx_power_changed` |
| What if one radio misses the switch? | `missed` | Yes | `findings.rejoined_by_itself`, `findings.b_landed_on` |
| Do an RF toggle and a reboot bring a radio home? | `recover` | Yes | `findings.rf_toggle_returns_*_home`, `findings.reboot_returns_home` |

## Setup

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
