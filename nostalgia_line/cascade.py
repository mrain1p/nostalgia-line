"""The resolution cascade (spec S3).

Apply in order, first match wins. Every step records *why* it fired, because a
wrong assignment the user cannot explain is worse than no assignment: in the
spec's own testing, 22 of 32 low-confidence guesses were wrong (S9).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .channels import (
    ChannelCatalog,
    DefaultAssignments,
    NetworkMap,
    load_network_map,
    load_orphan_networks,
)
from .stations import CustomStation, StationBook
from .tmdb import TMDBMovie, TMDBSeries

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
NONE = "none"  # nothing was placed, so there is nothing to be confident about
CONFIDENCE_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2, NONE: 3}

# Status of a library item after the cascade runs.
STATUS_APP = "already_assigned"   # the shipped channels.csv already places it
STATUS_LINE = "assigned"          # Nostalgia Line placed it
STATUS_UNASSIGNED = "unassigned"  # nothing could place it

# -- content-type rules (spec S3.3) --------------------------------------
# Plex's TV genre tags come from TMDB's TV taxonomy, which has no Travel genre and
# merges sci-fi with fantasy. Keywords carry the signal genres lose.

TRAVEL_KEYWORDS = {
    "travel", "road trip", "culinary travel", "tourism", "expedition", "backpacking",
    "world travel", "adventure travel", "food and travel",
}
FOOD_KEYWORDS = {
    "cooking", "food competition", "chef", "restaurant", "baking", "cuisine",
    "cooking show", "culinary", "food",
}
TRUE_CRIME_KEYWORDS = {
    "true crime", "murder", "investigation", "serial killer", "cold case",
    "forensic", "homicide", "criminal investigation", "missing person",
}
NATURE_KEYWORDS = {
    "nature", "wildlife", "animal", "animals", "conservation", "ocean",
    "natural history", "ecology", "wilderness",
}
HISTORY_KEYWORDS = {
    "history", "war documentary", "world war ii", "world war i", "ancient history",
    "historical", "archaeology", "civil war",
}
HORROR_KEYWORDS = {"horror", "supernatural horror", "slasher", "zombie", "haunting", "paranormal"}
SCIFI_KEYWORDS = {"science fiction", "space opera", "dystopia", "time travel", "cyberpunk", "alien"}
ADULT_ANIMATION_KEYWORDS = {"adult animation", "adult cartoon", "satire"}
ANIME_KEYWORDS = {"anime", "based on manga", "shounen", "shoujo", "seinen", "isekai", "mecha"}
SPORTS_KEYWORDS = {
    "sports", "sport", "football", "basketball", "baseball", "soccer", "olympics",
    "sports documentary", "athlete", "wrestling",
}

# Channel numbers referenced by the content-type rules.
CH_MEAL = 1012
CH_STORY = 1017
CH_ANIMAL = 1033
CH_NATGEO = 1035
CH_SIGHFI = 1036
CH_TERROR = 1037
CH_TRUTH = 1046
CH_AME = 1048
CH_ADULT_SKIM = 1051
CH_TRIP = 1059
CH_YESPN = 1061
CH_MUNCHY = 1071

# Genre channels, 1097-1110 (spec S4). TMDB's TV taxonomy on the left.
GENRE_CHANNELS: dict[str, int] = {
    "action & adventure": 1097,
    "action": 1097,
    "comedy": 1098,
    "drama": 1099,
    "western": 1100,
    "family": 1101,
    "kids": 1101,
    "crime": 1102,
    "mystery": 1102,
    "war & politics": 1103,
    "war": 1103,
    "sci-fi & fantasy": 1104,
    "science fiction": 1104,
    "romance": 1105,
    "thriller": 1106,
    "fantasy": 1107,
    "adventure": 1108,
    "music": 1109,
    "documentary": 1032,
}
# "reality" is deliberately absent. It is the largest genre with no honest
# channel analogue, and inventing one buries hundreds of titles on a channel the
# user never chose. Unrouted reality falls through to the review queue instead.

# Genres that carry no routing signal on their own.
WEAK_GENRES = {"documentary", "reality", "news", "talk", "soap", "animation"}


@dataclass
class Assignment:
    channel_number: int
    channel_name: str
    rule: str
    confidence: str
    reason: str
    primary: bool = True

    def to_dict(self) -> dict:
        return {
            "channel_number": self.channel_number,
            "channel_name": self.channel_name,
            "rule": self.rule,
            "confidence": self.confidence,
            "reason": self.reason,
            "primary": self.primary,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Assignment":
        return cls(
            channel_number=int(raw["channel_number"]),
            channel_name=str(raw.get("channel_name", "")),
            rule=str(raw.get("rule", "")),
            confidence=str(raw.get("confidence", LOW)),
            reason=str(raw.get("reason", "")),
            primary=bool(raw.get("primary", True)),
        )


@dataclass
class Resolution:
    """What the cascade decided about one library item."""

    status: str
    assignments: list[Assignment] = field(default_factory=list)
    existing_channels: list[int] = field(default_factory=list)
    network: str | None = None
    needs_review: bool = False
    review_reason: str = ""

    @property
    def confidence(self) -> str:
        # The user's own channels.csv is authoritative, not a guess.
        if self.status == STATUS_APP:
            return HIGH
        if not self.assignments:
            return NONE
        return min((a.confidence for a in self.assignments), key=lambda c: CONFIDENCE_ORDER[c])

    @property
    def primary(self) -> Assignment | None:
        for assignment in self.assignments:
            if assignment.primary:
                return assignment
        return self.assignments[0] if self.assignments else None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "assignments": [a.to_dict() for a in self.assignments],
            "existing_channels": self.existing_channels,
            "network": self.network,
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Resolution":
        return cls(
            status=str(raw["status"]),
            assignments=[Assignment.from_dict(a) for a in (raw.get("assignments") or [])],
            existing_channels=[int(c) for c in (raw.get("existing_channels") or [])],
            network=raw.get("network"),
            needs_review=bool(raw.get("needs_review", False)),
            review_reason=str(raw.get("review_reason", "")),
        )


def _hits(haystack: set[str], needles: set[str]) -> set[str]:
    """Keyword hits, tolerating multi-word TMDB keywords."""
    found = haystack & needles
    if found:
        return found
    for needle in needles:
        if " " in needle and any(needle in item for item in haystack):
            found.add(needle)
    return found


class Cascade:
    """Resolves a library item to one or more channels."""

    def __init__(
        self,
        catalog: ChannelCatalog,
        defaults: DefaultAssignments,
        network_map: NetworkMap,
        orphan_map: dict[str, tuple[int, str, str]],
        stations: StationBook | None = None,
        mode: str = "streaming_first",
        multi_channel: str = "sanctioned_pairs_only",
        orphan_policy: str = "parent_fallback",
    ):
        self.catalog = catalog
        self.defaults = defaults
        self.network_map = network_map
        self.orphan_map = orphan_map
        self.stations = stations or StationBook()
        self.mode = mode
        self.multi_channel = multi_channel
        self.orphan_policy = orphan_policy

    @classmethod
    def from_config(
        cls, cfg, catalog, defaults, stations=None, network_overrides=None
    ) -> "Cascade":
        network_map = load_network_map(cfg.path(cfg.data.network_map))
        if network_overrides:
            network_map.apply_overrides(network_overrides, catalog)
        return cls(
            catalog=catalog,
            defaults=defaults,
            network_map=network_map,
            orphan_map=load_orphan_networks(cfg.path(cfg.data.orphan_networks)),
            stations=stations,
            mode=cfg.routing.mode,
            multi_channel=cfg.routing.multi_channel,
            orphan_policy=cfg.routing.orphan_network,
        )

    # -- helpers ---------------------------------------------------------

    def _assign(self, number: int, rule: str, confidence: str, reason: str, primary=True) -> Assignment:
        return Assignment(
            channel_number=number,
            channel_name=self.catalog.name_of(number),
            rule=rule,
            confidence=confidence,
            reason=reason,
            primary=primary,
        )

    def _station_for_network(self, network: str | None) -> CustomStation | None:
        claims = self.stations.claiming_network(network)
        return claims[0] if claims else None

    def _mirrors_for_channel(self, number: int) -> list[CustomStation]:
        return [s for s in self.stations.claiming_channel(number)]

    def _apply_stations(self, assignments: list[Assignment]) -> list[Assignment]:
        """Let custom stations claim or mirror what the stock rules produced."""
        if not len(self.stations):
            return assignments
        out: list[Assignment] = []
        for assignment in assignments:
            claimed = False
            for station in self._mirrors_for_channel(assignment.channel_number):
                if station.mode == "claim":
                    out.append(
                        Assignment(
                            channel_number=station.number,
                            channel_name=station.name,
                            rule="custom_station_claim",
                            confidence=assignment.confidence,
                            reason=(
                                f"custom station '{station.name}' claims the "
                                f"{assignment.channel_name} lineup"
                            ),
                            primary=assignment.primary,
                        )
                    )
                    claimed = True
                else:
                    out.append(
                        Assignment(
                            channel_number=station.number,
                            channel_name=station.name,
                            rule="custom_station_mirror",
                            confidence=assignment.confidence,
                            reason=(
                                f"custom station '{station.name}' mirrors the "
                                f"{assignment.channel_name} lineup"
                            ),
                            primary=False,
                        )
                    )
            if not claimed:
                out.append(assignment)
        return out

    def _gate_secondary(
        self, primary: Assignment, candidates: list[Assignment], co_production: bool = False
    ) -> list[Assignment]:
        """Spec S6: emit a second channel only when the default file sanctions the pair.

        Co-productions are exempt in ``sanctioned_pairs_only``: when TMDB itself
        lists both networks, both are true, and picking a winner loses information.
        """
        if self.multi_channel == "off":
            return []
        kept: list[Assignment] = []
        for candidate in candidates:
            if candidate.channel_number == primary.channel_number:
                continue
            if self.multi_channel == "permissive":
                kept.append(candidate)
                continue
            if co_production or self.defaults.is_sanctioned_pair(
                primary.channel_number, candidate.channel_number
            ):
                kept.append(candidate)
        return kept

    # -- content-type rules (step 3) -------------------------------------

    def _content_type(self, genres: set[str], keywords: set[str]) -> Assignment | None:
        if hits := _hits(keywords, TRAVEL_KEYWORDS):
            return self._assign(CH_TRIP, "content_type", MEDIUM, f"travel keywords: {sorted(hits)}")
        if hits := _hits(keywords, FOOD_KEYWORDS):
            return self._assign(CH_MEAL, "content_type", MEDIUM, f"food keywords: {sorted(hits)}")
        if hits := _hits(keywords, TRUE_CRIME_KEYWORDS):
            channel = CH_TRUTH if "crime" in genres else CH_AME
            return self._assign(channel, "content_type", MEDIUM, f"true-crime keywords: {sorted(hits)}")
        if hits := _hits(keywords, NATURE_KEYWORDS):
            channel = CH_NATGEO if "documentary" in genres else CH_ANIMAL
            return self._assign(channel, "content_type", MEDIUM, f"nature keywords: {sorted(hits)}")
        if hits := _hits(keywords, HISTORY_KEYWORDS):
            return self._assign(CH_STORY, "content_type", MEDIUM, f"history keywords: {sorted(hits)}")
        if hits := _hits(keywords, HORROR_KEYWORDS):
            return self._assign(CH_TERROR, "content_type", MEDIUM, f"horror keywords: {sorted(hits)}")
        if hits := _hits(keywords, SCIFI_KEYWORDS):
            return self._assign(CH_SIGHFI, "content_type", MEDIUM, f"sci-fi keywords: {sorted(hits)}")
        if "animation" in genres and (hits := _hits(keywords, ADULT_ANIMATION_KEYWORDS)):
            return self._assign(CH_ADULT_SKIM, "content_type", MEDIUM, f"adult animation: {sorted(hits)}")
        if hits := _hits(keywords, SPORTS_KEYWORDS):
            return self._assign(CH_YESPN, "content_type", MEDIUM, f"sports keywords: {sorted(hits)}")
        return None

    def _anime(self, series: TMDBSeries, genres: set[str], keywords: set[str]) -> Assignment | None:
        japanese = series.original_language == "ja" or "JP" in series.origin_country
        if japanese and "animation" in genres:
            return self._assign(
                CH_MUNCHY, "anime", MEDIUM, "Japanese-language animation"
            )
        if hits := _hits(keywords, ANIME_KEYWORDS):
            return self._assign(CH_MUNCHY, "anime", MEDIUM, f"anime keywords: {sorted(hits)}")
        return None

    def _genre_channel(self, genres: set[str]) -> Assignment | None:
        for genre in sorted(genres):
            if genre in WEAK_GENRES:
                continue
            if number := GENRE_CHANNELS.get(genre):
                return self._assign(number, "genre", LOW, f"TMDB genre '{genre}'")
        for genre in sorted(genres):
            if number := GENRE_CHANNELS.get(genre):
                return self._assign(number, "genre", LOW, f"TMDB genre '{genre}' (weak signal)")
        return None

    # -- the cascade -----------------------------------------------------

    def resolve_series(
        self, title: str, year: int | None, series: TMDBSeries | None
    ) -> Resolution:
        existing = sorted(self.defaults.channels_for(title, year))
        if existing:
            return Resolution(
                status=STATUS_APP,
                existing_channels=existing,
                network=(series.primary_network if series else None),
            )

        if series is None or series.missing:
            return Resolution(
                status=STATUS_UNASSIGNED,
                needs_review=True,
                review_reason=(
                    "no TMDB record for this item - Plex may be missing a tmdb guid"
                ),
            )

        genres = {g.casefold() for g in series.genres}
        keywords = {k.casefold() for k in series.keywords}
        network = series.primary_network
        themed_first = self.mode in ("themed", "hybrid")

        # A custom station pointed at this exact network wins outright: the user
        # said so explicitly.
        if station := self._station_for_network(network):
            primary = Assignment(
                channel_number=station.number,
                channel_name=station.name,
                rule="custom_station_network",
                confidence=HIGH,
                reason=f"custom station '{station.name}' is configured for network '{network}'",
            )
            return Resolution(
                status=STATUS_LINE, assignments=[primary], network=network
            )

        content = self._anime(series, genres, keywords) or self._content_type(genres, keywords)

        # Step 1 - network match. Skipped entirely in themed mode.
        if self.mode != "themed" and network:
            mapped = self.network_map.get(network, series.origin_country)
            if mapped:
                number, _ = mapped
                primary = self._assign(
                    number, "network", HIGH, f"TMDB network '{network}'"
                )
                # Hybrid gives a content-type channel first claim (spec S8).
                if themed_first and content and self.mode == "hybrid":
                    content.primary = True
                    primary.primary = False
                    ordered = [content] + self._gate_secondary(content, [primary])
                else:
                    ordered = [primary]
                    # Co-productions: TMDB lists more than one network (spec S6).
                    co_prod = []
                    for other in series.networks[1:]:
                        other_mapped = self.network_map.get(other, series.origin_country)
                        if other_mapped and other_mapped[0] != number:
                            co_prod.append(
                                self._assign(
                                    other_mapped[0],
                                    "network_coproduction",
                                    HIGH,
                                    f"TMDB also lists network '{other}'",
                                    primary=False,
                                )
                            )
                    ordered += self._gate_secondary(primary, co_prod, co_production=True)
                    if content:
                        ordered += self._gate_secondary(
                            primary,
                            [
                                Assignment(
                                    content.channel_number,
                                    content.channel_name,
                                    content.rule,
                                    content.confidence,
                                    content.reason,
                                    primary=False,
                                )
                            ],
                        )
                return Resolution(
                    status=STATUS_LINE,
                    assignments=self._apply_stations(ordered),
                    network=network,
                )

            # Step 2 - orphan network: it exists, but has no channel analogue.
            orphan = self.orphan_map.get(network.casefold())
            if orphan and self.orphan_policy == "parent_fallback":
                number, _, rationale = orphan
                primary = self._assign(
                    number,
                    "orphan_network",
                    MEDIUM,
                    f"'{network}' has no channel analogue; routed by {rationale}",
                )
                return Resolution(
                    status=STATUS_LINE,
                    assignments=self._apply_stations([primary]),
                    network=network,
                    needs_review=True,
                    review_reason=f"orphan network '{network}' routed by fallback table",
                )

            if self.orphan_policy == "flag_only":
                return Resolution(
                    status=STATUS_UNASSIGNED,
                    network=network,
                    needs_review=True,
                    review_reason=f"network '{network}' has no mapping (flag_only mode)",
                )

        # Step 3 - content-type rules.
        if content:
            review = self.mode != "themed" and bool(network)
            return Resolution(
                status=STATUS_LINE,
                assignments=self._apply_stations([content]),
                network=network,
                needs_review=review,
                review_reason=(
                    f"unmapped network '{network}' - routed on content type instead"
                    if review
                    else ""
                ),
            )

        # A custom station can also claim on keywords alone.
        for station in self.stations.enabled():
            if station.matches_keyword(keywords):
                return Resolution(
                    status=STATUS_LINE,
                    assignments=[
                        Assignment(
                            station.number,
                            station.name,
                            "custom_station_keyword",
                            MEDIUM,
                            f"custom station '{station.name}' matches keywords",
                        )
                    ],
                    network=network,
                    needs_review=True,
                    review_reason=f"custom station '{station.name}' matched on keywords only",
                )

        # Step 4 - genre channel, last resort. Always low confidence.
        if genre_hit := self._genre_channel(genres):
            return Resolution(
                status=STATUS_LINE,
                assignments=self._apply_stations([genre_hit]),
                network=network,
                needs_review=True,
                review_reason="placed by genre fallback only - verify before export",
            )

        # Step 5 - unassigned. Never silently dropped.
        return Resolution(
            status=STATUS_UNASSIGNED,
            network=network,
            needs_review=True,
            review_reason=(
                f"no rule matched (network={network or 'none'}, genres={sorted(genres) or 'none'})"
            ),
        )

    # -- films (post-1.0, spec S3 "For films") ---------------------------

    def resolve_movie(self, title: str, year: int | None, movie: TMDBMovie | None) -> Resolution:
        existing = sorted(self.defaults.channels_for(title, year))
        if existing:
            return Resolution(status=STATUS_APP, existing_channels=existing)
        if movie is None or movie.missing:
            return Resolution(
                status=STATUS_UNASSIGNED,
                needs_review=True,
                review_reason="no TMDB record for this film",
            )

        genres = {g.casefold() for g in movie.genres}
        keywords = {k.casefold() for k in movie.keywords}
        release_year = movie.year or year

        if "oscar" in movie.collection.casefold():
            return Resolution(
                status=STATUS_LINE,
                assignments=[self._assign(1113, "collection", HIGH, f"collection '{movie.collection}'")],
            )

        distinctive = {
            "western": 1100, "horror": 1037, "music": 1109, "war": 1103,
            "documentary": 1032, "history": 1017,
        }
        for genre, number in distinctive.items():
            if genre in genres:
                # Era split keeps modern horror from swallowing the film library.
                if genre == "horror" and release_year and release_year < 2000:
                    number = 1053
                return self._resolution(
                    self._assign(number, "film_genre", MEDIUM, f"distinctive genre '{genre}'")
                )

        if release_year and release_year < 1950:
            return self._resolution(self._assign(1050, "film_era", MEDIUM, "released before 1950"))

        is_anime = movie.original_language == "ja" and "animation" in genres
        acclaimed = movie.vote_average >= 7.5 and movie.vote_count >= 500
        if movie.original_language not in ("en", "") and acclaimed and not is_anime:
            return self._resolution(
                self._assign(1112, "film_foreign", MEDIUM, "foreign-language and acclaimed")
            )

        if release_year:
            decade = max(1950, min(2020, (release_year // 10) * 10))
            number = 1089 + (decade - 1950) // 10
            return self._resolution(
                self._assign(number, "film_decade", LOW, f"released {release_year}"),
                review=True,
                reason="placed by decade only - verify before export",
            )

        return Resolution(
            status=STATUS_UNASSIGNED,
            needs_review=True,
            review_reason="no release year and no distinctive genre",
        )

    def _resolution(self, assignment: Assignment, review: bool = False, reason: str = "") -> Resolution:
        return Resolution(
            status=STATUS_LINE,
            assignments=self._apply_stations([assignment]),
            needs_review=review,
            review_reason=reason,
        )
