# curry_shutter.be - Berry driver for the Currysmarter EU-CL02 roller shutter
#
# Replaces the vendor Rule1..Rule3. The Tuya MCU drives the motor and knows
# the real position; Tasmota's Shutter1 stays as the interface for HA, MQTT and
# the web UI.
#
# Tuya datapoints (MCU product key csgb8eqhczvjaetl):
#   DP1 enum  control  0=open 1=stop 2=close
#   DP2 value position 0..100 % (set + report, reported about every second)
#   DP7 bool  button LEDs
#   DP8 enum  unknown
#
# What it does:
#   Shutter1 target changes (HA/MQTT/web/timer) -> TuyaSend2 2,<target>
#   MCU position settles (wall buttons, end of move) -> ShutterSetPosition1 <pos>
#     (moves the Tasmota shutter model only, never the motor)
#   optional: keep the button LEDs off/on
#
# Unlike the vendor rules it never toggles rules (no flash writes), never
# restarts on WiFi loss and never sends a stale target to the MCU after boot.
# If the vendor rules (Rule1..3) are still active, it switches them off once
# (their text is kept, so they can be switched back on).
#
# Status:   CurryShutter        (console / MQTT / HTTP)
# Backout:  Br curry_shutter.unload()  then  Rule1 1; Rule2 1; Rule3 1
#           and remove the load() line from autoexec.be
#
# https://github.com/martinseener/currysmarter-shutter-firmware

import string
import json
import global

class CurryShutter
  static VERSION = "1.1.0"
  # --- configuration ---
  static SYNC_TOLERANCE = 2    # resync if |MCU - Tasmota| > this (percent)
  static STOP_MS = 1200        # DP2 unchanged this long and DP1 = stopped: the motor has stopped
  static SETTLE_MS = 3000      # DP2 unchanged this long: stopped, whatever DP1 says (DP1 can lag or stick)
  static CMD_MOVE_MS = 60000   # a forwarded target counts as "our" move at most this long
  static LIVE_MS = 1000       # live position updates to MQTT at most this often
  static REINIT_JUMP = 10      # model position jump (%) on stop that marks a re-init, not a stop
  static LED_MODE = 0         # nil = don't touch LEDs, 0 = keep off, 1 = keep on
  static LED_RETRY_MS = 10000  # at most one LED correction per 10 s
  static ID = "curry_shutter"
  static RULES = ["TuyaReceived#DpType2Id2", "TuyaReceived#DpType4Id1", "TuyaReceived#DpType1Id7",
                  "Shutter1", "Wifi#Connected"]

  var mcu_pos        # last DP2 from the MCU, nil until the first report
  var mcu_pos_ms     # millis() of the last DP2 change
  var mcu_ctrl       # last DP1 from the MCU: 0 opening, 1 stopped, 2 closing
  var led            # last DP7 from the MCU
  var led_fix_ms     # millis() of the last LED correction
  var tas_pos        # Tasmota Shutter1 position
  var tas_dir        # Tasmota Shutter1 direction, 0 = idle
  var last_target    # last target sent to the MCU, or taken over from it
  var pending        # a sync check is due once the MCU position has settled
  var cmd_move       # the running move was commanded through Tasmota (model animates it)
  var fwd_ms         # millis() of the last target forwarded to the MCU
  var live           # live positions of the running move were published
  var live_ms        # millis() of the last live publish
  var topic_base     # cached "stat/<topic>/"
  var n_fwd          # targets forwarded to the MCU
  var n_sync         # Tasmota position corrections

  def init()
    self.tas_dir = 0
    self.pending = true
    self.cmd_move = false
    self.fwd_ms = 0
    self.live = false
    self.live_ms = 0
    self.led_fix_ms = 0
    self.n_fwd = 0
    self.n_sync = 0
    var id = self.ID
    tasmota.add_rule("TuyaReceived#DpType2Id2", / v -> self.on_mcu_pos(v), id)
    tasmota.add_rule("TuyaReceived#DpType4Id1", / v -> self.on_mcu_ctrl(v), id)
    tasmota.add_rule("TuyaReceived#DpType1Id7", / v -> self.on_led(v), id)
    tasmota.add_rule("Shutter1", / m -> self.on_shutter(m), id)
    tasmota.add_rule("Wifi#Connected", / -> self.apply_led(), id)
    tasmota.add_cron("0 */2 * * * *", / -> self.check_sync("cron"), id)
    tasmota.add_cmd("CurryShutter", / cmd, idx, payload -> tasmota.resp_cmnd(self.status()))
    var st = tasmota.cmd("Status 10", true)               # current model position, no movement
    if st != nil && st.contains("StatusSNS") && st["StatusSNS"].contains("Shutter1")
      var s = st["StatusSNS"]["Shutter1"]
      self.tas_pos = int(s["Position"])
      self.tas_dir = int(s["Direction"])
    end
    self.disable_vendor_rules()
    self.apply_led()
    log("CRY: driver " + self.VERSION + " loaded", 2)
  end

  # The vendor rules do the same job as this driver; both together would
  # fight. They are recognised by Rule2 (the 'McuSyn' sync), and all three are
  # switched off, since Rule3 switches Rule1/2 back on at every boot.
  def disable_vendor_rules()
    var r2 = tasmota.cmd("Rule2", true)
    if r2 == nil || !r2.contains("Rule2") return end
    if string.find(str(r2["Rule2"]["Rules"]), "McuSyn") < 0 return end
    var on = false
    for i : 1..3
      var r = tasmota.cmd("Rule" + str(i), true)
      if r != nil && r.contains("Rule" + str(i)) && r["Rule" + str(i)]["State"] == "ON" on = true end
    end
    if on
      log("CRY: vendor rules active, switching Rule1..3 off", 2)
      tasmota.cmd("Backlog Rule1 0; Rule2 0; Rule3 0", true)
    end
  end

  def unload()
    for r : self.RULES
      tasmota.remove_rule(r, self.ID)
    end
    tasmota.remove_cron(self.ID)
    tasmota.remove_cmd("CurryShutter")
    log("CRY: driver unloaded", 2)
  end

  # --- MCU -> Tasmota ---

  # DP2 is reported about once per second when idle and 2-3 times per second
  # while the motor runs. During a wall-button move it is the live position;
  # during a move commanded via DP2 it jumps to the target at once.
  # Motion is judged from DP2: DP1 lags or sticks on some MCUs (it may stay
  # 'stopped' for a whole run, or report the direction only after arriving).
  def on_mcu_pos(v)
    v = int(v)
    var now = tasmota.millis()
    if self.cmd_move && (now - self.fwd_ms) >= self.CMD_MOVE_MS
      self.cmd_move = false                                # forwarded target never led to a stop
    end
    if v != self.mcu_pos
      var delta = (self.mcu_pos != nil) ? v - self.mcu_pos : 0
      if self.mcu_pos == nil
        log(string.format("CRY: first MCU position %i", v), 2)
      end
      self.mcu_pos = v
      self.mcu_pos_ms = now
      self.pending = true
      # motor running: pass live positions of moves Tasmota did not command
      if delta != 0 && !self.cmd_move && self.tas_dir == 0 && (now - self.live_ms) >= self.LIVE_MS
        self.live = true
        self.live_ms = now
        self.publish_position(v, delta > 0 ? 1 : -1)
      end
    elif self.pending
      var still = now - self.mcu_pos_ms
      if (still >= self.STOP_MS && self.mcu_ctrl == 1) || still >= self.SETTLE_MS
        self.pending = !self.check_sync(self.mcu_ctrl == 1 ? "stopped" : "settled")
      end
    end
  end

  def on_mcu_ctrl(v)
    v = int(v)
    if v != self.mcu_ctrl
      log(string.format("CRY: MCU control %s -> %i", str(self.mcu_ctrl), v), 3)
      self.mcu_ctrl = v
      self.pending = true
    end
  end

  # returns false if the check had to be postponed
  def check_sync(reason)
    if self.mcu_pos == nil || self.tas_pos == nil return false end
    if self.tas_dir != 0 return false end                  # Tasmota model still moving
    var still = tasmota.millis() - self.mcu_pos_ms
    if still < self.STOP_MS return false end               # position still changing
    if self.mcu_ctrl != 1 && still < self.SETTLE_MS return false end  # DP1 says moving (may lag)
    self.cmd_move = false
    var force = self.live                                  # HA saw live values: always send the final one
    self.live = false
    var diff = self.mcu_pos - self.tas_pos
    if diff > self.SYNC_TOLERANCE || diff < -self.SYNC_TOLERANCE || (force && diff != 0)
      self.n_sync += 1
      self.last_target = self.mcu_pos                      # the resulting Target event must not go back to the MCU
      log(string.format("CRY: sync (%s) Tasmota %i -> MCU %i", reason, self.tas_pos, self.mcu_pos), 2)
      tasmota.cmd(string.format("ShutterSetPosition1 %i", self.mcu_pos), true)
      self.tas_pos = self.mcu_pos
      self.publish_position(self.mcu_pos, 0)
    elif force
      self.publish_position(self.mcu_pos, 0)
    end
    return true
  end

  # ShutterSetPosition publishes nothing, so send what Tasmota sends during
  # and at the end of a move; HA's cover reads these topics.
  def publish_position(p, dir)
    import mqtt
    if self.topic_base == nil
      var full = tasmota.cmd("FullTopic", true)["FullTopic"]
      var stat = tasmota.cmd("Prefix2", true)["Prefix2"]
      var topic = tasmota.cmd("Topic", true)["Topic"]
      self.topic_base = string.replace(string.replace(full, "%prefix%", stat), "%topic%", topic)
    end
    mqtt.publish(self.topic_base + "SHUTTER1", str(p))
    mqtt.publish(self.topic_base + "RESULT",
      string.format('{"Shutter1":{"Position":%i,"Direction":%i,"Target":%i,"Tilt":0}}', p, dir, p))
  end

  # --- Tasmota -> MCU ---

  # {"Shutter1":{"Position":p,"Direction":d,"Target":t,"Tilt":x}}
  def on_shutter(m)
    if type(m) != 'instance' || !m.contains("Target") return end
    var dir = int(m["Direction"])
    var pos = int(m["Position"])
    var was_moving = (self.tas_dir != 0)
    var jump = (self.tas_pos != nil) ? pos - self.tas_pos : 0
    self.tas_pos = pos
    self.tas_dir = dir
    if dir == 0 && was_moving self.pending = true end      # re-check once the model stopped

    var t = int(m["Target"])
    if t == self.last_target return end                    # reports repeat the target while moving
    # Only a commanded move (model starts moving, or a stop ends one) goes to
    # the MCU. Reports while idle (tele, boot, after a sync) are ignored, so a
    # stale target can never move the motor.
    if dir == 0 && !was_moving return end
    # A real stop ends where the model is. A shutter re-init (e.g. changing
    # ShutterOpenDuration during a move) makes the model jump to its saved
    # position; that is not a command, the sync will follow the MCU instead.
    if dir == 0 && (jump > self.REINIT_JUMP || jump < -self.REINIT_JUMP)
      log(string.format("CRY: model re-init (%i%% jump), target %i not sent", jump, t), 2)
      return
    end
    self.last_target = t
    if self.mcu_pos == nil
      log(string.format("CRY: target %i ignored, MCU position unknown", t), 2)
      return
    end
    self.n_fwd += 1
    self.cmd_move = true
    self.fwd_ms = tasmota.millis()
    log(string.format("CRY: target %i -> MCU", t), 2)
    tasmota.cmd(string.format("TuyaSend2 2,%i", t), true)
  end

  # --- LEDs ---

  def on_led(v)
    self.led = int(v)
    if self.LED_MODE != nil && self.led != self.LED_MODE &&
       (tasmota.millis() - self.led_fix_ms) >= self.LED_RETRY_MS
      self.apply_led()
    end
  end

  def apply_led()
    if self.LED_MODE == nil return end
    self.led_fix_ms = tasmota.millis()
    tasmota.cmd(string.format("TuyaSend1 7,%i", self.LED_MODE), true)
  end

  # --- diagnostics ---

  def status()
    return json.dump({"CurryShutter": {"Version": self.VERSION,
      "MCU": {"Position": self.mcu_pos, "Control": self.mcu_ctrl, "LED": self.led},
      "Tasmota": {"Position": self.tas_pos, "Direction": self.tas_dir},
      "LastTarget": self.last_target, "Pending": self.pending, "CmdMove": self.cmd_move, "Live": self.live,
      "Forwarded": self.n_fwd, "Synced": self.n_sync, "LEDMode": self.LED_MODE}})
  end
end

if global.contains("curry_shutter") && global.curry_shutter != nil
  global.curry_shutter.unload()
end
global.curry_shutter = CurryShutter()
