"""Measure the cascade against the lineup it is supposed to reproduce.

Every title the user's channels.csv already places is free ground truth: ask
the cascade what it *would* have chosen, and compare. That turns "the routing
feels right" into a number - per rule, with sample sizes - and it is how any
future routing change gets justified.

The probe prefix is the load-bearing detail. ``resolve_series`` short-circuits
at step 0 when the title is already in the lineup - which, for ground truth,
is every title by construction. Probing under a mangled name forces the
cascade past that step to an independent opinion. Without the prefix the
measurement compares the lineup with itself and reports perfect agreement,
which is the worst failure mode available because it looks like good news.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cascade import STATUS_APP, STATUS_LINE, Cascade
from .media import SHOW
from .pipeline import ScanResult
from .tmdb import TMDBCache, TMDBSeries

# Prepended to every probed title so step 0 cannot match the lineup row the
# title came from. normalize_title() folds it to "probe<title>", which exists
# in no real lineup.
PROBE_PREFIX = "__probe__"

# Below this many samples a percentage is a signal, not a verdict - 0/9 was
# what prompted this module, and even that needed the spec's independent
# measurement (S9) to be trusted.
MIN_SAMPLES = 20


def _pct(agree: int, total: int) -> float | None:
    return round(100.0 * agree / total, 1) if total else None


@dataclass
class Disagreement:
    """One title where the cascade and the lineup part ways.

    Each of these is either a cascade bug or a debatable call in the lineup,
    and both are worth a human look.
    """

    uid: str
    title: str
    year: int | None
    network: str | None
    rule: str
    confidence: str
    reason: str
    ours: list[dict]    # what the cascade chose: {number, name}
    theirs: list[dict]  # what the lineup says:   {number, name}

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "year": self.year,
            "network": self.network,
            "rule": self.rule,
            "confidence": self.confidence,
            "reason": self.reason,
            "ours": self.ours,
            "theirs": self.theirs,
        }


@dataclass
class AccuracyReport:
    """Agreement between the cascade and the existing lineup, one mode."""

    mode: str
    ground_truth: int = 0  # lineup-placed shows considered
    sampled: int = 0       # probes that produced an opinion
    agree: int = 0
    # Why a ground-truth title produced no sample. no_opinion is itself
    # informative: the cascade could not place a title the lineup places.
    skipped: dict[str, int] = field(
        default_factory=lambda: {"no_tmdb_id": 0, "no_cached_record": 0, "no_opinion": 0}
    )
    # rule -> [agree, total]
    by_rule: dict[str, list[int]] = field(default_factory=dict)
    disagreements: list[Disagreement] = field(default_factory=list)
    # The retired genre rule, scored on what it *suggests*. Kept separate from
    # the placement figures: it no longer places anything, but its track record
    # is the evidence the films decision will need.
    suggestion_agree: int = 0
    suggestion_n: int = 0

    @property
    def pct(self) -> float | None:
        return _pct(self.agree, self.sampled)

    def _verdict(self, total: int) -> bool:
        return total >= MIN_SAMPLES

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "ground_truth": self.ground_truth,
            "sampled": self.sampled,
            "agree": self.agree,
            "pct": self.pct,
            "sufficient": self._verdict(self.sampled),
            "skipped": dict(self.skipped),
            "by_rule": [
                {
                    "rule": rule,
                    "agree": agree,
                    "n": total,
                    "pct": _pct(agree, total),
                    "sufficient": self._verdict(total),
                }
                for rule, (agree, total) in sorted(
                    self.by_rule.items(), key=lambda kv: -kv[1][1]
                )
            ],
            "suggestions": {
                "agree": self.suggestion_agree,
                "n": self.suggestion_n,
                "pct": _pct(self.suggestion_agree, self.suggestion_n),
                "sufficient": self._verdict(self.suggestion_n),
            },
            "disagreements": [d.to_dict() for d in self.disagreements],
        }

    def summary(self) -> dict:
        """The mode-comparison row: everything but the disagreement list."""
        payload = self.to_dict()
        payload.pop("disagreements")
        return payload


def measure(result: ScanResult, cascade: Cascade, cache: TMDBCache) -> AccuracyReport:
    """Probe every lineup-placed show and score the cascade against it.

    Only shows are measured: the ground truth is looked up in the series cache,
    and film routing is a different (currently disabled) cascade whose accuracy
    should be measured on film ground truth when that decision comes up.
    """
    report = AccuracyReport(mode=cascade.mode)
    name_of = cascade.catalog.name_of

    for entry in result.entries:
        if entry.status != STATUS_APP or entry.type != SHOW:
            continue
        report.ground_truth += 1
        if not entry.tmdb_id:
            report.skipped["no_tmdb_id"] += 1
            continue
        raw = cache.get("series", entry.tmdb_id)
        if raw is None:
            report.skipped["no_cached_record"] += 1
            continue

        probe = cascade.resolve_series(
            PROBE_PREFIX + entry.title, entry.year, TMDBSeries.from_dict(raw)
        )
        theirs = entry.resolution.existing_channels

        # The retired genre rule is scored on its suggestion, separately.
        if probe.suggestion is not None:
            report.suggestion_n += 1
            if probe.suggestion.channel_number in theirs:
                report.suggestion_agree += 1

        if probe.status != STATUS_LINE or not probe.assignments:
            report.skipped["no_opinion"] += 1
            continue

        choice = probe.primary or probe.assignments[0]
        report.sampled += 1
        tally = report.by_rule.setdefault(choice.rule, [0, 0])
        tally[1] += 1
        if choice.channel_number in theirs:
            report.agree += 1
            tally[0] += 1
        else:
            report.disagreements.append(
                Disagreement(
                    uid=entry.uid,
                    title=entry.title,
                    year=entry.year,
                    network=entry.network,
                    rule=choice.rule,
                    confidence=choice.confidence,
                    reason=choice.reason,
                    ours=[
                        {"number": a.channel_number, "name": a.channel_name}
                        for a in probe.assignments
                    ],
                    theirs=[{"number": n, "name": name_of(n)} for n in theirs],
                )
            )

    return report
