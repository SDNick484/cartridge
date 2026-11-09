#!/bin/sh
# Install Cartridge as an OpenRC service on Alpine (e.g. a Proxmox LXC).
#
#   sh deploy/alpine/install.sh SOURCE      # SOURCE: a checkout directory (or a pip spec)
#
# Layout:
#   /opt/cartridge/venv                the code                 root:root         0755
#   /opt/cartridge/run.sh              the service command      root:root         0755
#   /etc/conf.d/cartridge              settings                 root:cartridge    0640
#   /etc/cartridge/devices.yaml        your MiSTers             root:cartridge    0640
#   /var/lib/cartridge/                database, artwork cache  cartridge         0700
#   /var/log/cartridge/cartridge.log   service output           cartridge         0750 (directory)
#   /etc/init.d/cartridge              the OpenRC script
# Re-running upgrades the code and keeps settings, devices and data; then rc-service cartridge restart.
set -eu

SRC="${1:?usage: install.sh SOURCE (a checkout directory or a pip spec)}"
USER_NAME=cartridge
PREFIX=/opt/cartridge
ETC=/etc/cartridge
STATE=/var/lib/cartridge
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# Every compiled dependency ships a musl wheel, so no compiler is needed. Node isn't needed at runtime.
apk add --no-cache python3 py3-pip openrc >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h "$STATE" -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"

# Never overwrite what the owner has edited.
install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/devices.yaml" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/devices.yaml.example" "$ETC/devices.yaml"
[ -e /etc/conf.d/cartridge ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/conf.d.example" /etc/conf.d/cartridge
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/log/cartridge
install -m 0755 "$HERE/cartridge.initd" /etc/init.d/cartridge

echo "Installed Cartridge. If it is already running, upgrade it with: rc-service cartridge restart"
echo "Next:"
echo "  1. Edit $ETC/devices.yaml (your MiSTers) and /etc/conf.d/cartridge."
echo "  2. Check, as the service user:  su -s /bin/sh $USER_NAME -c '$PREFIX/run.sh check'"
echo "  3. rc-update add cartridge default && rc-service cartridge start"
echo "It listens on the LAN_BIND address (default 0.0.0.0:8770). For remote access see the README."
