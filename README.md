# Currysmarter shutter firmware

Upgrade the **Currysmarter Smart TASMOTA roller shutter switch** (EU-CL02, ESP32-C3 + Tuya MCU) from the vendor's Tasmota 13/14 to **Tasmota 16**, both app and safeboot. A small **Berry driver** replaces the vendor rules, which make shutters move on their own.

One Python script does everything from a Mac or Linux shell: checks, backup, its own web server for the firmware, app upgrade, safeboot upgrade, driver installation and travel-time calibration. There's nothing to install besides Python 3.8+.

```sh
git clone https://github.com/martinseener/currysmarter-shutter-firmware.git
cd currysmarter-shutter-firmware
python3 currysmarter-upgrade.py check   192.168.1.50      # read-only
python3 currysmarter-upgrade.py upgrade 192.168.1.50      # ~5-8 min per shutter
```

Background story: [Upgrading Currysmarter Roller Shutters to Tasmota 16 - and Why They Moved on Their Own](https://www.sysorchestra.com/upgrading-currysmarter-roller-shutters-to-tasmota-16/) · Setup of these shutters with Home Assistant: [Setting Up Currysmarter Tasmota Roller Shutters with Home Assistant](https://www.sysorchestra.com/setting-up-currysmarter-tasmota-roller-shutters-with-home-assistant/)

> [!WARNING]
> Flashing firmware is at your own risk. The procedure keeps the old app until the new one is verified, and the safeboot partition stays usable as a fallback. Still, a power loss while the safeboot itself is being rewritten (a few seconds in step 3) can leave the device needing a serial flash. I also do NOT take responsibility by using the update script or custom firmware although I've tested and use the custom firmware on 12 of my devices.

## Why?

The shutters work out of the box, but the vendor firmware has some issues:

| Vendor firmware | After this upgrade |
|---|---|
| **Shutters sometimes move on their own**, e.g. close and then reopen to a random position. Three rules sync Tasmota's shutter model with the Tuya MCU and keep switching each other on and off. After a reboot they send the last *saved* target to the motor, and Rule3 reboots the device on every WiFi drop. | The driver forwards only commanded moves. A stale target never reaches the motor. |
| Tasmota 13.0.0 (2023) with a 14.0-dev safeboot | Tasmota 16.0.0 app and safeboot |
| Upgrading to stock Tasmota fails: the device reboots into safeboot and falls back to v13 | Upgrades work: the script serves the firmware from your computer and checks first that the shutter can reach it |
| Toggling rules writes the settings to flash all the time (SaveCount between 7,000 and 71,000 on our 12 devices) | No extra flash writes |
| Rule3 restarts the device on every WiFi disconnect | Tasmota simply reconnects |
| Every WiFi connect switches the button LEDs **on** (hence the "LEDs off every 15 min" automations) | LEDs stay off (configurable) |
| HA gets the MCU position as `Var4` (needs a special `position_template`) | Live position about once per second as a normal `Shutter1` result, and the final position about 1-3 s after the stop |
| **A factory backup WiFi "T-test" / "88888888" is configured.** If your WiFi is down, the shutter joins any AP with that name. With no web password set, whoever runs that AP can take the device over | The upgrade clears it |

Your Home Assistant setup keeps working unchanged: same MQTT topics, same `Shutter1`, and `tuyasend4` and `ShutterPosition` work as before.

## Choose your firmware

| | `--firmware custom` (default) | `--firmware stock` |
|---|---|---|
| App image | `tasmota32c3-currysmarter.bin`: official Tasmota v16.0.0 plus a small installer that carries the Berry driver | `tasmota32c3.bin`: the official v16.0.0 release, byte-identical |
| Berry driver | written to the file system by the firmware on first boot | uploaded by the script (or later with `install-driver`) |
| Vendor rules | switched off by the driver (text kept) | switched off by the script, or kept with `--no-driver` |
| Version shown | `16.0.0(currysmarter)` | `16.0.0(release-tasmota32)` |

Both use the official v16.0.0 safeboot. `firmware/manifest.json` lists the sha256 of every file, and the script refuses to serve a file that doesn't match. The custom image is built by [`build/build.sh`](build/build.sh) from the official v16.0.0 tag plus [`build/xdrv_124_curry_installer.ino`](build/xdrv_124_curry_installer.ino). Build it yourself if you prefer.

If you don't trust either image, flash any official Tasmota ESP32-C3 build by hand and run `install-driver` afterwards.

## Requirements

- The shutter's web UI is reachable from your computer.
- **The shutter can open a TCP connection to your computer** on port 8000 (`--port`). This is the step that silently fails in many networks (IoT VLANs, client firewalls, the macOS firewall or Little Snitch for `python3`). `check` and `upgrade` test it first: they make the shutter call the script's web server with `WebSend`. Nothing is flashed until that works.
- Python 3.8+ (preinstalled on macOS and most Linux distributions).
- If you set a `WebPassword`: pass `--password`.

## Commands

```sh
python3 currysmarter-upgrade.py check 192.168.1.50 192.168.1.51 ...
```
Read-only. Shows version, partitions, TuyaMCU mapping, rules, driver, travel times, WiFi 2 and boot/save counters, and whether the shutter can reach your computer.

```sh
python3 currysmarter-upgrade.py upgrade 192.168.1.50 [192.168.1.51 ...] [--firmware stock] [--no-driver]
```
For each shutter, one after another, or `--jobs N` at a time (no further shutter is started after an error):
1. checks (aborts if something doesn't match, or if the shutter is moving)
2. saves a config backup to `backups/` (it contains your WiFi/MQTT passwords, so keep it private)
3. **app** → `OtaUrl http://<you>:8000/<image>` and `Upgrade 1`. The device reboots into its safeboot, which downloads the app. The script shows the download progress.
4. **safeboot** → `Upgrade 2` (`<OtaUrl name>-safeboot.bin`), then `Restart 3` back to the app
5. resets `OtaUrl` to the official Tasmota URL (change it with `--ota-url`)
6. installs and verifies the Berry driver, and switches the vendor rules off
7. clears the factory WiFi 2 "T-test" (keep it with `--keep-wifi2`)

The shutters don't move during the upgrade: only the ESP restarts, the Tuya MCU keeps its position. With `--skip-safeboot` (safeboot already on 16) it takes about a minute per shutter. Settings (WiFi, MQTT, template, shutter) are kept. On our devices 13.0.0 → 16.0.0 migrated them without changes.

```sh
python3 currysmarter-upgrade.py calibrate 192.168.1.50 [--apply]
```
**Moves the shutter**: fully open first if needed, then fully closed and fully open again. It times each run from the MCU's position reports and suggests `ShutterOpenDuration1`/`ShutterCloseDuration1`. `--apply` sets them. Correct durations make the live position in HA and `ShutterPosition` moves accurate.

```sh
python3 currysmarter-upgrade.py install-driver 192.168.1.50   # Tasmota >= 14 with Berry
python3 currysmarter-upgrade.py rescue 192.168.1.50           # stuck in safeboot -> Restart 3
python3 currysmarter-upgrade.py serve                         # only the web server, for manual upgrades
```

## Manual upgrade (without the script)

Serve `firmware/` over HTTP (`python3 currysmarter-upgrade.py serve` or `python3 -m http.server 8000 -d firmware`) and run this in the shutter's console:

```
Backlog Rule1 0; Rule2 0; Rule3 0                                  # only with the driver
OtaUrl http://<your-ip>:8000/tasmota32c3-currysmarter.bin          # or tasmota32c3.bin
Upgrade 1                                                          # wait for 16.0.0
OtaUrl http://<your-ip>:8000/tasmota32c3.bin
Upgrade 2                                                          # ends in safeboot 16.0.0
Restart 3                                                          # (in safeboot) back to the app
OtaUrl http://ota.tasmota.com/tasmota32/release/tasmota32c3.bin
Backlog SSId2 1; Password2 1                                       # remove the "T-test" WiFi
```

For the stock image, upload `berry/curry_shutter.be` and `berry/autoexec.be` in *Tools → Manage File system* and restart.

> [!IMPORTANT]
> The safeboot file name is derived from the `OtaUrl` and **cut at the first dot**. So `tasmota32c3-16.0.0.bin` becomes `tasmota32c3-safeboot.0.0.bin`, which doesn't exist. Use a file name without a version for `Upgrade 2`.

## The Berry driver

[`berry/curry_shutter.be`](berry/curry_shutter.be) is loaded by `autoexec.be`. The Tuya MCU stays in charge of the motor and knows the real position. Tasmota's `Shutter1` stays the interface for Home Assistant, MQTT and the web UI.

- **Tasmota → MCU:** a commanded move (HA, MQTT, web, timer) is forwarded once as `TuyaSend2 2,<target>`. Idle reports are never forwarded, so a reboot can't move the motor.
- **MCU → Tasmota:** when the position comes from the MCU (wall buttons, `TuyaSend4`), the driver publishes live positions about once per second on `stat/<topic>/RESULT` and `SHUTTER1`. When the motor stops, it syncs the model with `ShutterSetPosition1`, which moves only the model, never the motor. Motion is judged from the position (DP2): the MCU's control state (DP1) lags or sticks on some units.
- **LEDs:** kept off by default (`LED_MODE` at the top of the file: `0` off, `1` on, `nil` don't touch).
- **Vendor rules:** if they are still active, the driver switches Rule1-3 off once. It recognises them by the `McuSyn` in Rule2, so it never touches your own rules.

Status: `CurryShutter` in the console (or `cmnd/<topic>/CurryShutter`). Log lines start with `CRY:`.
Backout: `Br curry_shutter.unload()`, remove the `load()` line from `autoexec.be`, `Backlog Rule1 1; Rule2 1; Rule3 1`.

The custom firmware writes the driver once per firmware build and records it in `/curry_shutter.ver`. Your later edits stay. To stop the installer for good, write `off` into that file.

Tuya datapoints, the vendor rules and the flash layout are described in [docs/TECHNICAL.md](docs/TECHNICAL.md).

## Tests

```sh
python3 tests/test_upgrade.py      # the script against simulated shutters (tests/fake_shutter.py)
berry tests/test_driver.be         # the driver against a mocked Tasmota (native Berry, see the file)
```

## Status

- Stock image + driver: ran on all my 12 shutters, upgraded from the vendor's 13.0.0 for a day...
- Custom image: after that, flashed the custom image and now all 12 shutters running on it for couple days. `upgrade` took about 2 minutes (app, safeboot, driver), and moves from Home Assistant were tested afterwards.

## License

MIT, see [LICENSE](LICENSE): the script, the Berry driver, the installer source, tests and docs.

The firmware images in `firmware/` are [Tasmota](https://github.com/arendst/Tasmota) by Theo Arends and contributors, licensed under **GPL-3.0** ([firmware/LICENSE-GPL-3.0](firmware/LICENSE-GPL-3.0)). Their source is the Tasmota tag v16.0.0, plus [`build/`](build/) for `tasmota32c3-currysmarter.bin`.

This project is not affiliated with Currysmarter or Tasmota.

## Disclaimer

Use at your own risk. This worked on my shutters, but I don't take any responsibility if you kill your shutter, your device ends up needing a serial flash, or anything else goes wrong.

## A note on AI

The reverse engineering of these devices was supported by AI (Claude Opus 5.5).
