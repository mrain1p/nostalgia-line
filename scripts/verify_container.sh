#!/bin/sh
# Verify a built image actually boots, seeds /config, and honours PUID/PGID.
#
# This is the only place the container is exercised - there is no Docker on the
# development machine - so both CI and the publish pipeline run it.
set -eu

IMAGE=${1:-nostalgia-line:ci}
PORT=${2:-8779}
WANT_UID=1000
WANT_GID=10
NAME="nl-verify-$$"
DIR=$(mktemp -d)

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "running $IMAGE with PUID=$WANT_UID PGID=$WANT_GID"
docker run -d --name "$NAME" -p "$PORT:8777" \
  -e PUID="$WANT_UID" -e PGID="$WANT_GID" -e TZ=America/New_York \
  -v "$DIR:/config" "$IMAGE" >/dev/null

i=0
while [ "$i" -lt 40 ]; do
  if curl -sf "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then break; fi
  i=$((i + 1))
  sleep 1
done
if ! curl -sf "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  echo "FAIL: never became healthy"
  docker logs "$NAME"
  exit 1
fi
echo "  API responded after ${i}s"
curl -s "http://127.0.0.1:$PORT/api/status" | head -c 200
echo

# PID 1 is the app. `docker exec` would report the image's USER (root) instead,
# which says nothing about who the server is actually running as.
uid=$(docker exec "$NAME" awk '/^Uid:/ {print $2}' /proc/1/status)
gid=$(docker exec "$NAME" awk '/^Gid:/ {print $2}' /proc/1/status)
echo "  PID 1 runs as ${uid}:${gid}"
[ "$uid" = "$WANT_UID" ] || { echo "FAIL: expected uid $WANT_UID"; docker logs "$NAME"; exit 1; }
[ "$gid" = "$WANT_GID" ] || { echo "FAIL: expected gid $WANT_GID"; docker logs "$NAME"; exit 1; }

# And the files it seeded must be editable from the host.
ls -ln "$DIR" | head -6
fuid=$(stat -c %u "$DIR/config.yaml")
fgid=$(stat -c %g "$DIR/config.yaml")
echo "  config.yaml owned by ${fuid}:${fgid}"
[ "$fuid" = "$WANT_UID" ] || { echo "FAIL: config.yaml not owned by PUID"; exit 1; }
[ "$fgid" = "$WANT_GID" ] || { echo "FAIL: config.yaml not owned by PGID"; exit 1; }

test -f "$DIR/data/channels.csv" || { echo "FAIL: reference data was not seeded"; exit 1; }
echo "  reference data seeded"
echo "container verified"
