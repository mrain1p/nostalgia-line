# Nostalgia Line

A companion app for **NostalgiaTV** that puts every show in your Plex or Jellyfin
library on a retro TV channel — and shows you the ones it can't place.

NostalgiaTV ships a default `channels.csv` assigning ~4,200 well-known titles to
channels. Anything else in your library plays nowhere, and there's no way to see
what's missing. Nostalgia Line finds those titles, routes them, and hands you a
merged CSV you can upload straight back.

> **Status: 1.0 beta.** Shows only. Films are implemented but off by default —
> see [Scope](#scope).

---

## How it works

The obvious approach — read the network off Plex metadata — doesn't work. Plex
exposes `studio`, which is the *production company*, not the broadcast network.
Across an 806-series library that produced 503 distinct values, topping out at
"Marvel Studios" with 13. Plex has no network field at all.

So the network comes from TMDB, joined on the id your media server already stores:

```
Plex library
  └─ GET /library/sections/{id}/all
       └─ <Guid id="tmdb://244442"/>        ← join key, already present
            └─ TMDB GET /3/tv/244442
                 └─ networks: [{ name: "HBO" }]
                      └─ NETWORK_MAP["HBO"] → 1068 "H.B.Yo Min"
```

TMDB treats streaming services as networks for TV, so Netflix, Apple TV+, Max and
Prime Video all come back through the same `networks[]` array. One API call
resolves what would otherwise need human knowledge of every show.

### The resolution cascade

First match wins. Every step records *why* it fired.

| # | Rule | Confidence |
|---|------|-----------|
| 0 | Already in your `channels.csv` — left alone | — |
| 1 | **Network match** → `network_map.csv` | high |
| 2 | **Orphan network** → closest sibling (Peacock → N.B.Sea) | medium, flagged |
| 3 | **Content type** from TMDB genres + keywords | medium |
| 4 | **Genre channel** (1097–1110), last resort | low, flagged |
| 5 | **Unassigned** — surfaced in the review queue, never dropped | — |

Step 3 exists because TMDB's TV taxonomy has no Travel genre. Travel shows are
tagged `Documentary` or `Reality`, so genre-based routing silently reports zero
travel content in a library that has *An Idiot Abroad*, both Bourdain series and
*The Grand Tour*. Keywords carry the signal genres lose.

### Why there's a review queue

In testing, 32 of 330 new assignments came back low-confidence — and once checked
against real sources, **22 of those 32 were wrong**. A 69% error rate on the
uncertain tier. The queue is not optional, and low-confidence rows are held back
from export until you look at them.

Real failures the cascade is built to avoid: *The Dark Wizard* reads as fantasy
but is an HBO Max documentary about a BASE jumper. *DTF St. Louis* reads as
reality TV but is an HBO limited series with Jason Bateman. **Never infer a
network from a title.**

---

## Quick start

### Docker (recommended)

The image is published to GitHub Container Registry from the `dev` branch, for
`linux/amd64` and `linux/arm64`. It's public — no login needed.

```bash
curl -O https://raw.githubusercontent.com/mrain1p/nostalgia-line/main/docker-compose.yml
```

```bash
docker compose pull && docker compose up -d
```

> **Tags:** `:dev` is the only published tag today. `main` is the stable branch
> and deliberately does not build, so `:latest` does not exist yet. Pin to a
> commit with `:sha-<short>` if you want to stay put across `dev` pushes.

Then open <http://localhost:8777> and fill in the Settings tab. Everything —
Plex URL, token, TMDB key, routing mode, custom stations — is configured in the
UI. You should never need to hand-edit a YAML file.

Upgrading is `docker compose pull && docker compose up -d`; your `./config`
volume carries everything across.

To build from source instead of pulling:

```bash
docker compose -f docker-compose.build.yml up -d --build
```

Your Plex URL must be the **host IP**, not `localhost` — inside a container
`localhost` is the container itself:

```yaml
plex:
  url: http://192.168.1.245:32400   # ✓
  # url: http://localhost:32400     # ✗ from inside Docker
```

Config, your `channels.csv`, overrides, exports and the TMDB cache all live in
the mounted `./config` volume, so an image upgrade never clobbers them.

To match your host user so the files stay editable, uncomment in
`docker-compose.yml`:

```yaml
user: "1000:1000"
```

### Without Docker

```bash
pip install -r requirements.txt
python run.py
```

---

## Using it

**Settings** → pick your media server (**Plex** or **Jellyfin**), enter its URL and
credential, add a TMDB API key (the same one Kometa uses), then hit
**Test connection**.

**Scan library** → pulls every show, resolves each against TMDB, and diffs
against your existing `channels.csv`. TMDB responses are cached on disk by
`tmdb_id`, so the second scan is much faster.

**Library tab** — the main view. Every title as a row: name, year, episode count,
network, assigned channel, and status — *already assigned by the app* vs
*assigned by Nostalgia Line* vs *unassigned*. Sortable, filterable, searchable.
Click any channel chip to reassign by hand.

**Review tab** — everything low-confidence plus every orphan network, with the
show's overview and a TMDB link so you can judge quickly. Each card also offers
*All from <network>*, which jumps to the library filtered to that network.

**Channels tab** — channel-by-channel counts, flagging channels that are empty or
have 1–3 titles. Watch this: streaming-first routing concentrates content. In
testing, Netflicks alone took 161 of 806 series.

**Networks tab** — the highest-leverage screen. Every TMDB network in your
library, worst-covered first, with how many titles each one accounts for and
where they currently land. See [Fixing a whole network at once](#fixing-a-whole-network-at-once).

**Export…** → shows a preview before writing anything, then produces the files
below.

---

## What you get out

Two CSVs, in exactly the same four-column format NostalgiaTV reads in:

```csv
Channel Number,Channel Name,Title,Release Year
1068,H.B.Yo Min,DTF St. Louis,2026
1006,Cartoon Net,Some New Toon,2024
```

| File | What it is |
|---|---|
| `channels_additions.csv` | **only the new rows.** Small, readable — this is the one to eyeball. |
| `channels_merged.csv` | **your original file plus the additions.** This is the one you upload back to NostalgiaTV. |

Nothing is ever removed or rewritten. If your file had 4,651 rows and Nostalgia
Line adds 300, the merged file has exactly 4,951 — the original 4,651 byte-for-byte
plus 300 new ones. The exporter asserts this before writing and refuses if it
doesn't hold.

The export dialog previews all of it first: how many rows, how many are secondary
channels, how many are being held back for review, and which channels are most
affected — so you can spot one channel swallowing the library before you commit.

Low-confidence rows are **excluded by default**. Tick the box to include them.

In Docker both files land in `./config/exports/`.

---

## Fixing a whole network at once

Assigning titles one at a time is exactly the drudgery the spec calls untenable.
Most of the long tail isn't hundreds of independent decisions — it's a handful of
**networks** nothing maps yet, each dragging dozens of titles with it.

The Networks tab makes that visible. A real example from testing:

```
unmapped   Adult Swim UK    19 titles   → scattered across 2 channels, 19/19 flagged
```

Those 19 titles fell through to content-type and genre rules, landing 14 on
Punchline and 5 on Trip Channel, every one of them needing review. Pick a channel
in that row, click **Map**, re-scan:

```
custom     Adult Swim UK    19 titles   → 1051 Adult Skim, 0 flagged
```

One decision, nineteen titles, review queue emptied. Mappings persist in
`state.json` and layer on top of the shipped `network_map.csv`, so they survive
upgrades and outrank even the country-qualified built-in rows.

For whatever's genuinely one-off, the library view does **bulk assignment**:
filter to what you want, tick the header checkbox (or *Select all matching* to
grab every row across every page), then assign the lot in one go — replacing
their channels or adding a second one.

## Bringing your own lineup

Nostalgia Line ships the stock NostalgiaTV defaults, so it works out of the box
for anyone. If you've customised your channels, upload your own export on the
Settings tab — the sanctioned-pair rules and the already-assigned diff are both
rebuilt from *your* file. The upload is fully validated before anything is
touched, and the file it replaces is backed up alongside it.

(You can also just drop the file in at `config/data/channels.csv` and restart.)

### Custom stations

If you've added your own channels in NostalgiaTV, tell Nostalgia Line what they
are in the vocabulary the cascade already speaks:

> *"Channel 200 'Retro Gaming' should use the lineup for G4."*
> *"Channel 201 'Saturday Mornings' should mirror Boomer-Rang."*

Two kinds of source, freely combined:

- **Source networks** — real TMDB network names (`G4`, `TechTV`, `Toonami`).
  Anything TMDB says aired there routes to your station. This is how a station
  claims a network with no stock analogue.
- **Borrowed channels** — an existing channel number. Your station inherits
  whatever would route there.

And two modes: **claim** takes the title *instead of* the original channel,
**mirror** takes it *as well*.

Custom stations default to channel 1200+ so they can't collide with a future
NostalgiaTV channel.

---

## Routing modes

Measured against 476 titles where independent assignment overlapped the app's
existing defaults:

| Mode | Agreement | |
|------|-----------|---|
| **Streaming-first** — route to the original network/service | **85%** | default |
| Hybrid — content-type channels get first claim | 61% | |
| Themed — route purely by what a show *is* | 33% | |

Streaming-first is the default because it matches the convention already encoded
in `channels.csv`. Themed routing empties the streaming channels entirely; hybrid
produces a file inconsistent with the 4,200 rows already in it.

### Multi-channel assignments

The default file puts 8.7% of titles on more than one channel, and the pairings
are overwhelmingly sibling channels within a family (Boomer-Rang + Cartoon Net,
Animal Globe + National Geography) — not scattergun genre tagging.

So the default `sanctioned_pairs_only` mode only emits a second channel when that
exact pairing already appears in your file. The set is built at load time from
your own `channels.csv`, so it adapts if you change your defaults.

Co-productions are the exception: when TMDB itself lists both networks — *Half
Man* returns BBC One *and* HBO — both are true, and picking a winner loses
information.

---

## Things it tells you that you would not otherwise notice

**Items Plex never matched.** A show with no `tmdb://` guid can never be routed
by anything. That is a Plex-side match problem, not a routing problem, and it is
reported at the top of the library rather than silently swelling the unassigned
count.

**Shows TMDB has no network for.** Rarer, but the same class of silent failure.

**Results going stale.** Changing a routing mode, adding a custom station or
remapping a network means the scan on screen was produced under the old rules. A
bar appears saying so, with a re-scan button, so you never export something that
does not match what you are looking at.

**Channel concentration.** Streaming-first routing piles content up: in testing
Netflicks alone took 20% of the library. The Channels tab and the export preview
both surface this before you commit.

## Data integrity

**Nostalgia Line never modifies or deletes an existing row.** Your assignments
are authoritative. Before writing the merged file, the exporter asserts that the
original row set is a subset of the output and refuses to write if it isn't.

Matching is on `tmdb_id` wherever possible. The stock `channels.csv` carries no
ids, so the diff against it falls back to normalized title + year: trailing
`(YYYY)` stripped (Plex appends these — `Our Planet (2019)`, `Rugrats (2021)`),
leading articles stripped, lowercased, non-alphanumerics removed. Where the same
title appears twice with different years — *Aladdin* the 1992 film and the 1994
series, *The Little Mermaid*, *Bob the Builder* — the cascade refuses to guess
rather than collide.

---

## Scope

**1.0 is shows only.** Film routing (genre + decade + collection; movies have no
`networks` field) is implemented and tested but off by default. Enable it with
`POST /api/scan?include_movies=true` if you want to try it — the UI switch lands
in a later release.

---

## Reference data

| File | What it is |
|---|---|
| `data/channel_catalog.csv` | all 113 channels, 1001–1113, with app keys |
| `data/channels.csv` | the stock NostalgiaTV default assignments |
| `data/network_map.csv` | 183 TMDB network → channel rules |
| `data/orphan_networks.csv` | fallbacks for services with no analogue |

### The 1054 gotcha

NostalgiaTV's internal channel order is *not* a clean offset from its settings
JSON. `LACKLUSTER` sits at 1054, between VHS Channel (1053) and SPARZ (1055) —
but in the settings JSON it appears among the unnumbered entries at the end. Any
code deriving channel numbers from JSON key order will be off by one for every
channel from 1055 up. `channel_catalog.csv` has it right; there's a test pinning
it.

Channels 1072–1088 (Storm Channel, What's On, and 15 Tune music channels) exist
but never receive routed content, and are excluded from every export.

### Network name collisions

Network names aren't unique across countries — TMDB lists both an American `TBS`
and a Japanese `TBS`. Rows in `network_map.csv` may carry an optional
`origin_country`; a qualified row only matches a series from that country, and
the more specific row wins. Without this, anime lands on an American cable
channel.

---

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest
python run.py --reload
```

191 tests covering the catalog, the cascade, custom stations, network mapping and
the rollup, bulk assignment, both media-server adapters and their paging, cache
durability, the export integrity guarantee, and the whole HTTP surface. No network
access required — Plex, Jellyfin and TMDB are faked throughout.

The Jellyfin adapter has **not** been run against a real server; its tests pin the
documented response shapes. If Jellyfin changes, start there.

### Layout

```
nostalgia_line/
  config.py     configuration, env-var overrides
  media.py      MediaItem, guid/ProviderIds normalisation, LibrarySource
  sources.py    picks the configured server
  plex.py       Plex adapter
  jellyfin.py   Jellyfin (and probably Emby) adapter
  channels.py   catalog, defaults, sanctioned pairs, title normalization
  stations.py   custom stations
  tmdb.py       TMDB client, disk cache, rate limiting
  cascade.py    the resolution cascade
  pipeline.py   scan orchestration
  export.py     CSV writers + the additive-only assertion
  store.py      persisted overrides and network mappings
  server.py     FastAPI app
web/            single-page UI, no build step
```

## Media servers, and why not NostalgiaTV

Nostalgia Line reads your library from **Plex** or **Jellyfin**, selected in
Settings. Both expose the TMDB id the cascade joins on — Plex as
`<Guid id="tmdb://1396"/>`, Jellyfin as `ProviderIds: {"Tmdb": "1396"}` — and both
normalise to the same internal item, so nothing downstream knows the difference.
Emby is untested but uses the same API family as Jellyfin, so it may work by
pointing the Jellyfin source at it.

**NostalgiaTV itself is never contacted.** It does maintain its own content index
with the same ids, and reading from it would cover every backend it supports in
one integration. It was considered and rejected: the server API is closed,
undocumented, licensed, and ships on a moving tag, so coupling to it means
breaking whenever it refactors. `channels.csv` is a far more stable contract.

The exchange is therefore file-based, in both directions:

```
NostalgiaTV  --export channels.csv-->  Nostalgia Line
NostalgiaTV  <--import merged csv----  Nostalgia Line
```

One consequence worth knowing: your media server may hold libraries NostalgiaTV
doesn't use. Set the per-library opt-in in Settings to match, or you'll route
content NostalgiaTV can't play.

## Channel artwork

Channels show the real network's logo automatically. Every scan already downloads
each series' `networks[]` from TMDB, which carries a `logo_path`, so the artwork
comes free — no configuration, no extra API calls, and it works on a fresh
install for everyone.

Resolution order for a channel's logo:

1. Artwork you imported, in `/config/logos`
2. Artwork from a read-only mount, if you added one
3. The TMDB logo of a real network that maps to the channel
4. A generated badge — initials and channel number, deterministically coloured

To override with your own art, use **Import artwork** on the Channels tab
(images or a zip), or mount a folder read-only:

```yaml
volumes:
  - "/path/to/your/logos:/logos:ro"
```

Filenames are matched by channel number (`1068.png`), channel name
(`H.B.Yo Min.png`), or **the real network being parodied** (`logo_hbo.png`) —
which is how artwork is usually filed. Unmatched files are reported back rather
than silently ignored.

## Branches

| Branch | What it is |
|---|---|
| `dev` | Where work happens. Every push builds and publishes `ghcr.io/mrain1p/nostalgia-line:dev`. |
| `main` | Stable. Does not build. Merged into deliberately. |

Aim pull requests at `dev`. CI (tests on Python 3.11–3.13, plus a container
build-and-boot check) runs on both branches.

## Acknowledgements

Nostalgia Line is an unofficial companion tool. **NostalgiaTV** is a separate
project — the 113-channel lineup, the parody channel names (LACKLUSTER, H.B.Yo
Min, Munchyroll and the rest) and the default `channels.csv` shipped in `data/`
are its work, not mine. They're bundled so this tool routes correctly out of the
box against a stock install. All credit for that lineup goes to NostalgiaTV; if
you maintain it and would rather this repo not redistribute the file, open an
issue and I'll swap it for a first-run download.

## A note on access

Nostalgia Line has no authentication. It holds your Plex token and TMDB key, and
it writes files. It binds to `127.0.0.1` by default; the Docker image binds
`0.0.0.0` because it has to. Treat it as a trusted-LAN tool — don't expose the
port to the internet or put it behind a public reverse proxy without adding auth
in front of it.

## License

MIT — see [LICENSE](LICENSE).
