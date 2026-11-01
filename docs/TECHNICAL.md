# Technical notes

What I found while reverse-engineering 12 Currysmarter EU-CL02 shutters (bought March 2025). Everything was read through the Tasmota web API and Berry. No device was opened.

## Hardware

- **ESP32-C3** (Tasmota reports `ESP8685 v0.4`, the C3 in a small package), 4 MB flash
- **Tuya MCU** on GPIO20/21 (template `{"NAME":"TuyaMCU","GPIO":[1,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0,1,1,2304,2272],"FLAG":0,"BASE":1}`). Product `{"p":"csgb8eqhczvjaetl","v":"1.0.0","m":0}`, protocol v3. The MCU drives the motor and handles the touch buttons.
- No secure boot, no flash encryption. The bootloader, safeboot and app are plain images with valid SHA.

## Flash layout (Tasmota safeboot layout)

```
nvs       data/nvs     0x009000  0x005000
otadata   data/ota     0x00E000  0x002000
safeboot  app/factory  0x010000  0x0D0000   (832 KB)
app0      app/ota_0    0x0E0000  0x2D0000   (2880 KB)
spiffs    data/spiffs  0x3B0000  0x050000   (320 KB LittleFS: settings, Berry files)
```

As shipped: app `13.0.0(Shutter)` (IDF 4.4.5, core 2.0.10) plus a stock `14.0.0` development safeboot (IDF 5.1.3, core 3.0.0). The vendor later built `15.0.1`, which is stock Tasmota 15.0.1 with compiled-in defaults (the same rules and template, `STA_SSID2 "T-test"`, `STA_PASS2 "88888888"`).

## Tuya datapoints

| DP | Type | Meaning |
|---|---|---|
| 1 | enum | control: `0` open, `1` stop, `2` close. Command: `TuyaSend4 1,<n>`. **Reports are unreliable on some units**: it stays `1` (stopped) during a whole run, or reports the direction only on arrival |
| 2 | value | position 0-100 % (100 = open). Command: `TuyaSend2 2,<pos>`. Reported about every second when idle and 2-3 times per second while moving. During a wall-button or `TuyaSend4` move it is the live position. During a move commanded by DP2 it jumps straight to the target |
| 7 | bool | button LEDs |
| 8 | enum | unknown |

`TuyaMCU 11,4` maps a non-existent DP4 to Relay1. It is only a dummy relay for Tasmota's shutter driver (`ShutterMode 3`).

## The vendor rules

```
Rule1  ON Shutter1#Target DO if(%value%$!%Var1%) Var1 %value% endif ENDON
       ON Var1#state DO backlog TuyaSend2 2,%Var1% ENDON
       ON Power1#state=1 DO rule2 0 ENDON
       ON Power1#state=0 DO rule2 1 ENDON
Rule2  ON Var6#state=1 DO if (Var4>Var3+2 or Var4<Var3-2) backlog rule1 0; Var16 McuSyn endif ENDON
       ON Time#Minute|2 DO if (Var4>Var3+2 or Var4<Var3-2) backlog rule1 0; Var16 McuSyn endif ENDON
       ON Var16#state=McuSyn DO ShutterPosition %Var4% ENDON
       ON Shutter1#Target DO if(%value%$!%Var1%) Var1 %value% endif ENDON
       ON Shutter1#Position Do Var3 %value% ENDON
       ON TuyaReceived#DpType2Id2 Do Var4 %value% ENDON
       ON TuyaReceived#DpType4Id1 DO  if (%value%$!%Var6%) Var6 %value% endif ENDON
       ON Power1#state=0 DO rule1 1 ENDON
Rule3  ON Wifi#Disconnected DO Restart 1 ENDON
       ON system#init DO Backlog rule1 1; rule2 1 ENDON
       ON Wifi#Connected DO Backlog TuyaSend1 7,0; TuyaSend1 7,1; TuyaSend1 7,0; TuyaSend1 7,1 ENDON
       ON Time#Minute|20 DO Backlog rule1 1; rule2 1 ENDON
```

How they work:
- Tasmota → MCU: every new `Shutter1#Target` goes to the MCU as `TuyaSend2 2,<target>`.
- MCU → Tasmota: on DP1 "stopped", or every 2 minutes, if the positions differ by more than 2 %, Rule1 is switched off and a real `ShutterPosition <mcu>` move runs. That is a fake move with the dummy relay, made only to update the model. When the relay switches off, Rule1 is switched on again.

Why shutters move on their own:
- **After every boot** (`system#init`) Rule1 is switched on, and Tasmota reports `Shutter1` with the last *saved* target, while `Var1` is empty. The rule sends that target to the motor. If the shutter was moved with the wall buttons in between, it drives back to the old position. **Rule3 reboots the device on every WiFi disconnect**, so this happens whenever WiFi drops.
- The sync relies on DP1 "stopped" reports, which arrive mid-run on some units, and on a real `ShutterPosition` move with rules toggled around it. An intermediate position can end up as a new target, and the shutter stops and drives to a "random" position.

Side effects:
- Each `rule1 0/1` and `rule2 0/1` persists the settings. With the toggling on every relay change and every 20 minutes, the devices showed **SaveCount 7,000-71,000**.
- `Wifi#Connected` leaves the LEDs **on**.

## Why upgrades to stock Tasmota "failed"

On these devices `Upgrade 1` (or the web "Start upgrade") saves the settings to NVS, erases `otadata` and reboots into the **safeboot**. The safeboot then downloads the app from `OtaUrl` and writes it to app0. If the download fails (`OTA: Connection timeout`, 4 attempts), it stays in safeboot. The next reboot starts the untouched v13 in app0 again. It looks as if the upgrade was rolled back.

On my network the cause was simply that **the safeboot could not open a TCP connection to the download server** from the IoT VLAN. There is no vendor lock, no secure boot, and the safeboot is a stock one. The script therefore serves the firmware itself and checks first with `WebSend` that the shutter can reach it.

Two more details:
- `Upgrade 2` (safeboot OTA) builds the safeboot URL from `OtaUrl`: it cuts the file name at the **first dot**, removes everything from the last dash, and appends `-safeboot` plus the cut-off extension. `…/tasmota32c3.bin` → `…/tasmota32c3-safeboot.bin`. A versioned name like `tasmota32c3-16.0.0.bin` does not work.
- After `Upgrade 2`, Tasmota 16 stays in the new safeboot. `Restart 3` (in safeboot) switches back to the app. The v16 safeboot no longer knows `Status`, but its web page shows the version.

## Travel times

12 devices, measured from the DP2 position reports. Each value is the average of "start → end" and the 10-90 % speed extrapolated to a full run.

| Window | Close / open |
|---|---|
| small windows | 19-22 s |
| doors / large windows | 29-30 s |

The MCU runs about 1 s longer against the end stop and reports "stopped" after that.
