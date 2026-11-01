#!/usr/bin/env python3
"""currysmarter-upgrade.py - upgrade Currysmarter EU-CL02 roller shutters

ESP32-C3 + Tuya MCU, shipped with vendor Tasmota 13/14. Upgrades app and
safeboot to Tasmota 16 and installs the Berry driver that replaces the vendor
rules. Needs Python 3.8+ only, no packages.

The shutter downloads its firmware itself, so this script runs a small web
server. Your computer must be reachable from the shutter on --port (TCP).

  python3 currysmarter-upgrade.py check   <host> [<host> ...]   read-only
  python3 currysmarter-upgrade.py upgrade <host> [<host> ...]   [--firmware custom|stock] [--no-driver] [--jobs N]
  python3 currysmarter-upgrade.py install-driver <host> ...     driver only (Tasmota >= 14 with Berry)
  python3 currysmarter-upgrade.py calibrate <host> [--apply]    measure travel times (moves the shutter!)
  python3 currysmarter-upgrade.py rescue <host>                 leave safeboot (Restart 3)
  python3 currysmarter-upgrade.py serve                         only serve firmware/ for manual OtaUrl use

https://github.com/martinseener/currysmarter-shutter-firmware
"""
import argparse
import base64
import concurrent.futures
import datetime
import hashlib
import http.client
import http.server
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.join(HERE, "firmware")
BERRY_DIR = os.path.join(HERE, "berry")
BACKUP_DIR = os.path.join(HERE, "backups")
BERRY_FILES = ["curry_shutter.be", "autoexec.be"]
OFFICIAL_OTA_URL = "http://ota.tasmota.com/tasmota32/release/tasmota32c3.bin"
# Upgrade 2 turns the OtaUrl file name into "<name up to the last dash>-safeboot.bin"
# and cuts it at the first dot, so this name must not contain a version.
SAFEBOOT_OTA_NAME = "tasmota32c3.bin"
TUYA_TX, TUYA_RX = 2272, 2304             # template GPIO components of the Tuya serial port
VENDOR_RULE_MARK = "McuSyn"               # only found in the vendor Rule2


class UpgradeError(Exception):
    pass


def host_ip(host):
    """IP address of 'host' or 'host:port'."""
    return socket.gethostbyname(host.rsplit(":", 1)[0] if host.count(":") == 1 else host)


def log(host, msg):
    print("%s %s%s" % (time.strftime("%H:%M:%S"), "[%s] " % host if host else "", msg), flush=True)


# --------------------------------------------------------------------------------------------
# firmware files and the web server the shutter downloads from

def load_manifest():
    with open(os.path.join(FIRMWARE_DIR, "manifest.json")) as f:
        manifest = json.load(f)
    for name, meta in manifest["files"].items():
        path = os.path.join(FIRMWARE_DIR, name)
        if not os.path.exists(path):
            raise UpgradeError("missing firmware/%s" % name)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != meta["sha256"]:
            raise UpgradeError("firmware/%s: sha256 %s does not match manifest.json" % (name, digest))
    return manifest


class Downloads:
    """What each shutter fetched from our server, for progress output and checks."""
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []                  # (time, client ip, path, bytes sent, total, done)
        self.pings = {}

    def add(self, *event):
        with self.lock:
            self.events.append((time.time(),) + event)

    def since(self, client, t0):
        with self.lock:
            return [e for e in self.events if e[1] == client and e[0] >= t0]


class FirmwareHandler(http.server.BaseHTTPRequestHandler):
    files = {}                            # served name -> local path
    downloads = Downloads()

    def log_message(self, fmt, *args):    # silence the default access log
        pass

    def do_GET(self):
        client = self.client_address[0]
        path = urllib.parse.urlparse(self.path).path.lstrip("/")
        if path == "cm":                  # reachability test, see Server.test_from()
            self.downloads.pings[client] = time.time()
            self._send(200, b'{"curry":"ok"}', "application/json")
            return
        local = self.files.get(path)
        if local is None:
            self.downloads.add(client, path, 0, 0, False)
            self._send(404, b"not found", "text/plain")
            return
        with open(local, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        sent = 0
        try:
            while sent < len(data):
                chunk = data[sent:sent + 16384]
                self.wfile.write(chunk)
                sent += len(chunk)
                if sent % (256 * 1024) < 16384:
                    self.downloads.add(client, path, sent, len(data), False)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.downloads.add(client, path, sent, len(data), sent == len(data))

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server:
    def __init__(self, port, server_ip, manifest):
        files = {name: os.path.join(FIRMWARE_DIR, name) for name in manifest["files"]}
        FirmwareHandler.files = files
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), FirmwareHandler)
        self.port = port
        self.server_ip = server_ip
        self.downloads = FirmwareHandler.downloads
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def ip_for(self, host):
        if self.server_ip:
            return self.server_ip
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host_ip(host), 80))   # no packet is sent, only picks the route
            return s.getsockname()[0]
        finally:
            s.close()

    def url(self, host, name):
        return "http://%s:%d/%s" % (self.ip_for(host), self.port, name)

    def test_from(self, dev):
        """Let the shutter call us (WebSend). This is the connection the safeboot needs."""
        ip = self.ip_for(dev.host)
        t0 = time.time()
        r = dev.cmd("WebSend [%s:%d] curry_ping" % (ip, self.port), timeout=15)
        for _ in range(10):
            if self.downloads.pings.get(host_ip(dev.host), 0) >= t0:
                return True
            time.sleep(0.5)
        if "Unknown" in json.dumps(r):
            return None                   # WebSend not compiled in, cannot test
        return False


def watch_download(server, dev, t0, seen):
    """Print download progress of dev since t0; returns True once a download completed."""
    done = False
    for e in server.downloads.since(host_ip(dev.host), t0):
        if e in seen:
            continue
        seen.add(e)
        _, _, path, sent, total, complete = e
        if total == 0:
            log(dev.host, "shutter asked for /%s, which is not served (404)" % path)
        elif complete:
            log(dev.host, "download of %s complete (%d KB)" % (path, total // 1024))
            done = True
        else:
            log(dev.host, "downloading %s %d%%" % (path, 100 * sent // total))
    return done


# --------------------------------------------------------------------------------------------
# talking to a shutter (Tasmota web API)

class Device:
    def __init__(self, host, password=None):
        self.host = host
        self.password = password

    def _open(self, path, data=None, headers=None, timeout=10):
        req = urllib.request.Request("http://%s%s" % (self.host, path), data=data, headers=headers or {})
        if self.password:
            token = base64.b64encode(("admin:%s" % self.password).encode()).decode()
            req.add_header("Authorization", "Basic " + token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def get(self, path, timeout=10, retries=2):
        for attempt in range(retries + 1):
            try:
                return self._open(path, timeout=timeout)
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                if attempt == retries:
                    raise
                time.sleep(1)

    def cmd(self, command, timeout=10):
        q = {"cmnd": command}
        if self.password:
            q.update(user="admin", password=self.password)
        raw = self.get("/cm?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote), timeout=timeout)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise UpgradeError("unexpected answer to %r: %r" % (command, raw[:200]))

    def try_cmd(self, command, timeout=5):
        try:
            return self.cmd(command, timeout=timeout)
        except Exception:
            return None

    def state(self):
        """'app', 'safeboot' or None (not reachable)."""
        r = self.try_cmd("Status 2", timeout=4)
        if r and "StatusFWR" in r:
            return "safeboot" if "safeboot" in r["StatusFWR"]["Version"] else "app"
        try:
            page = self.get("/", timeout=4, retries=0).decode("utf-8", "replace")
        except Exception:
            return None
        return "safeboot" if "safeboot" in page else "app"

    def safeboot_version(self):
        try:
            page = self.get("/", timeout=4, retries=0).decode("utf-8", "replace")
        except Exception:
            return None
        m = re.search(r"Tasmota\s+([\d.]+)\s*\(([^)]*safeboot[^)]*)\)", page)
        return m.group(1) if m else None

    def version(self):
        return self.cmd("Status 2")["StatusFWR"]["Version"]

    def shutter(self):
        r = self.try_cmd("Status 10")
        try:
            return r["StatusSNS"]["Shutter1"]
        except (TypeError, KeyError):
            return None

    def is_idle(self):
        s = self.shutter()
        return s is None or int(s.get("Direction", 0)) == 0

    def driver(self):
        r = self.try_cmd("CurryShutter")
        return r.get("CurryShutter") if r else None

    def rules(self):
        out = {}
        for i in (1, 2, 3):
            r = self.cmd("Rule%d" % i)["Rule%d" % i]
            out[i] = (r.get("State"), r.get("Rules", ""))
        return out

    def vendor_rules_on(self):
        rules = self.rules()
        return VENDOR_RULE_MARK in rules[2][1] and any(state == "ON" for state, _ in rules.values())

    def template_gpio(self):
        t = self.cmd("Template")
        return t.get("GPIO") if isinstance(t, dict) else None

    def info_page(self):
        return self.get("/in", timeout=15).decode("utf-8", "replace")

    def backup(self, path):
        data = self.get("/dl", timeout=20)
        if len(data) < 1000:
            raise UpgradeError("config backup too small (%d bytes)" % len(data))
        with open(path, "wb") as f:
            f.write(data)

    def upload(self, local, name):
        with open(local, "rb") as f:
            data = f.read()
        boundary = "----curry" + uuid.uuid4().hex
        body = (("--%s\r\nContent-Disposition: form-data; name=\"ufsu\"; filename=\"%s\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n") % (boundary, name)).encode() + data + \
               ("\r\n--%s--\r\n" % boundary).encode()
        self._open("/ufsu?fsz=%d" % len(data), data=body, timeout=30,
                   headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        back = self.get("/ufsd?download=/" + urllib.parse.quote(name), timeout=15)
        if back != data:
            raise UpgradeError("upload of %s did not verify (%d of %d bytes)" % (name, len(back), len(data)))


def wait_for(timeout, test, interval=3):
    end = time.time() + timeout
    while True:
        try:
            if test():
                return True
        except Exception:
            pass
        if time.time() > end:
            return False
        time.sleep(interval)


def wait_idle(dev, timeout=60):
    if not wait_for(timeout, dev.is_idle):
        raise UpgradeError("shutter still moving after %d s" % timeout)


# --------------------------------------------------------------------------------------------
# check

def check(dev, manifest, server=None, quiet=False):
    """Read-only. Returns (facts, problems, warnings)."""
    facts, problems, warnings = {}, [], []
    st = dev.try_cmd("Status 0")
    if not st or "StatusFWR" not in st:
        state = dev.state()
        if state == "safeboot":
            problems.append("device is in safeboot mode (use: rescue %s)" % dev.host)
        elif state is None:
            problems.append("not reachable (or WebPassword set: use --password)")
        else:
            problems.append("no Tasmota JSON answer on /cm (WebPassword set? use --password)")
        return facts, problems, warnings

    fwr, prm = st["StatusFWR"], st["StatusPRM"] if "StatusPRM" in st else {}
    facts["name"] = st["Status"].get("DeviceName", "")
    facts["topic"] = st["Status"].get("Topic", "")
    facts["version"] = fwr["Version"]
    facts["hardware"] = fwr.get("Hardware", "")
    facts["boot_count"] = prm.get("BootCount")
    facts["save_count"] = prm.get("SaveCount")
    if not facts["hardware"].startswith(("ESP32-C3", "ESP8685")):    # ESP8685 = C3 in a small package
        problems.append("hardware is %r, expected ESP32-C3" % facts["hardware"])

    gpio = dev.template_gpio()
    facts["tuya_template"] = bool(gpio) and TUYA_TX in gpio and TUYA_RX in gpio
    if not facts["tuya_template"]:
        problems.append("template has no TuyaMCU Tx/Rx - is this a Currysmarter shutter?")
    facts["tuyamcu"] = json.dumps(dev.cmd("TuyaMCU").get("TuyaMCU"))

    info = dev.info_page()
    parts = dict(re.findall(r"Partition (\w+)\*?\}2(\d+) KB", info))
    facts["partitions"] = parts
    if "safeboot" not in parts:
        problems.append("no safeboot partition (%s) - this tool needs the safeboot layout" % parts)
    m = re.search(r"Partition safeboot\}2\d+ KB \(used (\d+)%\)", info)
    facts["safeboot_used"] = int(m.group(1)) if m else None

    shutter = dev.shutter()
    facts["shutter"] = shutter
    if shutter is None:
        warnings.append("no Shutter1 configured")
    elif int(shutter.get("Direction", 0)) != 0:
        problems.append("shutter is moving")
    for key in ("ShutterOpenDuration1", "ShutterCloseDuration1"):
        r = dev.try_cmd(key) or {}
        facts[key] = r.get(key)

    rules = dev.rules()
    facts["vendor_rules"] = VENDOR_RULE_MARK in rules[2][1]
    facts["rules_state"] = "/".join(state for state, _ in rules.values())
    drv = dev.driver()
    facts["driver"] = drv.get("Version", "1.0") if drv else None
    if facts["vendor_rules"] and drv and "ON" in facts["rules_state"]:
        warnings.append("vendor rules AND Berry driver are active")

    ssid = dev.try_cmd("SSId") or {}
    facts["ssid2"] = ssid.get("SSId2")
    if facts["ssid2"] == "T-test":
        warnings.append('factory backup WiFi "T-test"/"88888888" configured (upgrade clears it)')
    if not dev.password:                  # /cm answered without credentials
        warnings.append("no WebPassword set: anyone in the network can reconfigure or flash it")

    if server:
        ok = server.test_from(dev)
        facts["reaches_us"] = ok
        if ok is False:
            problems.append("the shutter cannot reach http://%s:%d - open TCP %d on this computer/firewall "
                            "(also between VLANs)" % (server.ip_for(dev.host), server.port, server.port))
        elif ok is None:
            warnings.append("could not test whether the shutter reaches this computer (no WebSend)")

    if not quiet:
        log(dev.host, "%s (%s) %s" % (facts["name"], facts["topic"], facts["version"]))
        log(dev.host, "  partitions %s, safeboot used %s%%" % (
            ", ".join("%s %s KB" % kv for kv in parts.items()), facts["safeboot_used"]))
        log(dev.host, "  TuyaMCU %s, shutter %s, durations open %s / close %s s" % (
            facts["tuyamcu"], shutter and "%s%%" % shutter.get("Position"),
            facts["ShutterOpenDuration1"], facts["ShutterCloseDuration1"]))
        log(dev.host, "  rules %s%s, Berry driver %s, BootCount %s, SaveCount %s" % (
            facts["rules_state"], " (vendor rule text present)" if facts["vendor_rules"] else "",
            facts["driver"] or "not running", facts["boot_count"], facts["save_count"]))
        if server and facts.get("reaches_us"):
            log(dev.host, "  can reach this computer on port %d: yes" % server.port)
        for w in warnings:
            log(dev.host, "  warning: " + w)
        for p in problems:
            log(dev.host, "  PROBLEM: " + p)
    return facts, problems, warnings


# --------------------------------------------------------------------------------------------
# upgrade steps

def target_for(manifest, firmware):
    name = manifest["app"][firmware]
    return name, manifest["files"][name]


def matches(version, meta):
    return version.startswith(meta["version"] + "(") and meta["image"] in version


def upgrade_app(dev, server, manifest, firmware):
    name, meta = target_for(manifest, firmware)
    version = dev.version()
    if matches(version, meta):
        log(dev.host, "app is already %s, skipped" % version)
        return
    url = server.url(dev.host, name)
    if url not in json.dumps(dev.cmd("OtaUrl " + url)):
        raise UpgradeError("OtaUrl not accepted")
    wait_idle(dev)
    log(dev.host, "Upgrade 1: %s -> %s" % (version, url))
    t0, seen, downloaded = time.time(), set(), False
    dev.cmd("Upgrade 1")
    end = t0 + 480
    while time.time() < end:
        time.sleep(3)
        downloaded |= watch_download(server, dev, t0, seen)
        r = dev.try_cmd("Status 2", timeout=4)
        if r and "StatusFWR" in r and matches(r["StatusFWR"]["Version"], meta):
            log(dev.host, "running %s" % r["StatusFWR"]["Version"])
            return
        if not downloaded and not seen and time.time() - t0 > 150:
            raise UpgradeError("the shutter did not download anything from %s:%d after 150 s. "
                               "It may be waiting in safeboot: check the firewall, then run "
                               "'rescue %s' or upgrade by file upload in its web UI"
                               % (server.ip_for(dev.host), server.port, dev.host))
    raise UpgradeError("app not on %s after 8 min (state: %s)" % (meta["version"], dev.state()))


def upgrade_safeboot(dev, server, manifest):
    meta = manifest["files"][manifest["safeboot"]]
    url = server.url(dev.host, SAFEBOOT_OTA_NAME)
    if url not in json.dumps(dev.cmd("OtaUrl " + url)):
        raise UpgradeError("OtaUrl not accepted")
    wait_idle(dev)
    log(dev.host, "Upgrade 2: safeboot from %s" % url.replace(SAFEBOOT_OTA_NAME, manifest["safeboot"]))
    t0, seen = time.time(), set()
    if "Safeboot" not in json.dumps(dev.cmd("Upgrade 2")):
        raise UpgradeError("Upgrade 2 not accepted (firmware without safeboot OTA support?)")
    end = t0 + 300
    while time.time() < end:
        time.sleep(3)
        watch_download(server, dev, t0, seen)
        if dev.safeboot_version() == meta["version"]:
            break
    else:
        raise UpgradeError("no safeboot %s after 5 min (state: %s)" % (meta["version"], dev.state()))
    log(dev.host, "safeboot %s running, Restart 3 (back to the app)" % meta["version"])
    time.sleep(3)
    dev.try_cmd("Restart 3")
    if not wait_for(240, lambda: dev.state() == "app" and dev.try_cmd("Status 2") is not None):
        raise UpgradeError("not back in the app after Restart 3")
    log(dev.host, "back in %s" % dev.version())


def install_driver(dev, manifest):
    """Stock path: upload berry/*.be, switch the vendor rules off, restart."""
    if (dev.try_cmd("Br 1+1") or {}).get("Br") != "2":
        raise UpgradeError("no Berry on this firmware (needs Tasmota ESP32 >= 14)")
    for name in BERRY_FILES:
        dev.upload(os.path.join(BERRY_DIR, name), name)
    r = dev.cmd('Br var f=open("/curry_shutter.be") var c=compile(f.read()) f.close() return type(c)')
    if r.get("Br") != "function":
        raise UpgradeError("driver does not compile on the device: %s" % r)
    log(dev.host, "driver files uploaded and verified")
    wait_idle(dev)
    if dev.vendor_rules_on():
        dev.cmd("Backlog Rule1 0; Rule2 0; Rule3 0")
        time.sleep(2)
        log(dev.host, "vendor rules switched off (text kept for backout)")
    dev.cmd("Restart 1")
    time.sleep(5)


def verify_driver(dev, timeout=180):
    if not wait_for(timeout, lambda: dev.driver() is not None):
        raise UpgradeError("Berry driver not running (see the console of the web UI)")
    time.sleep(6)
    drv = dev.driver()
    if drv.get("Forwarded") != 0:
        raise UpgradeError("driver sent a target at boot: %s" % drv)
    if dev.vendor_rules_on():
        raise UpgradeError("vendor rules still active next to the driver")
    log(dev.host, "Berry driver %s running, MCU position %s, LED %s" % (
        drv.get("Version", "1.0"), drv["MCU"].get("Position"), drv["MCU"].get("LED")))


def clear_wifi2(dev):
    if (dev.try_cmd("SSId") or {}).get("SSId2") != "T-test":
        return
    wait_idle(dev)
    log(dev.host, 'clearing factory WiFi 2 "T-test" (restarts once)')
    dev.cmd("Backlog SSId2 1; Password2 1")
    time.sleep(10)
    if not wait_for(120, lambda: (dev.try_cmd("SSId") or {}).get("SSId2") == ""):
        raise UpgradeError("WiFi 2 still set")


def upgrade(dev, server, manifest, args):
    facts, problems, warnings = check(dev, manifest, server)
    if problems:
        if not (args.force and all("moving" not in p and "reach" not in p for p in problems)):
            raise UpgradeError("check failed: " + "; ".join(problems))
        log(dev.host, "--force: continuing despite: " + "; ".join(problems))

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, "%s_%s_%s.dmp" % (
        facts["topic"] or dev.host, facts["version"].split("(")[0], datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
    dev.backup(backup)
    log(dev.host, "config backup: %s (contains your WiFi/MQTT passwords)" % os.path.relpath(backup))
    gpio_before = dev.template_gpio()

    upgrade_app(dev, server, manifest, args.firmware)
    if dev.template_gpio() != gpio_before:
        raise UpgradeError("template changed during the upgrade: %s" % dev.template_gpio())
    if not args.skip_safeboot:
        upgrade_safeboot(dev, server, manifest)
    ota = args.ota_url or OFFICIAL_OTA_URL
    dev.cmd("OtaUrl " + ota)
    log(dev.host, "OtaUrl set to %s" % ota)

    if args.firmware == "custom":
        verify_driver(dev)             # the firmware installed it on first boot
    elif not args.no_driver:
        install_driver(dev, manifest)
        verify_driver(dev)
    if not args.keep_wifi2:
        clear_wifi2(dev)
    after, _, _ = check(dev, manifest, quiet=True)
    log(dev.host, "done: %s, rules %s, driver %s, BootCount %s, SaveCount %s" % (
        after["version"], after["rules_state"], after["driver"], after["boot_count"], after["save_count"]))


# --------------------------------------------------------------------------------------------
# calibrate: measure full travel times from the MCU position (needs the driver)

def sample_run(dev, command, start, end, timeout=120):
    """Send command, sample the MCU position until it sits at 'end'. Returns [(t, pos)]."""
    samples = [(time.time(), start)]
    dev.cmd(command)
    t_end = time.time() + timeout
    while time.time() < t_end:
        t = time.time()
        drv = dev.driver()
        if drv and drv["MCU"].get("Position") is not None:
            samples.append((t, int(drv["MCU"]["Position"])))
            if samples[-1][1] == end and len(samples) > 3 and all(p == end for _, p in samples[-8:]):
                return samples
        time.sleep(max(0, 0.2 - (time.time() - t)))
    raise UpgradeError("shutter did not reach %d%% within %d s" % (end, timeout))


def travel_time(samples, start, end):
    """Average of 'reach' (left start -> arrived) and the 10..90 % speed, in s."""
    i = max(k for k, (_, p) in enumerate(samples) if p == start and k + 1 < len(samples) and samples[k + 1][1] != start)
    j = next(k for k in range(i, len(samples)) if samples[k][1] == end)
    reach = samples[j][0] - samples[i][0]
    mid = [s for s in samples[i:j + 1] if 10 <= s[1] <= 90]
    if len(mid) > 2 and mid[-1][0] > mid[0][0] and mid[-1][1] != mid[0][1]:
        full = 100 * (mid[-1][0] - mid[0][0]) / abs(mid[-1][1] - mid[0][1])
        return round((reach + full) / 2, 1), round(reach, 1), round(full, 1)
    return round(reach, 1), round(reach, 1), None


def calibrate(dev, args):
    drv = dev.driver()
    if not drv:
        raise UpgradeError("calibrate needs the Berry driver (CurryShutter) running")
    pos = drv["MCU"].get("Position")
    if pos is None:
        raise UpgradeError("MCU position unknown")
    if pos != 100:
        log(dev.host, "opening fully first")
        sample_run(dev, "TuyaSend4 1,0", pos, 100)
        time.sleep(3)
    log(dev.host, "closing (TuyaSend4 1,2) ...")
    down = travel_time(sample_run(dev, "TuyaSend4 1,2", 100, 0), 100, 0)
    time.sleep(3)
    log(dev.host, "opening (TuyaSend4 1,0) ...")
    up = travel_time(sample_run(dev, "TuyaSend4 1,0", 0, 100), 0, 100)
    log(dev.host, "close: %.1f s (reach %.1f, speed %s)   open: %.1f s (reach %.1f, speed %s)" % (
        down[0], down[1], down[2], up[0], up[1], up[2]))
    cur = (dev.try_cmd("ShutterCloseDuration1") or {}).get("ShutterCloseDuration1"), \
          (dev.try_cmd("ShutterOpenDuration1") or {}).get("ShutterOpenDuration1")
    log(dev.host, "current settings: close %s s, open %s s" % cur)
    if args.apply:
        time.sleep(4)
        wait_idle(dev)
        dev.cmd("Backlog ShutterCloseDuration1 %.1f; ShutterOpenDuration1 %.1f" % (down[0], up[0]))
        log(dev.host, "set ShutterCloseDuration1 %.1f, ShutterOpenDuration1 %.1f" % (down[0], up[0]))
    else:
        log(dev.host, "to apply: Backlog ShutterCloseDuration1 %.1f; ShutterOpenDuration1 %.1f  (or use --apply)"
            % (down[0], up[0]))


# --------------------------------------------------------------------------------------------

def confirm(text, args):
    if args.yes:
        return
    print(text)
    if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("aborted")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 2)[2])
    p.add_argument("command", choices=["check", "upgrade", "install-driver", "calibrate", "rescue", "serve"])
    p.add_argument("hosts", nargs="*", help="IP address or host name of each shutter")
    p.add_argument("--firmware", choices=["custom", "stock"], default="custom",
                   help="custom = Tasmota 16 + Berry driver built in (default); stock = official Tasmota 16")
    p.add_argument("--no-driver", action="store_true", help="with --firmware stock: keep the vendor rules, no driver")
    p.add_argument("--skip-safeboot", action="store_true", help="do not upgrade the safeboot partition")
    p.add_argument("--keep-wifi2", action="store_true", help='keep the factory WiFi 2 "T-test"')
    p.add_argument("--ota-url", help="OtaUrl to leave on the device (default: official Tasmota)")
    p.add_argument("--port", type=int, default=8000, help="web server port (default 8000)")
    p.add_argument("--server-ip", help="this computer's IP as seen by the shutters (default: auto)")
    p.add_argument("--password", help="WebPassword of the shutters, if set")
    p.add_argument("--apply", action="store_true", help="calibrate: write the measured durations")
    p.add_argument("--force", action="store_true", help="continue despite failed checks (not: moving / unreachable)")
    p.add_argument("--jobs", type=int, default=1, help="check/upgrade/install-driver this many hosts at once (default 1)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    args = p.parse_args()
    if args.command != "serve" and not args.hosts:
        p.error("no host given")

    manifest = load_manifest()
    needs_server = args.command in ("check", "upgrade", "serve")
    server = Server(args.port, args.server_ip, manifest) if needs_server else None

    if args.command == "serve":
        ip = args.server_ip or "<this computer's IP>"
        print("Serving firmware/ on port %d. On the shutter (console):" % args.port)
        print("  app:      OtaUrl http://%s:%d/%s  then  Upgrade 1" % (ip, args.port, manifest["app"]["custom"]))
        print("            (stock: %s)" % manifest["app"]["stock"])
        print("  safeboot: OtaUrl http://%s:%d/%s  then  Upgrade 2, and Restart 3 in safeboot"
              % (ip, args.port, SAFEBOOT_OTA_NAME))
        print("Ctrl-C to stop.")
        seen = set()
        try:
            while True:
                time.sleep(1)
                for e in server.downloads.events[:]:
                    if e not in seen:
                        seen.add(e)
                        log(e[1], "%s %s/%s bytes%s" % (e[2], e[3], e[4], " done" if e[5] else ""))
        except KeyboardInterrupt:
            return

    devices = [Device(h, args.password) for h in args.hosts]
    if args.command == "upgrade":
        app = manifest["app"][args.firmware]
        what = ["app -> %s (%s)" % (app, manifest["files"][app]["version"])]
        if not args.skip_safeboot:
            what.append("safeboot -> %s" % manifest["safeboot"])
        if args.firmware == "custom":
            what.append("Berry driver installed by the firmware, vendor rules switched off")
        elif not args.no_driver:
            what.append("Berry driver uploaded, vendor rules switched off")
        if not args.keep_wifi2:
            what.append('factory WiFi 2 "T-test" cleared')
        confirm("Will upgrade %s:\n  - %s\nThe shutters restart several times (about 5-8 min each). "
                "Do not power them off meanwhile." % (", ".join(args.hosts), "\n  - ".join(what)), args)
    elif args.command == "install-driver":
        confirm("Will upload the Berry driver to %s, switch the vendor rules off and restart."
                % ", ".join(args.hosts), args)
    elif args.command == "calibrate":
        confirm("Will CLOSE and OPEN %s fully to measure the travel times. Make sure nothing is in the way."
                % ", ".join(args.hosts), args)

    stop = threading.Event()             # an upgrade failed: start no further hosts

    def run_one(dev):
        """Returns True on success."""
        if stop.is_set():
            log(dev.host, "skipped, another host failed")
            return False
        try:
            if args.command == "check":
                return not check(dev, manifest, server)[1]
            elif args.command == "upgrade":
                upgrade(dev, server, manifest, args)
            elif args.command == "install-driver":
                install_driver(dev, manifest)
                verify_driver(dev)
            elif args.command == "calibrate":
                calibrate(dev, args)
            elif args.command == "rescue":
                if dev.state() != "safeboot":
                    log(dev.host, "not in safeboot (state: %s), nothing to do" % dev.state())
                    return True
                log(dev.host, "in safeboot, Restart 3 (boot the app partition)")
                dev.try_cmd("Restart 3")
                if not wait_for(120, lambda: dev.state() == "app"):
                    raise UpgradeError("still not in the app - upgrade by file upload in the safeboot web UI")
                log(dev.host, "app running: %s" % dev.version())
            return True
        except UpgradeError as e:
            log(dev.host, "FAILED: %s" % e)
            if args.command in ("upgrade", "install-driver", "calibrate"):
                stop.set()
                log(None, "starting no further hosts")
            return False

    jobs = args.jobs if args.command in ("check", "upgrade", "install-driver") else 1
    try:
        if jobs > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                results = list(pool.map(run_one, devices))
        else:
            results = [run_one(dev) for dev in devices]
    except KeyboardInterrupt:
        log(None, "interrupted - check the shutters' state with: check <host>")
        os._exit(130)
    failed = [dev.host for dev, ok in zip(devices, results) if not ok]
    if failed:
        sys.exit("failed: " + ", ".join(failed))


if __name__ == "__main__":
    try:
        main()
    except UpgradeError as e:
        sys.exit("error: %s" % e)
