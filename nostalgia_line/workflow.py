"""What to do next.

The app grew a lot of surface, and surface without sequence is just a wall of
controls. Nothing here is new capability - it reads the state the app already
has and answers one question: given where you are, what is the next useful
action, and why.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DONE = "done"
CURRENT = "current"
TODO = "todo"
BLOCKED = "blocked"


@dataclass
class Step:
    key: str
    title: str
    blurb: str
    state: str = TODO
    detail: str = ""
    action: str = ""
    action_label: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "blurb": self.blurb,
            "state": self.state,
            "detail": self.detail,
            "action": self.action,
            "action_label": self.action_label,
        }


@dataclass
class Workflow:
    steps: list[Step] = field(default_factory=list)

    @property
    def current(self) -> Step | None:
        for step in self.steps:
            if step.state in (CURRENT, BLOCKED):
                return step
        return None

    @property
    def complete(self) -> bool:
        return all(s.state == DONE for s in self.steps)

    def to_dict(self) -> dict:
        current = self.current
        return {
            "steps": [s.to_dict() for s in self.steps],
            "current": current.key if current else None,
            "complete": self.complete,
            "done_count": sum(1 for s in self.steps if s.state == DONE),
            "total": len(self.steps),
        }


def build(
    *,
    configured: bool,
    source_name: str,
    scanned: bool,
    scan_stats: dict | None,
    held_for_review: int,
    pending_additions: int,
    last_export_at: float | None,
    baseline_at: float | None,
    lineup_rows: int,
    no_tmdb_id: int = 0,
) -> Workflow:
    """Work out which step the user is on, from state the app already tracks."""
    steps = [
        Step(
            "connect",
            "Connect",
            f"Point Nostalgia Line at your media server and TMDB. "
            f"The server supplies the library; TMDB supplies the broadcast network, "
            f"which no media server stores.",
            action="settings",
            action_label="Open settings",
        ),
        Step(
            "scan",
            "Scan",
            "Read every show, look each one up on TMDB, and work out which channel "
            "it belongs on. Nothing is changed anywhere - this only builds a picture.",
            action="scan",
            action_label="Scan library",
        ),
        Step(
            "review",
            "Review",
            "Some placements are guesses. Anything uncertain is held back from the "
            "export until you have looked at it, because a wrong placement you "
            "cannot explain is worse than none.",
            action="review",
            action_label="Open review queue",
        ),
        Step(
            "export",
            "Export",
            "Write channels_merged.csv - your existing lineup plus the new rows, "
            "in the format NostalgiaTV reads. Nothing existing is ever changed.",
            action="export",
            action_label="Export CSV",
        ),
        Step(
            "apply",
            "Apply in NostalgiaTV",
            "Upload channels_merged.csv in NostalgiaTV, then import it back here so "
            "Nostalgia Line counts those titles as settled rather than as its own "
            "suggestions. It cannot check NostalgiaTV itself, so this step is judged "
            "by what comes back.",
            action="import",
            action_label="Import it back",
        ),
    ]
    by_key = {s.key: s for s in steps}

    # 1. connect
    if configured:
        by_key["connect"].state = DONE
        by_key["connect"].detail = f"{source_name.title()} and TMDB are configured."
    else:
        by_key["connect"].state = CURRENT
        by_key["connect"].detail = "Not configured yet."
        return Workflow(steps)

    # 2. scan
    if scanned and scan_stats:
        by_key["scan"].state = DONE
        by_key["scan"].detail = (
            f"{scan_stats['total']} titles, {scan_stats['coverage_pct']}% placed."
        )
        if no_tmdb_id:
            by_key["scan"].detail += (
                f" {no_tmdb_id} have no TMDB id in your media server and can never route."
            )
    else:
        by_key["scan"].state = CURRENT
        by_key["scan"].detail = "No scan yet."
        return Workflow(steps)

    # 3. review
    if held_for_review:
        by_key["review"].state = CURRENT
        by_key["review"].detail = (
            f"{held_for_review} placement(s) held back. They group by cause, so this "
            f"is usually a handful of decisions rather than {held_for_review}."
        )
    else:
        by_key["review"].state = DONE
        by_key["review"].detail = "Nothing outstanding."

    # 4. export - needed whenever there are rows the lineup has not got yet
    if pending_additions:
        by_key["export"].state = CURRENT if not held_for_review else TODO
        by_key["export"].detail = f"{pending_additions} new row(s) ready to write."
    elif last_export_at:
        by_key["export"].state = DONE
        by_key["export"].detail = "Everything placed is already in your lineup."
    else:
        by_key["export"].state = DONE
        by_key["export"].detail = "Nothing new to add."

    # 5. apply - the loop is only closed once the merged file is back in here
    # We cannot see inside NostalgiaTV, so this step is inferred, never verified.
    # Say what is actually known - the lineup here grew - rather than implying the
    # upload was checked.
    applied = pending_additions == 0 and baseline_at is not None
    if applied:
        by_key["apply"].state = DONE
        by_key["apply"].detail = (
            f"Nothing pending. The lineup loaded here holds {lineup_rows} rows. "
            f"Nostalgia Line cannot see inside NostalgiaTV, so check there if unsure."
        )
    elif pending_additions == 0 and last_export_at is None:
        by_key["apply"].state = DONE
        by_key["apply"].detail = "Nothing to apply."
    else:
        by_key["apply"].state = TODO
        by_key["apply"].detail = (
            "Upload channels_merged.csv in NostalgiaTV, then import it back here."
        )
        if last_export_at and not pending_additions:
            by_key["apply"].state = CURRENT

    # Whatever is furthest along and unfinished is the one to show.
    if not any(s.state == CURRENT for s in steps):
        for step in steps:
            if step.state == TODO:
                step.state = CURRENT
                break

    return Workflow(steps)
