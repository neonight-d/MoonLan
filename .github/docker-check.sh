#!/usr/bin/env bash
# The image, run the way an operator runs it (v0.8.3): the demo with
# one command, the API and the page through the published port, the
# console through docker exec, ping as the image's own user, a stop and
# a start, the example docker-compose.yml with a real database, and a
# start without config.yaml. Run by .github/workflows/docker.yml on the
# image it has just built; nothing is published from here.
#
#   .github/docker-check.sh <image> <expected build>
set -euo pipefail
IMAGE=${1:?usage: docker-check.sh <image> <expected build>}
BUILD=${2:-}
WORK=$(mktemp -d)

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAILED: %s\n' "$*"; exit 1; }

# the health check runs every 30 s: up to three minutes for the first
wait_healthy() {
  local name=$1 state=""
  for _ in $(seq 1 90); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$name")
    if [ "$state" = healthy ]; then echo "$name: healthy"; return 0; fi
    sleep 2
  done
  docker logs "$name" | tail -40
  fail "$name is $state, not healthy"
}

step "The demo, with one command"
docker run -d --name demo -p 8080:8080 -e MOONLAN_DEMO=1 "$IMAGE"
wait_healthy demo
docker ps --filter name=demo --format '{{.Names}}: {{.Status}}'
[ "$(docker exec demo id -u)" = 1000 ] || fail "the service does not run as uid 1000"
curl -fsS http://127.0.0.1:8080/api/health; echo
curl -fsSI http://127.0.0.1:8080/ | grep -i "^content-security-policy: default-src 'self'" \
  || fail "no Content-Security-Policy on the page"
curl -fsS -o /dev/null http://127.0.0.1:8080/vendor/vis-network-9.1.9/vis-network.min.js \
  || fail "vis-network is not served by MoonLan"
curl -fsS http://127.0.0.1:8080/api/topology | python3 -c '
import json, sys
topology = json.load(sys.stdin)
assert topology["switches"], "the demo has no switches"
print(len(topology["switches"]), "switches,", len(topology["hosts"]), "devices")'
curl -fsS http://127.0.0.1:8080/api/about | python3 -c '
import json, sys
about = json.load(sys.stdin)
print("version", about["version"], "build", about["build"])
assert about["build"] == sys.argv[1], "the build is not the commit the image was made from"' "$BUILD"

step "Ping and traceroute as the moonlan user"
GATEWAY=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')
docker exec demo ping -c 1 -W 2 127.0.0.1 >/dev/null || fail "ping to the loopback"
docker exec demo ping -c 1 -W 2 "$GATEWAY" || fail "ping out of the container to $GATEWAY"
docker exec demo traceroute -m 2 -w 2 "$GATEWAY" || fail "traceroute out of the container"

step "The console through docker exec"
printf 'ci-password-0123\nci-password-0123\n' \
  | docker exec -i demo python -m moonlan.users add ci-admin --role admin
docker exec demo python -m moonlan.users list | grep ci-admin || fail "users list"
# the IEEE register comes from IEEE's server, which turns some
# networks away: whether it is fetched is IEEE's; that the command
# runs is ours
docker exec demo python -m moonlan.oui update \
  || echo "(IEEE did not hand the register over to this runner)"
timeout 180 docker exec demo python -m moonlan.diag --stp >"$WORK/diag.txt" 2>&1 || true
head -20 "$WORK/diag.txt"
if grep -q Traceback "$WORK/diag.txt"; then fail "diag --stp raised"; fi

step "docker stop: SIGTERM, a clean exit"
docker stop demo
[ "$(docker inspect -f '{{.State.ExitCode}}' demo)" = 0 ] || fail "the service did not exit cleanly"
docker logs demo 2>&1 | grep -E "Shutting down|Finished server process" || fail "no orderly shutdown in the log"
docker start demo
wait_healthy demo
docker rm -f demo >/dev/null

step "Without config.yaml"
docker run -d --name bare "$IMAGE"
sleep 8
docker logs bare 2>&1 | grep "No config.yaml" | grep "/config/config.yaml" | grep "/app/config.example.yaml" \
  || fail "the log does not say where config.yaml goes"
docker rm -f bare >/dev/null

step "The example docker-compose.yml, with a database of its own"
mkdir -p "$WORK/compose/config" "$WORK/compose/data"
cp config.example.yaml "$WORK/compose/config/config.yaml"
sed "s|image: .*|image: $IMAGE|" docs/deploy/docker-compose.yml >"$WORK/compose/docker-compose.yml"
# the container's user is 1000:1000, the runner's is not: as DOCKER.md says
sudo chown -R 1000:1000 "$WORK/compose/data"
(cd "$WORK/compose" && docker compose up -d)
wait_healthy moonlan
docker exec moonlan ls -l /data
docker exec moonlan test -f /data/moonlan.db || fail "no database in /data"
(cd "$WORK/compose" && docker compose stop && docker compose start)
wait_healthy moonlan
if docker logs moonlan 2>&1 | grep -iE "wal|database disk image|malformed|locked"; then
  fail "the database complained after a stop and a start"
fi
(cd "$WORK/compose" && docker compose down)

step "All of it passed"
