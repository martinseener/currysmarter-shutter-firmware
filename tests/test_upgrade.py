#!/usr/bin/env python3
"""test_upgrade.py - runs currysmarter-upgrade.py against simulated shutters (tests/fake_shutter.py)

  python3 tests/test_upgrade.py

No real device is touched; everything runs on 127.0.0.1.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(REPO, "currysmarter-upgrade.py")
failed = 0


def check(name, ok, detail=""):
    global failed
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, "" if ok else "  " + str(detail)))
    failed += 0 if ok else 1


def fake(port, state):
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_shutter.py"), str(port), "--state", state])
    time.sleep(0.7)
    return p


def state(port):
    return json.loads(urllib.request.urlopen("http://127.0.0.1:%d/state" % port).read())


def run(*args):
    cmd = [sys.executable, SCRIPT] + list(args) + ["--server-ip", "127.0.0.1", "--port", "18000", "--yes"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    print("\n".join("    " + l for l in (r.stdout + r.stderr).strip().splitlines()))
    return r.returncode


scenarios = [
    ("vendor 13 -> custom firmware", "vendor13", ["upgrade"], "16.0.0(currysmarter)"),
    ("vendor 13 -> stock firmware + driver upload", "vendor13", ["upgrade", "--firmware", "stock"],
     "16.0.0(release-tasmota32)"),
]
port = 18080
for title, start, args, version in scenarios:
    print("== " + title)
    port += 1
    p = fake(port, start)
    try:
        rc = run(*(args + ["127.0.0.1:%d" % port]))
        s = state(port)
        check("exit code 0", rc == 0, rc)
        check("app is " + version, s["app"] == version, s["app"])
        check("safeboot is 16.0.0", s["safeboot"] == "16.0.0", s["safeboot"])
        check("safeboot fetched as tasmota32c3-safeboot.bin",
              any(l.endswith("/tasmota32c3-safeboot.bin") for l in s["log"]), s["log"])
        check("back in the app", s["mode"] == "app", s["mode"])
        check("driver running", s["driver"])
        check("vendor rules off", all(r[0] == "OFF" for r in s["rules"].values()), s["rules"])
        check("vendor rule text kept", "McuSyn" in s["rules"]["2"][1])
        check("WiFi 2 cleared", s["ssid2"] == "", s["ssid2"])
        check("OtaUrl official", s["ota"].startswith("http://ota.tasmota.com/"), s["ota"])
    finally:
        p.terminate()

print("== stock 16 without driver keeps the vendor rules")
port += 1
p = fake(port, "vendor13")
try:
    rc = run("upgrade", "--firmware", "stock", "--no-driver", "--keep-wifi2", "127.0.0.1:%d" % port)
    s = state(port)
    check("exit code 0", rc == 0, rc)
    check("no driver", not s["driver"])
    check("vendor rules on", all(r[0] == "ON" for r in s["rules"].values()), s["rules"])
    check("WiFi 2 kept", s["ssid2"] == "T-test")
    print("== install-driver later")
    rc = run("install-driver", "127.0.0.1:%d" % port)
    s = state(port)
    check("exit code 0", rc == 0, rc)
    check("driver running", s["driver"])
    check("vendor rules off", all(r[0] == "OFF" for r in s["rules"].values()), s["rules"])
finally:
    p.terminate()

print("== three stock-16 shutters -> custom image in parallel, safeboot kept")
ports = [port + 1, port + 2, port + 3]
port += 3
procs = [fake(p, "stock16") for p in ports]
try:
    t0 = time.time()
    rc = run("upgrade", "--jobs", "3", "--skip-safeboot", *["127.0.0.1:%d" % p for p in ports])
    took = time.time() - t0
    check("exit code 0", rc == 0, rc)
    for p in ports:
        s = state(p)
        check("%d: app is custom" % p, s["app"] == "16.0.0(currysmarter)", s["app"])
        check("%d: driver running" % p, s["driver"])
        check("%d: safeboot not downloaded" % p, not any("safeboot" in l for l in s["log"]), s["log"])
    check("ran in parallel (%.0f s)" % took, took < 60, took)
finally:
    for p in procs:
        p.terminate()

print("== unreachable firmware server is caught before flashing")
port += 1
p = fake(port, "vendor13")
try:
    cmd = [sys.executable, SCRIPT, "upgrade", "127.0.0.1:%d" % port, "--server-ip", "192.0.2.1", "--port", "18000", "--yes"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    s = state(port)
    check("exit code != 0", r.returncode != 0)
    check("reason: cannot reach", "cannot reach" in r.stdout + r.stderr, r.stdout[-300:])
    check("device untouched", s["app"] == "13.0.0(Shutter)" and s["mode"] == "app" and s["boot"] == 10, s)
finally:
    p.terminate()

print("ALL PASSED" if not failed else "%d FAILED" % failed)
sys.exit(1 if failed else 0)
