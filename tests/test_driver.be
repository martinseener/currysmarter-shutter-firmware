# test_driver.be - runs berry/curry_shutter.be against a mocked Tasmota
#
#   berry tests/test_driver.be        (from the repo root, native Berry interpreter)
#
# Covers boot (no target sent), vendor rule shutoff, forwarding a commanded
# move, syncing a wall-button move, and the re-init guard.

import string
import global

class MockTasmota
  var now, sent, rules_state, rules_text, shutter, logs, mqtt_out
  def init()
    self.now = 100000
    self.sent = []
    self.logs = []
    self.mqtt_out = []
    self.rules_state = ["ON", "ON", "ON"]
    self.rules_text = ["ON Var1#state DO backlog TuyaSend2 2,%Var1% ENDON",
                       "ON Var16#state=McuSyn DO ShutterPosition %Var4% ENDON",
                       "ON system#init DO Backlog rule1 1; rule2 1 ENDON"]
    self.shutter = {"Position": 100, "Direction": 0, "Target": 100, "Tilt": 0}
  end
  def millis() return self.now end
  def add_rule(t, f, id) end
  def remove_rule(t, id) end
  def add_cron(p, f, id) end
  def remove_cron(id) end
  def add_cmd(n, f) end
  def remove_cmd(n) end
  def resp_cmnd(s) end
  def cmd(c, mute)
    self.sent.push(c)
    if c == "Status 10" return {"StatusSNS": {"Shutter1": self.shutter}} end
    if c == "FullTopic" return {"FullTopic": "%prefix%/%topic%/"} end
    if c == "Prefix2" return {"Prefix2": "stat"} end
    if c == "Topic" return {"Topic": "shutter"} end
    for i : 1..3
      if c == "Rule" + str(i)
        return {"Rule" + str(i): {"State": self.rules_state[i - 1], "Rules": self.rules_text[i - 1]}}
      end
    end
    if c == "Backlog Rule1 0; Rule2 0; Rule3 0" self.rules_state = ["OFF", "OFF", "OFF"] end
    return {}
  end
  def sent_matching(prefix)
    var n = 0
    for c : self.sent if string.find(c, prefix) == 0 n += 1 end end
    return n
  end
end

class MockMqtt
  def publish(t, p) global.tasmota.mqtt_out.push(t + " " + p) end
end

global.tasmota = MockTasmota()
global.load = def (f) var h = open(f) var c = compile(h.read()) h.close() c() end
global.log = def (m, l) global.tasmota.logs.push(m) end
var mqtt_mock = MockMqtt()
global.mqtt = mqtt_mock

var failed = 0
def check(name, ok)
  print((ok ? "ok   " : "FAIL ") + name)
  if !ok failed += 1 end
end

# make 'import mqtt' inside the driver resolve to the mock
import introspect
var mod = module("mqtt")
mod.publish = def (t, p) global.tasmota.mqtt_out.push(t + " " + p) end
introspect.setmodule("mqtt", mod)

var t = global.tasmota
global.load("berry/curry_shutter.be")
var d = global.curry_shutter

check("boot: vendor rules switched off", t.rules_state[0] == "OFF" && t.rules_state[1] == "OFF" && t.rules_state[2] == "OFF")
check("boot: LED forced off", t.sent_matching("TuyaSend1 7,0") == 1)
check("boot: no target sent", t.sent_matching("TuyaSend2") == 0)

# idle report after boot (stale target) must not move the motor
d.on_mcu_pos(100)
d.on_shutter({"Position": 100, "Direction": 0, "Target": 37, "Tilt": 0})
check("idle report with other target ignored", t.sent_matching("TuyaSend2") == 0)

# HA sets 40 %: Tasmota model starts moving -> target goes to the MCU once
d.on_shutter({"Position": 98, "Direction": -1, "Target": 40, "Tilt": 0})
d.on_shutter({"Position": 90, "Direction": -1, "Target": 40, "Tilt": 0})
check("commanded move forwarded once", t.sent_matching("TuyaSend2 2,40") == 1)
d.on_mcu_pos(40)                                  # MCU jumps to the target
d.on_shutter({"Position": 40, "Direction": 0, "Target": 40, "Tilt": 0})
t.now += 4000
d.on_mcu_pos(40)
check("no sync needed after commanded move", d.n_sync == 0)

# wall button: MCU moves 40 -> 70 on its own, Tasmota follows after it settles
t.mqtt_out = []
for p : [44, 50, 57, 63, 70]
  t.now += 1000
  d.on_mcu_pos(p)
end
check("live positions published", t.mqtt_out.size() >= 4)
t.now += 1500
d.on_mcu_ctrl(1)
d.on_mcu_pos(70)
check("synced to MCU position", t.sent_matching("ShutterSetPosition1 70") == 1 && d.tas_pos == 70)
d.on_shutter({"Position": 70, "Direction": 0, "Target": 70, "Tilt": 0})
check("sync does not echo a target", t.sent_matching("TuyaSend2 2,70") == 0)

# re-init: model jumps on 'stop' -> not a command
d.on_shutter({"Position": 70, "Direction": 1, "Target": 100, "Tilt": 0})
var before = t.sent_matching("TuyaSend2")
d.on_shutter({"Position": 20, "Direction": 0, "Target": 20, "Tilt": 0})
check("re-init jump not forwarded", t.sent_matching("TuyaSend2") == before)

# second load keeps one instance and does not switch rules again
t.rules_state = ["OFF", "OFF", "OFF"]
var backlogs = t.sent_matching("Backlog Rule1 0")
global.load("berry/curry_shutter.be")
check("reload: rules untouched when already off", t.sent_matching("Backlog Rule1 0") == backlogs)

# user rules without the vendor signature are left alone
t.rules_text = ["ON Power1#state DO publish x ENDON", "ON Time#Minute DO y ENDON", ""]
t.rules_state = ["ON", "ON", "OFF"]
global.curry_shutter.disable_vendor_rules()
check("user rules left alone", t.rules_state[0] == "ON")

print(failed == 0 ? "ALL PASSED" : string.format("%i FAILED", failed))
if failed > 0 raise "test_failed", "driver tests failed" end
