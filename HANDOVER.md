# Handover — next two work items

Two pieces of work on Nostalgia Line, in the order they should be done. Films are
deliberately **not** in scope; see the last section for why.

Written 2026-08-25 against `dev` at the commit that added station artwork.

---

## Where things are

| | |
|---|---|
| Repo | `github.com/mrain1p/nostalgia-line` — work on `dev`, `main` is released |
| Source of truth | `NOSTALGIA_LINE_SPEC.md` in the repo root |
| Deployed | `ghcr.io/mrain1p/nostalgia-line:dev` on the user's NAS, `/config` at `/volume2/Docker/nostalgia lineup` |
| Tests | `python -m pytest` — 333, none need network access |
| Run locally | `python run.py` |

The routing lives in `nostalgia_line/cascade.py`. Everything else — sources,
export, artwork, the API — hangs off that.

**Before starting, read the "Traps" section at the bottom.** Three separate bugs
in this codebase passed a green test suite and only appeared against real data.

---

## Item 1 — Measure routing accuracy, and fix the genre fallback

### The finding

There are 746 titles the user's `channels.csv` already places. That is free
ground truth: for each one, ask the cascade what it *would* have chosen, and
compare. Measured on the live instance:

```
we agree      679  (91.0%)
we differ      67

by rule:
  network          673/724   93%
  orphan_network     4/4    100%
  content_type       2/9     22%
  genre              0/9      0%
```

**The genre fallback has never once matched.** It is also the rule producing 34
of the 56 items currently sitting in the review queue. Nine samples is not
conclusive on its own, but it agrees with the spec's own measurement of a 69%
error rate on the low-confidence tier (§9), so two independent measurements point
the same way.

### Reproduce it

```bash
ssh <nas> 'docker exec nostalgia-line python -c "
import sys, collections; sys.path.insert(0,\"/app\")
from nostalgia_line.server import state, _network_map_with_overrides
from nostalgia_line.cascade import Cascade, STATUS_APP, STATUS_LINE
from nostalgia_line.channels import load_orphan_networks
from nostalgia_line.tmdb import TMDBCache, TMDBSeries
r = state.result
cache = TMDBCache(state.cfg.path(state.cfg.data.cache_dir))
casc = Cascade(state.catalog, state.defaults, _network_map_with_overrides(),
               load_orphan_networks(state.cfg.path(state.cfg.data.orphan_networks)),
               state.stations)
agree = dis = 0
hits, oks = collections.Counter(), collections.Counter()
for e in r.entries:
    if e.status != STATUS_APP or not e.tmdb_id: continue
    raw = cache.get(\"series\", e.tmdb_id)
    if not raw: continue
    res = casc.resolve_series(\"__probe__\" + e.title, e.year, TMDBSeries.from_dict(raw))
    if res.status != STATUS_LINE or not res.assignments: continue
    rule = res.assignments[0].rule; hits[rule] += 1
    if res.assignments[0].channel_number in e.resolution.existing_channels:
        agree += 1; oks[rule] += 1
    else: dis += 1
print(agree, dis, {k: (oks[k], v) for k, v in hits.items()})
"'
```

The `"__probe__" + e.title` prefix matters: without it the cascade short-circuits
at step 0 and returns the existing answer instead of an independent opinion.

### Build

**1. `nostalgia_line/accuracy.py`** — lift the probe above into a real module.
Takes a `ScanResult`, a `Cascade` and a `TMDBCache`; returns overall agreement,
per-rule agreement with sample counts, and the list of disagreements (title, ours,
theirs) so they can be inspected.

Small samples are the trap here. Report `n` alongside every percentage and do not
render a verdict below roughly 20 samples — `0/9` is a signal, not a proof.

**2. `GET /api/accuracy`** — serve it. Cache per scan; it is a few hundred cascade
runs and should not be recomputed on a poll.

**3. Mode comparison.** Run the same measurement under `streaming_first`, `hybrid`
and `themed` and report all three. The spec quotes 85 / 61 / 33 from a different
library; the point is to show the user their own numbers. This is also how any
future routing change should be justified.

**4. An accuracy view in the UI.** Best home is the Station mapping page or a new
panel on Library. Show overall agreement, a per-rule table, and a sample of
disagreements — those are the interesting rows, because each one is either a bug
in the cascade or a debatable call in the lineup.

**5. Change what the genre rule does.** Do not delete it — the reasoning it
records is useful. Options, in order of preference:

- Leave the title **unassigned** and flag it, rather than placing it. The spec is
  explicit that nothing is ever silently dropped (§3.5), and an unassigned title
  in the review queue is honest, where a wrong placement is not.
- Keep the placement but never export it without an explicit decision, even when
  `include_review` is set.

Whichever is chosen, the review queue should shrink from 56 to roughly 22 and
those 22 are network-shaped, which the grouped review already handles well.

### Done when

- `GET /api/accuracy` returns overall and per-rule agreement with sample counts
- The three routing modes can be compared on the user's own library
- The genre rule no longer contributes rows to an export unreviewed
- A test asserts the probe prefix behaviour — a regression there would silently
  make the measurement report 100% agreement, which is the worst failure mode
  because it looks like good news

---

## Item 2 — The ongoing workflow

### Why

The big import is done: 4,938 rows, round trip proven. What remains is the
*incremental* case, and the app has no answer for it. Shows get added to Plex
every week. Right now the user must remember to scan, then compare 806 rows
against memory to find what changed.

Everything else in this user's stack is automated. This is the piece that turns a
one-shot migration tool into something that keeps earning its place.

### Build

**1. Scheduled scans.** Config under `routing` or a new `schedule` section:
`enabled`, `interval_hours`, optionally a quiet window. Run it from the existing
FastAPI lifespan as an `asyncio` task; do not add a scheduler dependency for one
timer.

Guard rails: never start if a scan is already running (`state.scan_task`), and
survive a failed scan without killing the loop.

**2. Delta tracking.** The persisted scan (`/config/scan.json.gz`) already gives a
previous state to compare against — see `ScanResult.load`. On each scan, diff by
`LibraryEntry.uid` and mark each entry as `new`, `changed` or `unchanged`. A
"changed" entry is one whose resolution moved, which matters after a station
remap.

`uid` is stable across scans and across a Plex/Jellyfin move by design — that is
why the no-guid fallback is `local:` rather than `plex:`. Do not key the diff on
anything else.

**3. Surface it.** A `since_last_scan` filter on `/api/library`, a count in the
provenance strip, and a stat tile. The workflow stepper in `nostalgia_line/workflow.py`
should gain a state for "new titles are waiting", which is the natural next action
once the first import is behind you.

**4. Optional, if it earns its place:** a notification on new titles. The user's
stack is homelab; a webhook is likely enough. Do not build an email pipeline.

### Done when

- A scan can run unattended on an interval, and a failure does not stop the next
- The library can be filtered to what is new since the previous scan
- The stepper points at new titles when there are any
- Restarting the container does not lose the delta — it is derived from the
  persisted scan, so this should follow for free, but test it

---

## Traps

Learned the hard way in this codebase. Each of these passed a green test suite.

**Test against real data before claiming a feature works.** Three bugs shipped
green: an empty TMDB cache field, a logo matcher that worked in tests and not on
real filenames, and a station chooser that picked YTV over Cartoon Network. Every
one was found by running against the user's NAS.

**The TMDB cache invalidates itself now — leave it that way.** `CACHE_SCHEMA` in
`tmdb.py` is a hash of the cached dataclasses' field names. Twice a field was
added without bumping a hand-maintained integer, and every warm cache went on
serving records with the new field blank, which looks exactly like a broken
feature. Do not replace it with a constant.

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

**Say what is known, not what is assumed.** The Apply step in the workflow used to
report "done" as though it had verified the upload. It cannot see inside
NostalgiaTV and now says so. Hold new work to the same line.

---

## Not in scope: films

Film routing exists, is tested, and is off behind a Settings switch. It is
deliberately excluded from this handover.

The reason is item 1. Films have no network to lean on, so they route on genre,
era and collection — and the measurement above shows genre-based routing is the
weakest thing the cascade does. Turning films on would add roughly 3,000 rows
routed largely by the rule with the worst agreement, into a lineup that is
currently clean.

Do item 1 first. Then film accuracy can be measured the same way, against
whatever films the lineup already places, and the decision becomes evidence-based
instead of hopeful.
