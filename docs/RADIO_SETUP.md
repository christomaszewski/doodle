# Radio setup

One-time preparation of a Doodle Labs Mesh Rider radio for this service, and the recovery
paths learned while bringing up the first one.

## Firmware

Run firmware 2025-06 or later (tested: `firmware-2026-03.1`). Older firmware has no `user` API
login, and the service refuses it at startup.

**Upgrade without keeping settings.** On first boot the firmware's defaults script
(`/etc/uci-defaults/40_doodlelabs`) checks for kept settings and, if it finds them, skips every
new default ("Skipping update of config"). Upgrading 2023 → 2025/2026 with settings kept left:

- no `user` login (the old `/etc/config/rpcd` survives; the web UI then rejects every password),
- the old network layout (Doodle's `message-system` restarts every 5 s),
- a stale channel number and mesh parameters that fail to apply,
- a web UI (Simple Config) that cannot load its configuration.

Record the settings you need (IP, mesh ID and key, frequency, bandwidth, country, TX power),
flash with "keep settings" unchecked (or run `firstboot -y && reboot`), then re-enter them with
the Setup Wizard. Do not restore a backup taken on older firmware.

## Finding the radio after a reset

- Factory address: `10.223.<b5>.<b6>/16`, where `b5`, `b6` are the last two bytes of the radio's
  wireless MAC (`/sys/devices/platform/qca953x_wmac/ieee80211/phy0/macaddress`) in decimal, as
  computed by `/usr/share/simpleconfig/get_ip_from_mac.sh`. Example: `…:12:34` → `10.223.18.52`.
- Also always present: `192.168.153.1/24`.
- Give your computer an address in that range, e.g. on macOS
  `sudo ifconfig <iface> alias 10.223.255.250 255.255.0.0`.
- The first boot after a reset takes several minutes (flash formatting, first-boot scripts,
  calibration). The web UI can answer before logins work; wait before concluding the password
  is wrong.
- IPv6 link-local always works on a direct cable: `ping6 ff02::1%<iface>`, then `ndp -an`.
- Physical reset: hold the button 5–30 s (`firstboot -y && reboot`).

## Credentials

- Web UI and API: `user` / `DoodleSmartRadio`. Change it at once — the API is reachable from the
  whole mesh and the account has full access.
- SSH: `root`, no password after a reset. Set one with `passwd`.
- The service reads the API password from the host file named by
  `connection.ubus.password_file` (keep it `chmod 600`), mounted into the container as a compose
  secret. Only the path appears in the rendered compose or a `rig bake` artifact.
- The Setup Wizard stores its command line, including the mesh key after `-k`, in
  `uci get hotspot-wizard`. Config backups (`sysupgrade -b`) contain the mesh key and the
  API password in plain text.

## SSH from a modern client

Older firmware offers only `ssh-rsa` host keys, which OpenSSH ≥ 8.8 disables:

```
Host doodle
  HostName <radio-ip>
  User root
  HostKeyAlgorithms +ssh-rsa
  PubkeyAcceptedAlgorithms +ssh-rsa
```

2026-03 also accepts ECC (ed25519) client keys. After a reset the host key changes:
`ssh-keygen -R <radio-ip>`.

## Settings the service expects

- One radio (`radio0`, `wlan0`), mode Mesh.
- If the radio's own address is moved off 10.223.0.0/16 (rather than added alongside it), set
  `message-system.settings.brodcast_addr` (Doodle's spelling) for Sense messaging.
- Automatic power control may be on or off; the service reports which. See
  [RADIO_API.md](RADIO_API.md#automatic-power-control-ath9k_tpcd).

## Questions open with Doodle Labs

- Boot messages `model_generator: Checksum mismatch … Aborting model generation` and
  `RAM of freq is corrupted. No hope in dual antenna area` (after per-frequency calibration).
- Their supported way to set transmit power programmatically.
- Whether the driver caps `txpower_selfgen`/`txpower_mcast` at the global level.
- Licensing and an API for Radio Silence Mode.
- Whether the 2026-03 release is still beta.
