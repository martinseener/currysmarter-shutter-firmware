#!/bin/bash
# build.sh - builds firmware/tasmota32c3-currysmarter.bin
#
# Stock Tasmota (same tag as the stock binaries in firmware/) plus
# xdrv_124_curry_installer.ino, which carries berry/*.be and writes them to the
# file system on first boot. Needs git, python3 and ~3 GB for PlatformIO.
#   build/build.sh [workdir]
set -euo pipefail
TASMOTA_TAG=v16.0.0
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
WORK=${1:-$REPO/.build}
mkdir -p "$WORK"

if [ ! -d "$WORK/tasmota" ]; then
  git clone --depth 1 --branch "$TASMOTA_TAG" https://github.com/arendst/Tasmota.git "$WORK/tasmota"
fi
if [ ! -x "$WORK/venv/bin/pio" ]; then
  python3 -m venv "$WORK/venv"
  "$WORK/venv/bin/pip" install -q platformio
fi

T=$WORK/tasmota
cp "$HERE/xdrv_124_curry_installer.ino" "$T/tasmota/tasmota_xdrv_driver/"
cp "$HERE/platformio_override.ini" "$T/"
DRIVER_VERSION=$(python3 "$HERE/embed_berry.py" "$T/tasmota/include/curry_berry_files.h")
echo "embedded Berry driver $DRIVER_VERSION"

(cd "$T" && "$WORK/venv/bin/pio" run -e tasmota32c3-currysmarter)
cp "$T/build_output/firmware/tasmota32c3-currysmarter.bin" "$REPO/firmware/"

# record the new image in the manifest the upgrade script checks
python3 - "$REPO/firmware" "$DRIVER_VERSION" <<'PY'
import hashlib, json, sys
fw, driver = sys.argv[1], sys.argv[2]
m = json.load(open(fw + "/manifest.json"))
name = m["app"]["custom"]
m["files"][name]["sha256"] = hashlib.sha256(open(fw + "/" + name, "rb").read()).hexdigest()
m["berry_driver"] = driver
with open(fw + "/manifest.json", "w") as f:
    json.dump(m, f, indent=2)
    f.write("\n")
print("%s %s, driver %s" % (m["files"][name]["sha256"], name, driver))
PY
