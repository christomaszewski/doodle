# Doodle Labs radio API — verified behavior

What this service relies on, as measured on real hardware. Anything not listed under
"Verified" is an assumption and is called out as such.

**Tested:** a single-radio Mesh Rider (QCA9533/ath9k) on firmware `firmware-2026-03.1`, single node (no peers on the mesh). Earlier probes on
`firmware-2023-09.03` and `firmware-2025-06.5` are noted where behavior differed.
**Supported by the service:** firmware 2025-06 and later.

## Transport

- JSON-RPC 2.0, `POST https://<radio>/ubus` (uhttpd `-u /ubus`). Self-signed certificate.
- Login: `{"method":"call","params":["00000000000000000000000000000000","session","login",
  {"username":"user","password":"…"}]}`. The reply carries `ubus_rpc_session`, `timeout: 300`
  and the granted `acls`. A session stays alive while it is used; an expired or unknown session
  returns the JSON-RPC error `{"code":-32002,"message":"Access denied"}` → log in again.
- Every call: `{"method":"call","params":[<session>,<object>,<method>,<args>]}`. The reply is
  `"result":[<ubus status>]` or `"result":[0,<data>]`.

| ubus status | Meaning |
|---|---|
| 0 | OK |
| 1 | invalid command |
| 2 | invalid argument |
| 3 | method not found |
| 4 | not found (e.g. `file read` of a missing file) |
| 5 | no data |
| 6 | permission denied |
| 7 | timeout |
| 8 | not supported |
| 9 | unknown error |
| 10 | connection failed |

## Accounts

| Firmware | API login | Permissions |
|---|---|---|
| 2025-06 and later | `user` (default password `DoodleSmartRadio`; change it) | `access 'Full Access'`, ACL group `custom_dl_user`: every ubus object/method, all UCI, every file |
| 2025-06 and later | `root` | `rpcjson '0'` — Doodle says not to use it for the API |
| 2023-09 | `root` only | restricted `root.json` role (`iwinfo info` only, `iw *` via `file exec`, no `network.wireless`) |

The `user` password is effectively root over the API, and since the June 2025 firmware the
firewall accepts all incoming connections from the mesh. Fielded radios need a strong password.

## Calls the service uses

| Purpose | Call | Fields used |
|---|---|---|
| Identity | `system board` | `release.version`, `parent_model`, `sub_model`, `hostname` |
| Reboot detection, load | `system info` | `uptime` (s), `load` (×65536), `memory` |
| Effective power | `iwinfo info {device}` | `txpower` (int dBm), `channel`, `frequency`, `noise`, `country`, `htmode` |
| Supported power range | `iwinfo txpowerlist {device}` | `results[].dbm` (19…32 on the tested radio), `active` |
| Automatic power control | `uci get {config:wireless, section:<radio>}` | `values.dynamic_txpower`, `dynamic_txpower_aggr`, `chanbw` |
| RF on/off | `network.wireless status` | `<radio>.up` |
| Link status | `file read {path:/tmp/linkstate_current.json}` | see below |
| **Set power** | `file exec {command:/usr/sbin/iw, params:[dev,<device>,set,txpower,fixed,<dBm×100>]}` | reply `{code, stdout, stderr}` |
| **RF off / on** | `network.wireless down` / `up` `{device:<radio>}` | — |

Never called: `link-status logs` (exports the long-term log as an archive in `/tmp`, which is
RAM), anything that commits UCI or writes flash.

### Replies that contain secrets

Never log raw replies to these; extract the fields above and drop the rest.

- `network.wireless status` → `interfaces[].config.key` (mesh key, plain text)
- `uci get wireless <wifi-iface>` → `key`
- `uci get hotspot-wizard` → the Setup Wizard command line, which carries the key after `-k`
- `uci get rpcd` → `pswd` (the `user` password in plain text)

## Transmit power (bench results, 2026-03.1)

| Action | Result |
|---|---|
| `iw … set txpower fixed 2500` | 25 dBm, applied immediately |
| `iw … set txpower fixed 1500` (below range) | exit 0, **silently clamped to 19** |
| `iw … set txpower fixed 0` | 19 — the floor |
| `iw … set txpower auto` | 32 — the maximum |
| Fixed 19 dBm, automatic power control on, no peers | held for 60 s |
| `network.wireless down` | `wlan0` removed; wired management unaffected |
| `network.wireless up` | back after < 40 s at **32 dBm**, not the previous setting |

Consequences: requests are validated against `txpowerlist` before sending, every change is read
back, and the target is re-applied after RF comes back, after a radio reboot (uptime drops) and
after any wifi restart. Nothing above writes flash; a power cycle restores the saved config.

The 2023-09 firmware reported the same 19 dBm floor while its saved config asked for less and
its `txpowerlist` listed values below the floor; the 2026-03 list is accurate. The regulatory
domain can allow more than the hardware maximum (32 dBm here, per `iw phy`).

## Automatic power control (`ath9k_tpcd`)

Enabled by the Setup Wizard (`wireless.<radio>.dynamic_txpower='1'`, `dynamic_txpower_aggr='0'`).
A Lua daemon; it never changes the global `iw` level. Every cycle, per peer:

- **Per-peer limit** — `…/stations/<mac>/fixed_txpower` (debugfs). Set only when the peer reports
  hearing us above −35 dBm: `limit = power_we_used − (peer_signal − (−35))`. Otherwise 255 (none).
- **ACK and broadcast power** — `ath9k/txpower_selfgen`, `ath9k/txpower_mcast` (debugfs): the power
  the weakest peer needs to hear −85 dBm (capped at 30), raised to at least the lowest per-peer
  limit. With no close peer that minimum is 255, so the override is 255 = follow the global level.
- **LNA bypass** — only with `dynamic_txpower_aggr='1'`.

So the global level is exact except at very close range. One narrow case can exceed it: with one
very close peer and another very weak one, ACK/broadcast power can be set above the global level
(e.g. 29 dBm while the level is 19). Whether the driver honors that above the global limit is
**not verified**. The service reports these values and warns when they exceed the target.

Per-peer debugfs files, readable via `file read`
(`/sys/kernel/debug/ieee80211/phy0/netdev:wlan0/stations/<mac>/…`): `fixed_txpower`,
`peer_signal` (how we hear the peer), `peer_signal_reply` (how the peer hears us, and the power
we used). Format not yet observed (no peers).

## Link status (`linkstate`)

`/tmp/linkstate_current.json`, rewritten every 2 s (`linkstate.linkstate.interval`). With no
peers:

```json
{ "sysinfo": { "cpu_load": [ 20000, 18000, 19000 ], "freemem": 12000000, "localtime": 1700000000 },
  "oper_chan": 51, "oper_freq": 2450, "chan_width": "20",
  "noise": -94.0, "noise_peak": -92.0, "activity": 1, "lna_status": "1",
  "sta_count": 0, "sta_stats": [ ], "mesh_count": 0, "mesh_stats": [ ] }
```

Configured to include per-peer RSSI, inactive time, packet loss (`pl_ratio`), TX rate/MCS/width,
mesh TQ, hop status, last seen, `fixed_txpower` and average bitrate. The per-peer entry format is
**not yet observed**. `sysinfo.localtime` is the radio clock, which is wrong (no time sync) — use
host receipt time.

## Not available

- Temperature: no hwmon or thermal sensors.
- Radio Silence Mode (`wow.wow.fullsilence`) exists in 2026-03 but appears license-gated; not used.

## Not yet verified (needs a second radio)

- Per-peer formats in `linkstate` and the debugfs station files.
- Automatic power control against a fixed level with a close peer.
- Reported vs. radiated power (signal-strength change on a peer).
- Whether a live power change interrupts the link.
