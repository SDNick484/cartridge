#!/bin/sh
# What the OpenRC service runs (as user cartridge). Loads /etc/conf.d/cartridge, exporting every
# variable it sets, then replaces itself with the server so supervise-daemon watches the server
# process itself. This is the one place settings are loaded; the init script does not export them.
# With arguments it runs that subcommand instead, with the same settings, so `check` and `sync`
# see what the service sees:
#   su -s /bin/sh cartridge -c /opt/cartridge/run.sh          # serve, in the foreground
#   su -s /bin/sh cartridge -c '/opt/cartridge/run.sh check'
set -eu
CONF="${CARTRIDGE_CONF:-/etc/conf.d/cartridge}"
if [ -r "$CONF" ]; then
	set -a # export every variable the file sets
	# shellcheck disable=SC1090
	. "$CONF"
	set +a
fi
if [ "$#" -gt 0 ]; then
	exec /opt/cartridge/venv/bin/cartridge "$@"
fi
exec /opt/cartridge/venv/bin/cartridge serve
