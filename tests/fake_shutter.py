#!/usr/bin/env python3
"""fake_shutter.py <port> [--state vendor13|stock16] - a simulated Currysmarter shutter

Answers the Tasmota web API the way the real devices did during the migration
(vendor 13.0.0 with safeboot 14, vendor rules, factory WiFi 2), and really
downloads firmware from the OtaUrl like the safeboot does, including the
"-safeboot" file name rule of Upgrade 2. Used by tests/test_upgrade.sh.
"""
import argparse
import http.server
import json
import re
import threading
import time
import urllib.parse
import urllib.request

VENDOR_RULES = {
    1: "ON Shutter1#Target DO if(%value%$!%Var1%) Var1 %value% endif ENDON ON Var1#state DO backlog TuyaSend2 2,%Var1% ENDON",
    2: "ON Var6#state=1 DO if (Var4>Var3+2 or Var4<Var3-2) backlog rule1 0; Var16 McuSyn endif ENDON",
    3: "ON Wifi#Disconnected DO Restart 1 ENDON ON system#init DO Backlog rule1 1; rule2 1 ENDON",
}
TEMPLATE = {"NAME": "TuyaMCU", "GPIO": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 2304, 2272],
            "FLAG": 0, "BASE": 1}


class Shutter:
    def __init__(self, state):
        self.lock = threading.Lock()
        self.mode = "app"                     # app | safeboot | off
        self.app = "13.0.0(Shutter)" if state == "vendor13" else "16.0.0(release-tasmota32)"
        self.safeboot = "14.0.0" if state == "vendor13" else "16.0.0"
        self.rules = {i: ["ON", VENDOR_RULES[i]] for i in (1, 2, 3)}
        self.ssid2 = "T-test"
        self.ota_url = "http://ota.tasmota.com/tasmota32/release/tasmota32c3.bin"
        self.fs = {}
        self.driver = False
        self.forwarded = 0
        self.boot_count = 10
        self.log = []

    # boot sequence of the app: installer (custom image) -> autoexec -> driver
    def boot_app(self):
        self.boot_count += 1
        if "currysmarter" in self.app and self.fs.get("/curry_shutter.ver") != "custom":
            self.fs["/curry_shutter.be"] = "embedded driver"
            self.fs.setdefault("/autoexec.be", 'load("/curry_shutter.be")\n')
            self.fs["/curry_shutter.ver"] = "custom"
        if self.rules[3][0] == "ON":          # vendor Rule3: system#init enables Rule1/2
            self.rules[1][0] = self.rules[2][0] = "ON"
        self.driver = "curry_shutter.be" in self.fs.get("/autoexec.be", "") and "/curry_shutter.be" in self.fs
        if self.driver and "McuSyn" in self.rules[2][1]:
            for i in (1, 2, 3):
                self.rules[i][0] = "OFF"
        self.mode = "app"

    def reboot(self, into="app", delay=2):
        def run():
            self.mode = "off"
            time.sleep(delay)
            with self.lock:
                if into == "app":
                    self.boot_app()
                else:
                    self.mode = "safeboot"
        threading.Thread(target=run, daemon=True).start()

    def download(self, url):
        self.log.append("GET " + url)
        data = urllib.request.urlopen(url, timeout=30).read()
        if not data or data[0] != 0xE9:
            raise ValueError("not an ESP image")
        return data

    def ota(self, factory):
        """Upgrade 1 (app via safeboot) or Upgrade 2 (safeboot from the app)."""
        def run():
            url = self.ota_url
            if factory:                       # support_tasmota.ino: name up to last dash + -safeboot + type
                base, name = url.rsplit("/", 1)
                dot = name.find(".")
                ext = name[dot:] if dot >= 0 else ""
                stem = name[:dot] if dot >= 0 else name
                stem = stem[:stem.rfind("-")] if "-" in stem else stem
                url = "%s/%s-safeboot%s" % (base, stem, ext)
                time.sleep(1)
                try:
                    self.download(url)
                    self.safeboot = "16.0.0"
                except Exception as e:
                    self.log.append("safeboot download failed: %s" % e)
                self.mode = "off"
                time.sleep(2)
                self.mode = "safeboot"        # Tasmota ends in the new safeboot
                return
            self.mode = "off"
            time.sleep(2)
            self.mode = "safeboot"
            time.sleep(2)
            try:
                data = self.download(url)
            except Exception as e:
                self.log.append("app download failed: %s" % e)
                return                        # stays in safeboot, like the real device
            self.app = "16.0.0(currysmarter)" if b"currysmarter" in data[:4096] or "currysmarter" in url \
                else "16.0.0(release-tasmota32)"
            self.mode = "off"
            time.sleep(2)
            with self.lock:
                self.boot_app()
        threading.Thread(target=run, daemon=True).start()

    def command(self, cmnd):
        c = cmnd.strip()
        low = c.lower()
        if low.startswith("backlog "):
            out = {}
            for part in c[8:].split(";"):
                out.update(self.command(part) or {})
            return out
        if self.mode == "safeboot":
            if low == "restart 3":
                self.reboot("app")
                return {"Restart": "Restarting"}
            return {"Command": "Unknown"}
        m = re.match(r"(\w+?)(\d*)(?:\s+(.*))?$", c)
        name, idx, arg = m.group(1).lower(), m.group(2), m.group(3)
        if low == "status 0":
            return {"Status": {"DeviceName": "Fake Shutter", "Topic": "FakeShutter"},
                    "StatusPRM": {"BootCount": self.boot_count, "SaveCount": 100},
                    "StatusFWR": {"Version": self.app, "Hardware": "ESP8685 v0.4"}}
        if low == "status 2":
            return {"StatusFWR": {"Version": self.app, "Hardware": "ESP8685 v0.4"}}
        if low == "status 10":
            return {"StatusSNS": {"Shutter1": {"Position": 100, "Direction": 0, "Target": 100, "Tilt": 0}}}
        if name == "template":
            t = dict(TEMPLATE)
            if self.app.startswith("16"):
                t = dict(NAME=t["NAME"], GPIO=t["GPIO"], FLAG=0, BASE=1, ARCH="ESP32C3")
            return t
        if name == "tuyamcu":
            return {"TuyaMCU": [{"fnId": 11, "dpId": 4}]}
        if name == "rule" and idx:
            i = int(idx)
            if arg in ("0", "1"):
                self.rules[i][0] = "ON" if arg == "1" else "OFF"
            return {"Rule" + idx: {"State": self.rules[i][0], "Rules": self.rules[i][1]}}
        if name.startswith("shutter"):
            return {cmnd.split()[0]: 20.0}
        if name == "ssid" and not idx:
            return {"SSId1": "Home", "SSId2": self.ssid2}
        if name == "ssid" and idx == "2" and arg == "1":
            self.ssid2 = ""
            self.reboot()
            return {"SSId2": ""}
        if name == "password":
            return {"Password2": "****"}
        if name == "otaurl":
            if arg:
                self.ota_url = arg
            return {"OtaUrl": self.ota_url}
        if low == "upgrade 1":
            self.ota(False)
            return {"Upgrade": "Version %s from %s" % (self.app, self.ota_url)}
        if low == "upgrade 2":
            if not self.app.startswith(("14", "15", "16")):
                return {"Upgrade": "1 or >13.0.0"}
            self.ota(True)
            return {"Upgrade": "Safeboot"}
        if low == "restart 1":
            self.reboot()
            return {"Restart": "Restarting"}
        if name == "curryshutter" and self.driver:
            return {"CurryShutter": {"Version": "1.1.0", "Forwarded": self.forwarded, "Synced": 0,
                                     "MCU": {"Position": 100, "Control": 1, "LED": 0}}}
        if name == "br" and self.app.startswith("16"):
            if arg == "1+1":
                return {"Br": "2"}
            if "compile" in (arg or ""):
                return {"Br": "function" if "/curry_shutter.be" in self.fs else "io_error"}
        if name == "websend":
            target, path = re.match(r"\[([^\]]+)\]\s*(.*)", arg).groups()
            try:
                urllib.request.urlopen("http://%s/cm?cmnd=%s" % (target, urllib.parse.quote(path)), timeout=5).read()
                return {"WebSend": "Done"}
            except Exception:
                return {"WebSend": "Connect failed"}
        return {"Command": "Unknown"}


class Handler(http.server.BaseHTTPRequestHandler):
    shutter = None

    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json", code=200):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self.shutter
        if s.mode == "off":
            self.close_connection = True
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/cm":
            with s.lock:
                self.reply(json.dumps(s.command(q.get("cmnd", [""])[0])))
        elif u.path == "/":
            ver = s.app.replace("(", " (") if s.mode == "app" else "%s (release-safeboot)" % s.safeboot
            self.reply("<html>Tasmota %s by Theo Arends</html>" % ver, "text/html")
        elif u.path == "/in":
            self.reply("Partition safeboot}2832 KB (used %d%%)}1Partition app0*}22880 KB}1Partition fs}2320 KB"
                       % (96 if s.safeboot == "16.0.0" else 90), "text/html")
        elif u.path == "/dl":
            self.reply(b"\x00" * 4096, "application/octet-stream")
        elif u.path == "/ufsd":
            name = q["download"][0]
            data = s.fs.get(name)
            self.reply(data if isinstance(data, bytes) else (data or "").encode(), "application/octet-stream")
        elif u.path == "/state":
            self.reply(json.dumps({"mode": s.mode, "app": s.app, "safeboot": s.safeboot, "ota": s.ota_url,
                                   "rules": s.rules, "ssid2": s.ssid2, "fs": sorted(s.fs), "driver": s.driver,
                                   "boot": s.boot_count, "log": s.log}))
        else:
            self.reply("not found", "text/plain", 404)

    def do_POST(self):
        s = self.shutter
        u = urllib.parse.urlparse(self.path)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if u.path == "/ufsu":
            m = re.search(rb'filename="([^"]+)"\r\n[^\r]*\r\n\r\n', body)
            data = body[m.end():body.rfind(b"\r\n--")]
            s.fs["/" + m.group(1).decode()] = data.decode()
            self.reply("ok", "text/html")
        else:
            self.reply("not found", "text/plain", 404)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int)
    ap.add_argument("--state", choices=["vendor13", "stock16"], default="vendor13")
    a = ap.parse_args()
    Handler.shutter = Shutter(a.state)
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
