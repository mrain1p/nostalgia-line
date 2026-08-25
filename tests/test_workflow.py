"""The 'what to do next' state machine.

Nothing here is new capability - it reads state the app already has. The value
is that it must never point somewhere useless, so every stage is pinned.
"""
from nostalgia_line.workflow import CURRENT, DONE, build

BASE = dict(
    configured=True,
    source_name="plex",
    scanned=True,
    scan_stats={"total": 806, "coverage_pct": 99.6},
    held_for_review=0,
    pending_additions=0,
    last_export_at=None,
    baseline_at=None,
    lineup_rows=4651,
)


def at(**overrides):
    return build(**{**BASE, **overrides})


def test_a_fresh_install_asks_you_to_connect():
    w = at(configured=False, scanned=False, scan_stats=None)
    assert w.current.key == "connect"
    assert w.current.action == "settings"


def test_nothing_beyond_connect_is_offered_until_configured():
    """Pointing someone at Scan before they have credentials is a dead end."""
    w = at(configured=False, scanned=False, scan_stats=None)
    assert [s.state for s in w.steps[1:]] == ["todo"] * 4


def test_once_connected_it_asks_for_a_scan():
    w = at(scanned=False, scan_stats=None)
    assert w.current.key == "scan"
    assert w.steps[0].state == DONE


def test_a_held_review_queue_becomes_the_next_step():
    w = at(held_for_review=56, pending_additions=287)
    assert w.current.key == "review"
    assert "56" in w.current.detail
    assert "handful" in w.current.detail, "say it is fewer decisions than it looks"


def test_with_review_clear_it_asks_for_the_export():
    w = at(held_for_review=0, pending_additions=287)
    assert w.current.key == "export"
    assert "287" in w.current.detail


def test_after_exporting_it_asks_you_to_apply_it():
    w = at(pending_additions=0, last_export_at=1.0, baseline_at=None)
    assert w.current.key == "apply"
    assert "NostalgiaTV" in w.current.title


def test_a_closed_round_trip_finishes_the_sequence():
    w = at(pending_additions=0, last_export_at=1.0, baseline_at=2.0, lineup_rows=4938)
    assert w.current is None
    assert w.complete is True
    assert "4938" in w.steps[-1].detail


def test_a_library_needing_nothing_is_already_complete():
    """Everything already assigned - do not invent work."""
    w = at(pending_additions=0, held_for_review=0)
    assert w.complete is True


def test_untraceable_titles_are_called_out_on_the_scan_step():
    w = at(no_tmdb_id=4, held_for_review=1)
    assert "4" in w.steps[1].detail
    assert "never route" in w.steps[1].detail


def test_every_step_offers_an_action_and_an_explanation():
    for step in at(held_for_review=5, pending_additions=5).steps:
        assert step.title and step.blurb
        assert step.action and step.action_label


def test_exactly_one_step_is_current_while_work_remains():
    for kwargs in (
        dict(configured=False, scanned=False, scan_stats=None),
        dict(scanned=False, scan_stats=None),
        dict(held_for_review=3, pending_additions=9),
        dict(pending_additions=9),
        dict(pending_additions=0, last_export_at=1.0),
    ):
        w = at(**kwargs)
        assert sum(1 for s in w.steps if s.state == CURRENT) == 1, kwargs


def test_the_dict_shape_the_ui_reads():
    d = at(held_for_review=2).to_dict()
    assert set(d) == {"steps", "current", "complete", "done_count", "total"}
    assert d["total"] == 5
    assert all(set(s) >= {"key", "title", "blurb", "state", "action"} for s in d["steps"])
