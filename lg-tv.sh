#!/usr/bin/env bash
# One-command LG TV connection for macOS (also works on Linux).
#   bash ~/Downloads/lg-tv.sh
set -uo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

echo "== preparing (one time, ~20 seconds) =="
[ -d venv ] || python3 -m venv venv || { echo "Could not create the virtual environment."; exit 1; }
./venv/bin/pip install --quiet --disable-pip-version-check websockets || {
  echo "Could not install the 'websockets' package. Are you online?"; exit 1; }
echo "   ready."

echo
echo "== 1) looking for an LG TV on this network =="
./venv/bin/python lgtv_connect.py --scan || true

echo
printf "Enter the TV IP from above (or press Enter to stop): "
read -r IP
if [ -z "${IP:-}" ]; then echo "stopped."; exit 0; fi

echo
echo "== 2) connecting - a prompt will appear on the TV screen: choose Allow =="
echo "   (on macOS, if nothing appears, allow Local Network access for 'python' in"
echo "    System Settings > Privacy & Security > Local Network, then run this again)"
./venv/bin/python lgtv_connect.py --host "$IP"
RC=$?

echo
if [ $RC -eq 0 ]; then
  echo "Connected. A file named lgtv-report.txt was created next to this script."
  echo "Send that file back and you will get the exact cleanup plan for your model."
else
  echo "Connection did not complete (exit $RC). Read the message above."
fi
exit $RC
