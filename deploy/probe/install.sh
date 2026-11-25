#!/usr/bin/env bash
# Install / update the notrufmon probe on Raspberry Pi OS (Bookworm) or Debian/Ubuntu.
#   sudo ./install.sh --server https://notruf.example.net --token <TOKEN> [--ca ca.crt]
# Running it again updates the code and keeps the configuration (a given --token replaces the stored one).
set -euo pipefail

SERVER="" TOKEN="" CA=""
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --token)  TOKEN="$2"; shift 2 ;;
    --ca)     CA="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "please run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/notrufmon"
[ -d "$SRC" ] || SRC="$HERE/../../notrufmon"          # running from the git checkout
[ -f "$SRC/ua.py" ] || { echo "notrufmon package not found next to install.sh"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python >= 3.11 required"; exit 1; }

id notrufmon >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin notrufmon

# code (the probe needs only the core modules and probe/, not the server part)
rm -rf /opt/notrufmon.new
install -d /opt/notrufmon.new/notrufmon/probe
cp "$SRC"/*.py /opt/notrufmon.new/notrufmon/
cp "$SRC"/probe/*.py /opt/notrufmon.new/notrufmon/probe/
rm -rf /opt/notrufmon.old
[ -d /opt/notrufmon ] && mv /opt/notrufmon /opt/notrufmon.old
mv /opt/notrufmon.new /opt/notrufmon
chown -R root:root /opt/notrufmon

install -d -m 0750 -o root -g notrufmon /etc/notrufmon
if [ ! -f /etc/notrufmon/probe.toml ]; then
  [ -n "$SERVER" ] && [ -n "$TOKEN" ] || { echo "first install needs --server and --token"; exit 1; }
  {
    echo "server_url = \"$SERVER\""
    echo "token = \"$TOKEN\""
    if [ -n "$CA" ]; then
      install -m 0640 -o root -g notrufmon "$CA" /etc/notrufmon/ca.crt
      echo 'ca_file = "/etc/notrufmon/ca.crt"'
    fi
  } > /etc/notrufmon/probe.toml
elif [ -n "$TOKEN" ]; then
  sed -i "s|^token = .*|token = \"$TOKEN\"|" /etc/notrufmon/probe.toml
  echo "token in /etc/notrufmon/probe.toml updated"
fi
chown root:notrufmon /etc/notrufmon/probe.toml
chmod 0640 /etc/notrufmon/probe.toml

install -m 0644 "$HERE/notrufmon-probe.service" /etc/systemd/system/notrufmon-probe.service 2>/dev/null \
  || install -m 0644 "$HERE/../probe/notrufmon-probe.service" /etc/systemd/system/notrufmon-probe.service
systemctl daemon-reload
systemctl enable notrufmon-probe >/dev/null
systemctl restart notrufmon-probe
sleep 2
systemctl --no-pager --lines=5 status notrufmon-probe || true
echo
echo "Logs: journalctl -u notrufmon-probe -f"
