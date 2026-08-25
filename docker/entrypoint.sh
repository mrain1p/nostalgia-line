#!/bin/sh
# First-run setup. Everything the user can edit lives under /config so an image
# upgrade never clobbers it.
set -e

CONFIG_DIR=/config
DATA_DIR="$CONFIG_DIR/data"
CONFIG_FILE="${NOSTALGIA_CONFIG:-$CONFIG_DIR/config.yaml}"

mkdir -p "$DATA_DIR" "$CONFIG_DIR/exports" "$CONFIG_DIR/cache"

# Seed the reference data on first run only. A user who edits channels.csv - or
# drops in their own NostalgiaTV export - keeps their copy forever after.
for f in channels.csv network_map.csv orphan_networks.csv channel_catalog.csv; do
  if [ ! -f "$DATA_DIR/$f" ]; then
    cp "/app/data/$f" "$DATA_DIR/$f"
    echo "seeded $DATA_DIR/$f"
  fi
done

if [ ! -f "$CONFIG_FILE" ]; then
  cat > "$CONFIG_FILE" <<YAML
# Nostalgia Line. Nearly everything here is editable in the web UI
# (Settings tab) - you should not need to hand-edit this file.
plex:
  url: "http://192.168.1.245:32400"   # your Plex host IP, NOT localhost
  token: ""
  libraries: []                        # [] = every show library

tmdb:
  api_key: ""
  rate_limit: 50

routing:
  mode: streaming_first
  multi_channel: sanctioned_pairs_only
  orphan_network: parent_fallback

output:
  additions_only: exports/channels_additions.csv
  merged: exports/channels_merged.csv

data:
  channels_csv: data/channels.csv
  network_map: data/network_map.csv
  orphan_networks: data/orphan_networks.csv
  channel_catalog: data/channel_catalog.csv
  cache_dir: cache
  state_file: state.json

server:
  host: 0.0.0.0
  port: 8777
YAML
  echo "created $CONFIG_FILE - set your Plex token and TMDB key in the web UI"
fi

exec "$@"
