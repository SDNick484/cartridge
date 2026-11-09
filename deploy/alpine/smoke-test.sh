#!/bin/sh
# CI: install and run the service in a stock python:3.12-alpine container, against two simulated
# MiSTers. Proves the dependencies install on musl, the install layout and permissions, that
# OpenRC starts and stops the service, that conf.d settings reach the process, and that the LAN
# app serves while the remote app refuses requests without an Access assertion.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

# On failure, say where as a GitHub annotation, with the end of the service log.
report() {
	status=$?
	if [ "$status" != 0 ]; then
		tail_log=$(tail -n 30 "${STEP_LOG:-/var/log/cartridge/cartridge.log}" 2>/dev/null | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
		echo "::error title=Alpine smoke test failed (exit $status)::last step: ${STEP:-?}%0A$tail_log"
	fi
}
trap report EXIT

STEP='install'
apk add --no-cache openrc curl >/dev/null
sh deploy/alpine/install.sh /src

STEP='pytest on musl'
# Playwright has no musl wheel, so the browser smoke tests (tests/e2e, the e2e CI job) stay out.
/opt/cartridge/venv/bin/pip install --quiet pytest anyio
STEP_LOG=/tmp/pytest.log
cd /src
/opt/cartridge/venv/bin/pytest -q -p no:cacheprovider -rf --ignore=tests/e2e >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
tail -n 3 "$STEP_LOG"
cd /
STEP_LOG=''

STATE=/var/lib/cartridge
BIN=/opt/cartridge/venv/bin/cartridge
STEP='simulator'
# Two simulated MiSTers, started as the service user; their devices file becomes the service's.
su -s /bin/sh cartridge -c "$BIN simulate --fakes-only --dir $STATE/sim" &
for _ in $(seq 100); do [ -s "$STATE/sim/devices.yaml" ] && break; sleep 0.1; done
install -m 0640 -o root -g cartridge "$STATE/sim/devices.yaml" /etc/cartridge/devices.yaml

STEP='service start'
# The remote app is on, with Access settings that nothing here can satisfy: it must start and refuse.
cat >/etc/conf.d/cartridge <<'CONF'
REMOTE_ENABLED=1
CF_ACCESS_TEAM_DOMAIN=example.cloudflareaccess.com
CF_ACCESS_AUD=smoke
LAN_BIND=127.0.0.1:8770
CONF
chown root:cartridge /etc/conf.d/cartridge
chmod 0640 /etc/conf.d/cartridge
# OpenRC in a container: it needs to believe it has booted, and that the network (which the
# service `need`s) is provided by the host.
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
rc-service cartridge start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8770/healthz && break; sleep 0.2; done
rc-service cartridge status

STEP='lan app'
curl -fsS http://127.0.0.1:8770/healthz
# The first sync runs in the background; wait for games to appear.
for _ in $(seq 100); do
	games=$(curl -fsS http://127.0.0.1:8770/api/games) && echo "$games" | grep -q '"games":\[{' && break
	sleep 0.2
done
echo "$games" | head -c 300
echo "$games" | grep -q '"games":\[{'

STEP='remote app'
curl -fsS http://127.0.0.1:8771/healthz
code=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8771/api/games)
[ "$code" = 403 ]

STEP='service user'
# shellcheck disable=SC2009
pid=$(ps -o pid,args | grep '[c]artridge serve' | awk 'NR==1 {print $1}')
[ -n "$pid" ] && [ "$(stat -c %U "/proc/$pid")" = cartridge ]
STEP='file modes'
[ "$(stat -c '%a %U' $STATE)" = "700 cartridge" ]
[ "$(stat -c '%a %U:%G' /etc/conf.d/cartridge)" = "640 root:cartridge" ]
[ "$(stat -c '%a %U:%G' /etc/cartridge/devices.yaml)" = "640 root:cartridge" ]
[ "$(stat -c '%a' /opt/cartridge/run.sh)" = 755 ]

STEP='service stop'
rc-service cartridge stop
cat /var/log/cartridge/cartridge.log
echo "Alpine smoke test passed"
