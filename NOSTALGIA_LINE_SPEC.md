# Nostalgia Line — Build Specification

A companion app for **NostalgiaTV** that automatically assigns every title in a Plex
library to a retro TV channel, and surfaces anything it can't place.

This spec is derived from a real run against a 4,830-title Plex library (806 series,
4,024 films) mapped against NostalgiaTV's default `channels.csv`. Every number and
edge case below was measured, not assumed.

---

## 1. The problem

NostalgiaTV ships a default `channels.csv` assigning ~4,200 well-known titles to
channels. Anything in a user's library that isn't on that list plays nowhere. Users
have no way to see *what's missing*, and hand-assigning hundreds of titles is
untenable.

The naive fix — read the network off Plex metadata — **does not work**:

- Plex exposes `studio`, which is the **production company**, not the broadcast network.
  Across 806 series this produced 503 distinct values, topping out at "Marvel Studios"
  with 13. Useless for channel routing.
- Plex has **no network field at all**. Verified against `/library/metadata/{key}`:
  the returned attributes are `studio`, `genre`, `country`, `rating`, `role` — no network.
- Plex's TV genre tags come from TMDB's TV taxonomy, which has **no Travel genre**.
  Travel shows are tagged `Documentary` or `Reality`, so genre-based routing silently
  reports zero travel content in a library that has *An Idiot Abroad*, both Bourdain
  series, *Somebody Feed Phil*, and *The Grand Tour*.

**The network must come from TMDB.**

---

## 2. Core pipeline

```
Plex library
  └─ GET /library/sections/{id}/all
       └─ <Guid id="tmdb://244442"/>        ← join key, already present
            └─ TMDB GET /3/tv/244442
                 └─ networks: [{ name: "HBO" }]
                      └─ NETWORK_MAP["HBO"] → 1068 "H.B.Yo Min"
```

Plex stores TMDB, IMDb and TVDB ids on every item. **Join on `tmdb_id`, never on title
strings** (see §7).

TMDB treats streaming services as networks for TV, so Netflix, Apple TV+, HBO Max and
Prime Video all return through the same `networks[]` array. One API call resolves what
would otherwise require human knowledge of every show.

### Endpoints

| Purpose | Endpoint | Key fields |
|---|---|---|
| Series | `GET /3/tv/{id}` | `networks[]`, `genres[]`, `first_air_date`, `origin_country` |
| Film | `GET /3/movie/{id}` | `genres[]`, `release_date`, `original_language`, `production_companies[]` |
| Keywords | `GET /3/tv/{id}/keywords` | `results[]` — needed for travel/food/true-crime detection |

**Movies have no `networks` field.** Film routing is genre + decade + collection only.

---

## 3. Resolution cascade

Apply in order. First match wins.

### For series

1. **Network match** — `networks[0].name` → `NETWORK_MAP` → channel.
   Resolves ~95% of a typical library.
2. **Orphan-network fallback** — network exists but has no channel analogue.
   Map to closest real-world sibling (see §5).
3. **Content-type rules** — from `genres[]` + `keywords[]`:
   | Signal | Channel |
   |---|---|
   | travel, road trip, culinary travel | 1059 Trip Channel |
   | cooking, food competition | 1012 Meal Network |
   | true crime, murder, investigation | 1046 TruthTV / 1048 A&Me |
   | nature, wildlife | 1033 Animal Globe / 1035 National Geography |
   | history, war documentary | 1017 Story Channel |
   | horror | 1037 Terror Channel |
   | science fiction | 1036 Sigh-Fi |
   | adult animation | 1051 Adult Skim |
   | anime | 1071 Munchyroll |
   | sports documentary | 1061 YESPN |
4. **Genre channel** — last resort (§4, 1097–1110).
5. **Unassigned** — surface in the review queue. Never silently drop.

### For films

1. **Collection match** — TMDB/Plex collection containing "Oscar" → 1113 Oscar Hits.
2. **Distinctive genre** — Western → 1100, Horror → 1037, Musical → 1109, War → 1103,
   Documentary → 1032, Music → music channel, Sport → 1061.
3. **Era split** — pre-1950 → 1050 TCN. Pre-2000 horror → 1053 VHS Channel (keeps
   the modern horror channel from swallowing the entire film library; without this
   split Terror Channel took 430 titles in testing).
4. **Foreign + acclaimed** → 1112 Benchmark Hits. **Exclude anime** or it pulls in
   *Demon Slayer* alongside world cinema.
5. **Decade channel** by `release_date` → 1089–1096.

---

## 4. Channel model

113 channels, numbered **1001–1113**. Names are deliberate parodies and must be used
verbatim — several are non-obvious:

| Range | Contents |
|---|---|
| 1001–1011 | Kids (Dizzy Channel/Junior/XD, Toon Dizzy, Playhome Dizzy, Cartoon Net, Boomer-Rang, Pennyodeon, Penny Jr., P.B.Yes Tots, P.B.Yes) |
| 1012–1053 | Cable and broadcast (Meal Network, TV World, EF-X, EF-XX, eon Television, Story Channel, N.B.Sea, A.B.Sea, NTV, SeaW, C.B.Yes, FAUX, H.G.T.Vee, A.M.Sea, B.B.Sea, T.N.Tea, T.L.Sea, Bravio, BETV, Trademark Channel, Uncover Channel, Animal Globe, YouTV, National Geography, Sigh-Fi, Terror Channel, Comedy Middle, M.G.N., Watch-On-Repeat, Nap @ Nite, Faux Kids, Kids' DuB, TeeBS, Spoke, TruthTV, US Yay, A&Me, GuessSN, TCN, Adult Skim, Holiday Channel, VHS Channel) |
| 1054 | LACKLUSTER |
| 1055–1071 | Premium and streaming (SPARZ, CINEMIN, Feeform, Lifetune, Trip Channel, Sea.N.N., YESPN, C.B.Sea, Dizzy+, Netflicks, Pear TV+, Hula, Paramountain+, H.B.Yo Min, Primary Video, Viewtime, Munchyroll) |
| 1072–1088 | Storm Channel, What's On, 15 Tune music channels — **no default content** |
| 1089–1096 | The 1950's … The 2020's |
| 1097–1110 | Genre channels — **Adrenaline** (action), **Punchline** (comedy), **Spotlight** (drama), **The Frontier** (western), **Family Room** (family), **Case Closed** (crime), **Battleground** (war), **Event Horizon** (sci-fi), **Heartstrings** (romance), **Cliffhanger** (thriller), **Spellbound** (fantasy), **Uncharted** (adventure), **Showstopper** (musical), **ComicVerse** (superhero) |
| 1111–1113 | TeleMondo (Spanish), Benchmark Hits (acclaimed), Oscar Hits |

**Numbering gotcha:** the app's internal order is not a clean offset from the settings
JSON. `LACKLUSTER` sits at 1054, between VHS Channel (1053) and SPARZ (1055), while in
the settings JSON it appears among the unnumbered entries at the end. Any code deriving
channel numbers from the JSON key order must special-case this or every channel from
1055 up will be off by one.

---

## 5. Orphan networks

Services with no channel analogue. Encountered in 5 of a 32-title sample — this is not
an edge case, it's routine. Ship an explicit table:

| Network | Route to | Rationale |
|---|---|---|
| Peacock | 1018 N.B.Sea | NBCUniversal parent |
| Shudder | 1037 Terror Channel | horror-only service |
| Investigation Discovery | 1048 A&Me | crime/investigation format |
| Smithsonian Channel | 1035 National Geography | science/nature format |
| CuriosityStream | 1035 National Geography | science/nature format |
| YouTube Premium / Red | 1038 Comedy Middle | by content type |

Default behavior for an unlisted orphan: fall through to content-type rules and **flag
for review**. Do not silently route to a generic genre channel — that's how
*This Is a Gardening Show* (a Zach Galifianakis Netflix comedy) ends up on HGTV.

---

## 6. Multi-channel assignment

NostalgiaTV supports a title on multiple channels, and the default file uses it — but
**sparingly and with a clear convention**:

- 368 of 4,220 titles (**8.7%**) appear on more than one channel
- 325 of those on exactly 2; only 42 on 3+
- 226 distinct channel pairings are sanctioned by the default file

The pairings are overwhelmingly **sibling channels within a family**, not scattergun
genre tagging:

| Pairing | Count |
|---|---|
| Boomer-Rang + Cartoon Net | 56 |
| Animal Globe + National Geography | 44 |
| LACKLUSTER + Oscar Hits | 19 |
| LACKLUSTER + Toon Dizzy | 18 |
| TV World + YouTV | 15 |
| ComicVerse + LACKLUSTER | 13 |
| N.B.Sea + Watch-On-Repeat | 10 |

**Recommended rule:** only emit a second channel when the pair already appears in the
default file. Build the sanctioned-pair set at load time and gate every secondary
assignment through it. Applied to 330 new titles this yielded 45 secondary rows (13%) —
in line with the app's own restraint.

**Co-productions are the honest multi-channel case.** *Half Man* returns both BBC One
and HBO from TMDB. Emit both rather than picking a winner.

---

## 7. Data integrity rules

**Never modify existing rows.** The user's default assignments are authoritative.
Nostalgia Line is strictly additive. Verify on write that the original row set is a
subset of the output.

**Match on `tmdb_id`, not title.** The default file contains 21 cases of the same title
appearing twice on one channel — *Aladdin* the 1992 film and the 1994 series, *The
Little Mermaid*, *Bob the Builder*. Title-string matching will collide or dedupe
incorrectly.

**Normalize when you must fall back to titles.** Plex appends disambiguating years
(`Our Planet (2019)`, `Rugrats (2021)`, `Cosmos (2014)`). Strip trailing `(YYYY)`,
strip leading articles, lowercase, strip non-alphanumerics.

**CSV format** — exactly four columns, header required:

```csv
Channel Number,Channel Name,Title,Release Year
1068,H.B.Yo Min,DTF St. Louis,2026
```

Emit both an additions-only file and a merged full file. The merged file is what the
user uploads; the additions file is what they review.

---

## 8. Routing defaults

Measured against 476 titles where independent assignment overlapped the app's existing
defaults:

| Strategy | Agreement |
|---|---|
| **Streaming-first** (route to original network/service) | **85%** |
| Hybrid (content-type channels get first claim) | 61% |
| Themed (route purely by what a show is) | 33% |

**Ship streaming-first as the default.** It matches the convention already encoded in
`channels.csv`. Offer the others as user-selectable modes, but do not make them default —
themed routing empties the streaming channels entirely, and hybrid produces a file
inconsistent with the 4,200 rows already in it.

Worth surfacing in the UI: streaming-first concentrates content. In testing, Netflicks
alone took 161 of 806 series under pure streaming-first before the app's existing
assignments were respected. A "channel balance" view helps users spot this.

---

## 9. Required UI

The originating user request was: *"see all the media that's not already assigned to a
station easily so we can easily assign them to a channel... essentially a media library
view, not a channel view."*

**Media-library view (primary).** Every title in the library as a row: title, year,
episode count, assigned channel, and status — *already assigned by the app* vs *assigned
by Nostalgia Line* vs *unassigned*. Sortable by every column. Filterable to unassigned.
Searchable.

**Channel view (secondary).** Channel-by-channel with counts, flagging channels below a
threshold (1–3 titles) and channels that are empty.

**Review queue.** Anything the cascade resolved with low confidence, plus every orphan
network. In testing, 32 of 330 new assignments were low-confidence — and once verified
against real sources, **22 of those 32 were wrong**. That is a 69% error rate on the
uncertain tier. A review queue is not optional.

---

## 10. Verification

For any title where the network can't be resolved from TMDB, the app should not guess.
Options, in order of preference:

1. TMDB `/tv/{id}/external_ids` → cross-check TVDB, which sometimes has network data
   TMDB lacks
2. Flag for user review with the show's overview and poster for quick human judgment
3. Optional: a user-supplied override table, persisted across runs

Never infer a network from the title. Real failures from this run: *The Dark Wizard*
reads as fantasy but is an HBO Max documentary about a BASE jumper. *DTF St. Louis*
reads as reality TV but is an HBO limited series with Jason Bateman. *Rooster* reads as
generic comedy but is HBO with Steve Carell.

---

## 11. Configuration

```yaml
plex:
  url: http://192.168.1.245:32400     # host IP; Plex binds host network in Docker
  token: <X-Plex-Token>
  libraries: [Shows, Movies, ...]      # opt-in per library

tmdb:
  api_key: <key>                       # same key Kometa uses
  rate_limit: 50/sec                   # TMDB's documented ceiling

routing:
  mode: streaming_first                # streaming_first | hybrid | themed
  multi_channel: sanctioned_pairs_only # off | sanctioned_pairs_only | permissive
  orphan_network: parent_fallback      # parent_fallback | content_type | flag_only

output:
  additions_only: channels_additions.csv
  merged: channels_merged.csv
```

---

## 12. Reference data shipped with the app

- `network_map.csv` — 72 TMDB network → channel rules covering 62 channels. Expresses
  95% of a real 806-series library.
- `orphan_networks.csv` — §5 fallback table.
- `sanctioned_pairs` — derived at runtime from the user's own `channels.csv`, so it
  adapts if the dev changes the defaults.

---

## 13. Build order

1. Plex client — enumerate libraries, pull items with `tmdb://` guids
2. TMDB client — batch `/3/tv/{id}` and `/3/movie/{id}` with caching (libraries are
   large and mostly static; cache aggressively by tmdb_id)
3. Resolution cascade (§3) with the network map
4. Diff against the user's existing `channels.csv` → assigned / unassigned split
5. Media-library view — this is the feature users actually asked for
6. CSV export, additive-only, with the integrity assertion from §7
7. Review queue for low-confidence and orphan-network results
