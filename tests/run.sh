#!/bin/bash
# run.sh - all tests. The Berry tests need a native Berry interpreter; it is
# built from the Tasmota checkout in .build/ (build/build.sh creates it) if
# 'berry' is not on the PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
BERRY=$(command -v berry || true)
if [ -z "$BERRY" ]; then
  B=.build/tasmota/lib/libesp32/berry
  [ -d "$B" ] || git clone -q --depth 1 --branch v16.0.0 https://github.com/arendst/Tasmota.git .build/tasmota
  [ -x "$B/berry" ] || make -s -C "$B" >/dev/null
  BERRY=$B/berry
fi
"$BERRY" tests/test_driver.be
python3 tests/test_upgrade.py
