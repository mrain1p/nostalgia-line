# Handover — accuracy measurement and the ongoing workflow

Both work items from the 2026-08-25 handover are implemented on `dev`. This
file now records what was built, what was verified where, and what the next
session should pick up. Films remain deliberately out of scope; the last
section says why and what has changed about that decision.

---

## Where things are

| | |
|---|---|
| Repo | `github.com/mrain1p/nostalgia-line` — work on `dev`, `main` is released |
| Source of truth | `NOSTALGIA_LINE_SPEC.md` in the repo root |
| Deployed | `ghcr.io/mrain1p/nostalgia-line:dev` on the user's NAS, `/config` at `/volume2/Docker/nostalgia lineup` |
| Tests | `python -m pytest` — 365, none need network access |
| Run locally | `python run.py` |

The routing lives in `nostalgia_line/cascade.py`; the accuracy probe in
`nostalgia_line/accuracy.py`; scan delta and persistence in
`nostalgia_line/pipeline.py`; the scheduler in `nostalgia_line/server.py`
(lifespan task, no scheduler dependency).

**Before touching anything, read the "Traps" section at the bottom.** It has
grown by one entry and every entry was paid for.

---

## Item 1 — accuracy measurement, and the genre rule (done)

**`nostalgia_line/accuracy.py`** probes every show the imported `channels.csv`
already places: resolve it under a `__probe__` title so step 0 cannot answer,
compare the cascade's independent opinion with the lineup, tally per rule.
`GET /api/accuracy` serves it — computed once per (scan, routing inputs),
cached on `AppState`, all three routing modes measured so they can be compared
on the user's own library. Sample counts ride along everywhere;
`MIN_SAMPLES = 20` gates every verdict ("too few to judge", never a bare
percentage).

The **probe prefix is regression-tested** in `tests/test_accuracy.py::
test_without_the_prefix_the_measurement_collapses`: without the prefix every
probe short-circuits to the lineup's own answer, the sample count silently
drops to zero, and the measurement reads as perfect agreement. That test is
the one that must never be deleted.

**The genre rule no longer places anything.** Measured 0/9 on the live
instance (and 69% wrong on the low tier per spec §9), it now records an
`Assignment` in the new `Resolution.suggestion` field and returns the title
**unassigned + flagged** (spec §3.4 updated). Consequences:

- Genre-routed titles cannot reach an export by any path, including
  `include_review` — they are not placements.
- The review queue shows them with the suggestion one click from being applied
  as a *manual override* (the honest way for that channel to win).
- The probe still scores the retired rule's suggestions separately
  (`suggestions` block in `/api/accuracy`) — that running number is the
  evidence the films decision needs.
- `ScanResult.STATE_VERSION` bumped to 2: an old persisted scan predates the
  rule change and is discarded on upgrade rather than shown. **First start
  after deploying this build shows "no scan yet" until a scan runs.**

The UI lives on the **Station mapping tab**: overall agreement, per-rule
table, mode comparison, and the disagreement list (each row is either a
cascade bug or a debatable lineup call — click through to the title). The
Settings mode dropdown re-labels itself with the measured numbers once they
exist; until then it shows the spec's 85/61/33 marked as spec figures.

### Verified

- 365 tests green, including the probe-prefix regression, per-rule tallies,
  disagreement shape, the mode comparison, and `/api/accuracy` caching.
- A live local run against the real cascade + real `channels.csv` (4,651
  rows) with 700 seeded ground-truth titles: panel showed 92.9% on n=687,
  genre suggestions 0/13 "too few to judge", suggestion click became a manual
  override and left the queue.
- **Not yet verified on the NAS.** The NAS was unreachable from the dev
  machine when this shipped (ping dead). After deploying: open Station
  mapping, confirm `/api/accuracy` roughly reproduces the handover's 93%
  network / 0-for-genre shape on the real library, and confirm the review
  queue is ~22 network-shaped items plus genre items now showing as
  unassigned-with-suggestion.

## Item 2 — the ongoing workflow (done)

**Scheduled scans.** `schedule:` config section (`enabled`, `interval_hours`,
optional `quiet_start`/`quiet_end` local hours, wrap-midnight, both-or-neither
validated). A single asyncio task started from the FastAPI lifespan checks
once a minute; the decision ladder (`_schedule_decision`: disabled /
unconfigured / scan_running / not_due / quiet / scan) is a plain function with
tests. A scheduled scan runs the exact same worker as the Scan button, so
failure reporting, cancellation, logo refresh and persistence are one code
path; a failed scan sets `last_error` and does not stop the timer.

**Delta tracking.** After every scan, `apply_delta(new, previous)` marks each
entry `new` / `changed` / `unchanged` **keyed on `LibraryEntry.uid` only**,
records departed titles, and stamps `previous_scan_at`. It is persisted inside
`scan.json.gz`, so a container restart keeps it (tested by save/load
round-trip). "Changed" means the resolution moved — status or channel set —
which is what a station remap looks like. The first-ever scan marks nothing:
806 "new" titles on an initial import would be noise.

**Surfaced as:** `since_last_scan=true` filter on `/api/library`; `new`/
`moved` pills on library rows; a clickable "N new, M moved, K gone since last
scan" chip in the provenance strip; a "New since scan" stat tile; a
`schedule` block in `/api/status` ("Auto-scan in 23h" chip, quiet-window
aware); and the workflow stepper names the arrivals on whichever step they
re-opened ("The last scan added or moved 3 title(s).").

A webhook on new titles was considered and skipped: the delta is fully
surfaced in-app and no receiver exists to point one at. If wanted later, hang
it off the same worker-completion point that calls `apply_delta`.

### Verified

- Delta unit tests (uid-keyed, rename-immune, departed, first-scan-silent,
  restart round-trip), scheduler decision ladder, quiet-window wrap, settings
  round-trip (and: saving the schedule does **not** mark results stale —
  only routing changes do now), full scan-worker integration test with a
  monkeypatched `run_scan`.
- Live local run: scheduler enabled with quiet 23–07 showed the provenance
  chip, the tile filtered to 14 new + 1 moved, pills rendered.
- **Not yet verified on the NAS:** an unattended scan actually firing on
  interval against real Plex. Enable it in Settings after deploying and check
  the next morning.

---

## Also in this change

- Fixed a dead button: the grouped review view's per-item dismiss was never
  wired (only the flat list was). It works now, labelled "Leave as is" for
  unassigned items because "Looks right" would have claimed a placement that
  does not exist.
- `POST /api/settings` only marks results stale when a routing input actually
  changed. Saving a playlist URL or the scan schedule no longer claims the
  scan is out of date.
- `config.example.yaml` documents the `schedule:` section; README covers the
  accuracy view, scheduled scans, and the genre rule change; spec §3/§8
  updated to match reality.

## Deploy checklist

1. Push `dev` → CI runs 365 tests → `ghcr.io/mrain1p/nostalgia-line:dev`.
2. Merge to `main` and push → `:latest`.
3. On the NAS: pull, restart. Expect "no scan yet" (STATE_VERSION bump) — run
   a scan; the TMDB cache is warm so it is quick.
4. Verify the three NAS items above (accuracy numbers, review queue shape,
   one scheduled scan).

---

## Traps

Learned the hard way in this codebase. Each of these passed a green test suite.

**Test against real data before claiming a feature works.** Three bugs shipped
green: an empty TMDB cache field, a logo matcher that worked in tests and not on
real filenames, and a station chooser that picked YTV over Cartoon Network. Every
one was found by running against the user's NAS.

**The accuracy probe must never lose its prefix.** `resolve_series` answers from
the lineup for any title the lineup contains — which is every ground-truth title
by construction. Probe under `accuracy.PROBE_PREFIX` or the measurement compares
the lineup with itself and reports 100% agreement, which looks exactly like good
news. `test_without_the_prefix_the_measurement_collapses` pins it.

**The TMDB cache invalidates itself now — leave it that way.** `CACHE_SCHEMA` in
`tmdb.py` is a hash of the cached dataclasses' field names. Twice a field was
added without bumping a hand-maintained integer, and every warm cache went on
serving records with the new field blank, which looks exactly like a broken
feature. Do not replace it with a constant. (The same idea now guards the
persisted scan: `ScanResult.STATE_VERSION` — bump it when the on-disk shape
changes.)

**The export must stay byte-identical.** Exporting with zero additions reproduces
the user's file byte-for-byte. Two things earn that: 37 rows use `Various` as a
release year rather than a number, and their file is plain UTF-8 with bare LF
where `csv.writer` defaults to CRLF. `tests/test_export.py` pins both. If a test
there fails, the export is wrong, not the test.

**Never modify an existing row.** The integrity assertion compares every written
field, not a dedupe key — the key folds an unparseable year to `""` and so missed
37 rows being quietly rewritten. Keep it comparing `DefaultRow.exact()`.

**Do not couple to NostalgiaTV's API.** It is closed, compiled, licensed and on a
moving tag. The M3U playlist and `channels.csv` are interop formats meant to be
read by other software; depend only on those.

**Say what is known, not what is assumed.** The Apply step in the workflow
reports what came back, not what it hopes happened inside NostalgiaTV. The
accuracy panel measures agreement with the lineup, not truth. The scheduler
reports "due", not "will run at". Hold new work to the same line.

---

## Not in scope: films

Film routing exists, is tested, and is off behind a Settings switch. Still
deliberately excluded — but the decision now has a yardstick it lacked before.

Films route on genre, era and collection, and genre is the rule the
measurement retired for shows. What changed: `/api/accuracy` continuously
scores the retired genre rule's *suggestions* against the user's lineup. When
film work starts, measure film accuracy the same way — against whatever films
the lineup already places — before a single film row reaches an export. The
machinery (`accuracy.measure`) already takes any entries; it filters to shows
today precisely because a movie tmdb_id can collide with an unrelated series
id in the cache, so extend it with a `resolve_movie` probe path rather than
un-filtering.

---

Written 2026-08-25 against `dev`. The canonical copy is `HANDOVER.md` in the
repo, versioned alongside the code it describes — if the two disagree, the
repo wins.
