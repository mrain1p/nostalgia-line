"""HTTP surface against a server that has a completed scan in memory.

Uses the same throwaway-config trick as test_api, then injects a synthetic
ScanResult so the endpoints that need one can be driven without Plex or TMDB.
"""
import importlib
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from nostalgia_line.cascade import (
    HIGH,
    LOW,
    STATUS_APP,
    STATUS_LINE,
    STATUS_UNASSIGNED,
    Assignment,
    Resolution,
)
from nostalgia_line.pipeline import LibraryEntry, ScanResult

from .conftest import DATA


def make_entry(uid, title, network, channels, status=STATUS_LINE, review=False, conf=HIGH):
    # For an already-assigned entry the channels are the lineup's, not the
    # cascade's - they live in existing_channels and there are no assignments.
    already = status == STATUS_APP
    return LibraryEntry(
        uid=uid,
        title=title,
        year=2020,
        type="show",
        section="Shows",
        episode_count=12,
        season_count=2,
        tmdb_id=int(uid.rsplit(":", 1)[-1]),
        network=network,
        resolution=Resolution(
            status=status,
            assignments=(
                []
                if already
                else [Assignment(c, f"Ch{c}", "network", conf, "test") for c in channels]
            ),
            existing_channels=list(channels) if already else [],
            network=network,
            needs_review=review,
            review_reason="uncertain" if review else "",
        ),
    )


SCAN = [
    make_entry("tmdb:show:1", "Alpha Show", "HBO", [1068]),
    make_entry("tmdb:show:2", "Beta Show", "Netflix", [1064]),
    make_entry("tmdb:show:3", "Gamma Show", "Weird Service", [1099], review=True, conf=LOW),
    make_entry("tmdb:show:4", "Delta Show", "Weird Service", [1099], review=True, conf=LOW),
    make_entry("tmdb:show:5", "Epsilon Show", None, [], status=STATUS_UNASSIGNED, review=True),
    make_entry("tmdb:show:6", "Zeta Show", "HBO", [], status=STATUS_APP),
]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("api_scanned")
    config = {
        "plex": {"url": "http://fake:32400", "token": "t", "libraries": []},
        "tmdb": {"api_key": "k", "rate_limit": 50},
        "routing": {
            "mode": "streaming_first",
            "multi_channel": "sanctioned_pairs_only",
            "orphan_network": "parent_fallback",
        },
        "output": {"additions_only": "additions.csv", "merged": "merged.csv"},
        "data": {
            "channels_csv": str(workdir / "channels.csv"),
            "network_map": str(DATA / "network_map.csv"),
            "orphan_networks": str(DATA / "orphan_networks.csv"),
            "channel_catalog": str(DATA / "channel_catalog.csv"),
            "cache_dir": str(workdir / "cache"),
            "state_file": str(workdir / "state.json"),
        },
        "server": {"host": "127.0.0.1", "port": 8777},
    }
    # a private copy of channels.csv, since one test replaces it
    (workdir / "channels.csv").write_bytes((DATA / "channels.csv").read_bytes())
    config_path = workdir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    previous = os.environ.get("NOSTALGIA_CONFIG")
    os.environ["NOSTALGIA_CONFIG"] = str(config_path)
    for key in ("PLEX_URL", "PLEX_TOKEN", "TMDB_API_KEY"):
        os.environ.pop(key, None)

    from nostalgia_line import server

    importlib.reload(server)
    with TestClient(server.app) as test_client:
        test_client.server_module = server
        yield test_client

    if previous is None:
        os.environ.pop("NOSTALGIA_CONFIG", None)
    else:
        os.environ["NOSTALGIA_CONFIG"] = previous


@pytest.fixture(autouse=True)
def fresh_scan(client):
    """Reset the in-memory scan before each test so bulk edits do not leak."""
    import copy

    state = client.server_module.state
    state.result = ScanResult(entries=copy.deepcopy(SCAN), sections=["Shows"])
    state.store.overrides.clear()
    state.store.networks.clear()
    state.stale = False
    state.stale_reason = ""
    state.accuracy = None
    state.accuracy_key = None
    yield


# -- staleness -----------------------------------------------------------


def test_results_start_fresh(client):
    assert client.get("/api/status").json()["stale"] is False


def test_remapping_a_network_marks_the_results_stale(client):
    """The shown scan was produced under the old rules; say so."""
    client.post("/api/networks/map", json={"network": "Weird Service", "channel": 1051})
    body = client.get("/api/status").json()
    assert body["stale"] is True
    assert "Weird Service" in body["stale_reason"]


def test_changing_routing_marks_the_results_stale(client):
    client.post("/api/settings", json={"routing_mode": "themed"})
    assert client.get("/api/status").json()["stale"] is True
    client.post("/api/settings", json={"routing_mode": "streaming_first"})


def test_adding_a_station_marks_the_results_stale(client):
    created = client.post(
        "/api/stations", json={"name": "Stale Maker", "source_networks": ["G4"]}
    ).json()
    assert client.get("/api/status").json()["stale"] is True
    client.delete(f"/api/stations/{created['number']}")


def test_a_manual_override_does_not_mark_results_stale(client):
    """An override is applied immediately, so nothing is out of date."""
    client.post("/api/override", json={"uid": "tmdb:show:1", "channels": [1044]})
    assert client.get("/api/status").json()["stale"] is False


# -- cancellation --------------------------------------------------------


def test_cancelling_when_nothing_runs_is_a_conflict(client):
    assert client.post("/api/scan/cancel").status_code == 409


# -- library filters and sorting -----------------------------------------


def test_library_returns_the_scan(client):
    body = client.get("/api/library").json()
    assert body["scanned"] is True
    assert body["total"] == len(SCAN)


def test_filter_by_network(client):
    body = client.get("/api/library", params={"network": "Weird Service"}).json()
    assert body["total"] == 2
    assert {i["title"] for i in body["items"]} == {"Gamma Show", "Delta Show"}


def test_filter_by_confidence(client):
    body = client.get("/api/library", params={"confidence": "low"}).json()
    assert {i["title"] for i in body["items"]} == {"Gamma Show", "Delta Show"}


def test_filter_by_rule(client):
    assert client.get("/api/library", params={"rule": "network"}).json()["total"] == 4
    assert client.get("/api/library", params={"rule": "nonesuch"}).json()["total"] == 0


def test_filter_by_status_and_review(client):
    assert client.get("/api/library", params={"status_filter": "unassigned"}).json()["total"] == 1
    assert client.get("/api/library", params={"review_only": True}).json()["total"] == 3


def test_sort_by_confidence_puts_certain_first(client):
    items = client.get("/api/library", params={"sort": "confidence"}).json()["items"]
    assert [i["confidence"] for i in items] == ["high", "high", "high", "low", "low", "none"]


def test_an_already_assigned_title_is_not_reported_as_low_confidence(client):
    """It came from the user's own channels.csv, so it is authoritative."""
    item = client.get("/api/item/tmdb:show:6").json()
    assert item["status"] == STATUS_APP
    assert item["confidence"] == "high"


def test_an_unplaced_title_has_no_confidence_rather_than_low(client):
    item = client.get("/api/item/tmdb:show:5").json()
    assert item["status"] == STATUS_UNASSIGNED
    assert item["confidence"] == "none"


def test_seasons_are_exposed(client):
    item = client.get("/api/library").json()["items"][0]
    assert item["season_count"] == 2


def test_item_endpoint_returns_one_entry(client):
    body = client.get("/api/item/tmdb:show:1").json()
    assert body["title"] == "Alpha Show"
    assert client.get("/api/item/tmdb:show:999").status_code == 404


# -- networks ------------------------------------------------------------


def test_networks_rollup_is_served(client):
    body = client.get("/api/networks").json()
    assert body["scanned"] is True
    assert body["networks"][0]["network"] == "Weird Service"
    assert body["unmapped_titles"] == 2


def test_diagnostics_are_served(client):
    body = client.get("/api/networks").json()
    assert body["diagnostics"]["no_network"] == 1


def test_mapping_a_network_persists(client):
    response = client.post("/api/networks/map", json={"network": "Weird Service", "channel": 1051})
    assert response.status_code == 200
    assert response.json()["channel_name"] == "Adult Skim"
    assert client.server_module.state.store.networks["weird service"] == 1051

    body = client.get("/api/networks").json()
    weird = next(n for n in body["networks"] if n["network"] == "Weird Service")
    assert weird["status"] == "custom"
    assert weird["channel_number"] == 1051


def test_mapping_rejects_a_no_content_channel(client):
    response = client.post("/api/networks/map", json={"network": "Weird Service", "channel": 1074})
    assert response.status_code == 400
    assert "no content" in response.json()["detail"]


def test_mapping_rejects_an_unknown_channel(client):
    assert client.post(
        "/api/networks/map", json={"network": "X", "channel": 4242}
    ).status_code == 400


def test_mapping_rejects_a_blank_network(client):
    assert client.post(
        "/api/networks/map", json={"network": "   ", "channel": 1068}
    ).status_code == 400


def test_unmapping_a_network(client):
    client.post("/api/networks/map", json={"network": "Weird Service", "channel": 1051})
    assert client.delete("/api/networks/map/Weird Service").status_code == 200
    assert client.delete("/api/networks/map/Weird Service").status_code == 404


# -- bulk assignment -----------------------------------------------------


def test_bulk_replace_assigns_every_uid(client):
    response = client.post(
        "/api/override/bulk",
        json={"uids": ["tmdb:show:3", "tmdb:show:4"], "channels": [1051]},
    )
    assert response.json()["updated"] == 2
    for uid in ("tmdb:show:3", "tmdb:show:4"):
        item = client.get(f"/api/item/{uid}").json()
        assert [c["number"] for c in item["channels"]] == [1051]
        assert item["overridden"] is True
        assert item["needs_review"] is False


def test_bulk_add_keeps_the_existing_channel(client):
    client.post(
        "/api/override/bulk",
        json={"uids": ["tmdb:show:1"], "channels": [1044], "mode": "add"},
    )
    item = client.get("/api/item/tmdb:show:1").json()
    assert sorted(c["number"] for c in item["channels"]) == [1044, 1068]


def test_bulk_survives_into_the_store(client):
    client.post("/api/override/bulk", json={"uids": ["tmdb:show:3"], "channels": [1051]})
    assert client.server_module.state.store.overrides["tmdb:show:3"] == [1051]


def test_bulk_rejects_an_unknown_uid(client):
    response = client.post(
        "/api/override/bulk", json={"uids": ["tmdb:show:404"], "channels": [1051]}
    )
    assert response.status_code == 404


def test_bulk_rejects_an_unknown_channel(client):
    response = client.post(
        "/api/override/bulk", json={"uids": ["tmdb:show:3"], "channels": [4242]}
    )
    assert response.status_code == 400


def test_bulk_rejects_a_bad_mode(client):
    response = client.post(
        "/api/override/bulk",
        json={"uids": ["tmdb:show:3"], "channels": [1051], "mode": "sideways"},
    )
    assert response.status_code == 400


def test_bulk_to_empty_unassigns(client):
    client.post("/api/override/bulk", json={"uids": ["tmdb:show:1"], "channels": []})
    item = client.get("/api/item/tmdb:show:1").json()
    assert item["status"] == STATUS_UNASSIGNED
    assert item["channels"] == []


# -- export preview ------------------------------------------------------


def test_preview_reports_what_would_be_written(client):
    body = client.get("/api/export/preview").json()
    assert body["additions"] == 2, "the two review items are held back"
    assert body["skipped_review"] == 2
    assert body["merged_rows"] == body["original_rows"] + body["additions"]
    assert body["sample"]
    assert body["top_channels"]


def test_preview_with_review_included(client):
    body = client.get("/api/export/preview", params={"include_review": True}).json()
    assert body["additions"] == 4
    assert body["skipped_review"] == 0


def test_preview_writes_nothing(client):
    client.get("/api/export/preview")
    assert client.get("/api/download/merged").status_code == 404


def test_export_then_download(client):
    report = client.post("/api/export", json={"include_review": False}).json()
    assert report["additions"] == 2
    response = client.get("/api/download/merged")
    assert response.status_code == 200
    assert response.text.splitlines()[0] == "Channel Number,Channel Name,Title,Release Year"
    assert client.get("/api/download/additions").status_code == 200


# -- uploading your own channels.csv -------------------------------------


def test_upload_replaces_the_defaults_and_backs_up(client):
    csv_text = (
        "Channel Number,Channel Name,Title,Release Year\n"
        "1068,H.B.Yo Min,My Own Show,2001\n"
        "1064,Netflicks,Another Show,2002\n"
    )
    body = client.post("/api/channels-file", content=csv_text.encode()).json()
    assert body["rows"] == 2
    assert body["previous_rows"] > 2
    assert body["backup"], "the replaced file must be backed up"
    assert client.get("/api/status").json()["defaults"]["rows"] == 2
    # the scan is invalidated because it was diffed against the old file
    assert client.get("/api/library").json()["scanned"] is False


def test_upload_rejects_a_file_with_the_wrong_header(client):
    response = client.post("/api/channels-file", content=b"a,b,c\n1,2,3\n")
    assert response.status_code == 400
    assert "valid channels.csv" in response.json()["detail"]


def test_upload_rejects_an_empty_body(client):
    assert client.post("/api/channels-file", content=b"").status_code == 400


def test_upload_rejects_a_header_with_no_rows(client):
    response = client.post(
        "/api/channels-file", content=b"Channel Number,Channel Name,Title,Release Year\n"
    )
    assert response.status_code == 400
    assert "no rows" in response.json()["detail"]


def test_a_rejected_upload_leaves_the_defaults_alone(client):
    before = client.get("/api/status").json()["defaults"]["rows"]
    client.post("/api/channels-file", content=b"a,b,c\n1,2,3\n")
    assert client.get("/api/status").json()["defaults"]["rows"] == before


# -- split connection tests, logos, posters ------------------------------


def test_server_and_tmdb_tests_are_independent(client):
    """A bad TMDB key must not make the media server look broken, or vice versa."""
    server = client.post("/api/test/server").json()
    tmdb = client.post("/api/test/tmdb").json()
    assert set(server) >= {"ok", "kind"}
    assert "ok" in tmdb
    # Neither reports on the other.
    assert "tmdb" not in server
    assert "sections" not in tmdb


def test_server_test_names_the_selected_backend(client):
    assert client.post("/api/test/server").json()["kind"] == "plex"


def test_channel_logo_falls_back_to_a_generated_badge(client):
    response = client.get("/api/channel-logo/1068")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "1068" in response.text
    assert "cache-control" in response.headers


def test_channel_logo_404s_for_an_unknown_channel(client):
    assert client.get("/api/channel-logo/4242").status_code == 404


def test_every_channel_has_some_logo(client):
    """The UI shows art for all of them, so none may 404."""
    for number in (1001, 1054, 1074, 1113):
        assert client.get(f"/api/channel-logo/{number}").status_code == 200


def test_poster_rejects_a_non_tmdb_path(client):
    for bad in ("../../etc/passwd", "https://evil.example/x.jpg", "/nested/x.jpg"):
        assert client.get("/api/poster", params={"path": bad}).status_code == 400


def test_poster_cache_can_be_cleared(client):
    assert "removed" in client.post("/api/posters/clear").json()


def test_status_reports_pending_changes_and_provenance(client):
    body = client.get("/api/status").json()
    assert "pending" in body
    assert set(body["pending"]) == {"additions", "held_for_review", "overrides"}
    assert "posters" in body
    assert "baseline" in body
    assert "last_export_at" in body


def test_export_records_when_it_happened(client):
    """The client is module-scoped, so assert the stamp moves rather than that
    it starts empty - an earlier test in this module may already have exported."""
    client.post("/api/export", json={"include_review": False})
    first = client.get("/api/status").json()["last_export_at"]
    assert first is not None

    client.post("/api/export", json={"include_review": True})
    second = client.get("/api/status").json()["last_export_at"]
    assert second >= first


def test_uploading_a_lineup_records_its_provenance(client):
    csv_text = (
        "Channel Number,Channel Name,Title,Release Year\n"
        "1068,H.B.Yo Min,Provenance Show,2001\n"
    )
    client.post("/api/channels-file", content=csv_text.encode())
    baseline = client.get("/api/status").json()["baseline"]
    assert baseline["rows"] == 1
    assert baseline["filename"] == "channels.csv"
    assert len(baseline["sha256"]) == 16
    assert baseline["at"] > 0


# -- scan persistence ----------------------------------------------------


def test_a_scan_survives_a_restart(client, tmp_path_factory):
    """A container restart must not cost a re-scan."""
    from nostalgia_line.pipeline import ScanResult

    state = client.server_module.state
    state.persist_result()
    assert state.scan_path.exists()

    restored = ScanResult.load(state.scan_path)
    assert restored is not None
    assert len(restored.entries) == len(SCAN)
    by_title = {e.title: e for e in restored.entries}
    assert by_title["Alpha Show"].channels == [1068]
    assert by_title["Gamma Show"].resolution.needs_review is True
    assert by_title["Zeta Show"].status == STATUS_APP


def test_the_snapshot_carries_its_age(client):
    client.server_module.state.persist_result()
    assert client.get("/api/status").json()["scan_at"] is not None


def test_an_unreadable_snapshot_is_ignored_rather_than_crashing(client, tmp_path):
    from nostalgia_line.pipeline import ScanResult

    broken = tmp_path / "scan.json.gz"
    broken.write_bytes(b"definitely not gzip")
    assert ScanResult.load(broken) is None


def test_a_snapshot_from_another_version_is_discarded(client, tmp_path):
    import gzip
    import json

    from nostalgia_line.pipeline import ScanResult

    path = tmp_path / "scan.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"version": 999, "entries": []}, fh)
    assert ScanResult.load(path) is None


def test_uploading_a_new_lineup_drops_the_snapshot(client):
    state = client.server_module.state
    state.persist_result()
    assert state.scan_path.exists()
    client.post(
        "/api/channels-file",
        content=b"Channel Number,Channel Name,Title,Release Year\n1068,H.B.Yo Min,X,2001\n",
    )
    assert not state.scan_path.exists(), "the old scan was diffed against the old file"


# -- logo endpoints ------------------------------------------------------


def test_logo_listing_reports_coverage(client):
    body = client.get("/api/logos").json()
    assert body["total_channels"] == 96
    assert body["installed_count"] + body["missing_count"] == body["total_channels"]


def test_logo_upload_matches_and_reports(client):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    files = [
        ("files", ("logo_seaw.png", png, "image/png")),
        ("files", ("logo_tnt.png", png, "image/png")),
        ("files", ("nonsense.png", png, "image/png")),
    ]
    body = client.post("/api/logos", files=files).json()
    assert body["imported_count"] == 2
    assert body["unmatched"] == ["nonsense.png"]
    assert {i["channel"] for i in body["imported"]} == {1021, 1027}

    # and the serving endpoint now returns the real file, not a badge
    response = client.get("/api/channel-logo/1021")
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")

    assert client.delete("/api/logos").json()["removed"] >= 2


def test_logo_upload_with_no_files_is_rejected(client):
    assert client.post("/api/logos").status_code == 422


def test_artwork_copied_in_under_its_original_name_is_served(client):
    """Files dropped straight into /config/logos keep their source filename.

    NostalgiaTV names artwork after the real network, so logo_tnt.png must be
    served for T.N.Tea. Listing and serving must agree - an earlier revision had
    two different matchers and disagreed for exactly these channels.
    """
    png = b"\x89PNG\r\n\x1a\n" + b"1" * 64
    logos = client.server_module.state.cfg.path("logos")
    logos.mkdir(parents=True, exist_ok=True)
    for name in ("logo_tnt.png", "logo_metv.png", "logo_hbo.png", "logo_seaw.png"):
        (logos / name).write_bytes(png)

    listing = {row["channel"] for row in client.get("/api/logos").json()["installed"]}
    assert {1027, 1040, 1068, 1021} <= listing

    for number in (1027, 1040, 1068, 1021):
        response = client.get(f"/api/channel-logo/{number}")
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG"), f"channel {number} fell back to a badge"

    client.delete("/api/logos")


def test_a_channel_with_no_artwork_still_gets_a_badge(client):
    response = client.get("/api/channel-logo/1113")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


# -- automatic artwork from TMDB -----------------------------------------


def test_channels_report_where_their_logo_comes_from(client):
    rows = {c["number"]: c for c in client.get("/api/channels").json()["channels"]}
    assert rows[1068]["logo_source"] in ("file", "tmdb", "badge")


def test_a_harvested_network_logo_is_used_automatically(client):
    """No files, no config - the scan already knows AMC's logo, so 1025 gets it."""
    state = client.server_module.state
    state.result.network_logos = {"AMC": "/pmvRmATOCaDykE6JrVoeYxlFHw3.png"}

    rows = {c["number"]: c for c in client.get("/api/channels").json()["channels"]}
    assert rows[1025]["logo_source"] == "tmdb", "AMC maps to 1025 A.M.Sea"

    body = client.get("/api/logos").json()
    assert body["from_tmdb"] >= 1
    state.result.network_logos = {}


def test_supplied_artwork_beats_the_automatic_one(client):
    state = client.server_module.state
    state.result.network_logos = {"AMC": "/pmvRmATOCaDykE6JrVoeYxlFHw3.png"}
    png = b"\x89PNG\r\n\x1a\n" + b"2" * 64
    logos = state.cfg.path("logos")
    logos.mkdir(parents=True, exist_ok=True)
    (logos / "1025.png").write_bytes(png)

    rows = {c["number"]: c for c in client.get("/api/channels").json()["channels"]}
    assert rows[1025]["logo_source"] == "file"
    assert client.get("/api/channel-logo/1025").content.startswith(b"\x89PNG")

    client.delete("/api/logos")
    state.result.network_logos = {}


def test_a_network_with_no_logo_still_falls_back_to_a_badge(client):
    state = client.server_module.state
    state.result.network_logos = {}
    response = client.get("/api/channel-logo/1025")
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_a_read_only_mount_is_read(client, tmp_path, monkeypatch):
    """`- /path/to/logos:/logos:ro` in compose, with nothing copied in."""
    png = b"\x89PNG\r\n\x1a\n" + b"3" * 64
    mount = tmp_path / "mounted-logos"
    mount.mkdir()
    (mount / "logo_hbo.png").write_bytes(png)
    monkeypatch.setenv("NOSTALGIA_LOGO_DIRS", str(mount))

    rows = {c["number"]: c for c in client.get("/api/channels").json()["channels"]}
    assert rows[1068]["logo_source"] == "file"
    assert client.get("/api/channel-logo/1068").content.startswith(b"\x89PNG")
    assert str(mount) in client.get("/api/logos").json()["extra_dirs"]


# -- the saved playlist setting ------------------------------------------


def test_the_playlist_url_is_a_saved_setting(client):
    url = "https://tv.example/channels.m3u?profileId=abc"
    body = client.post("/api/settings", json={"nostalgiatv_m3u_url": url}).json()
    assert body["nostalgiatv_m3u_url"] == url
    assert body["auto_refresh_logos"] is True

    import yaml

    saved = yaml.safe_load(client.server_module.state.config_path.read_text(encoding="utf-8"))
    assert saved["nostalgiatv"]["m3u_url"] == url
    client.post("/api/settings", json={"nostalgiatv_m3u_url": ""})


def test_auto_refresh_can_be_turned_off(client):
    assert client.post("/api/settings", json={"auto_refresh_logos": False}).json()[
        "auto_refresh_logos"
    ] is False
    client.post("/api/settings", json={"auto_refresh_logos": True})


def test_importing_with_no_url_anywhere_is_a_clear_error(client):
    client.post("/api/settings", json={"nostalgiatv_m3u_url": ""})
    response = client.post("/api/logos/from-m3u", json={"url": ""})
    assert response.status_code == 400
    assert "Settings" in response.json()["detail"]


def test_an_uploaded_playlist_file_is_parsed(client):
    """A .m3u names artwork rather than containing it, so unreachable URLs are
    reported as skipped - but the entries must still be matched to channels."""
    m3u = (
        b'#EXTM3U\n'
        b'#EXTINF:-1 tvg-name="Dizzy Channel" '
        b'tvg-logo="http://127.0.0.1:9/api/channels/app_dizzy_channel/logo",Dizzy Channel\n'
        b'http://127.0.0.1:9/stream/app_dizzy_channel\n'
    )
    body = client.post(
        "/api/logos", files=[("files", ("channels.m3u", m3u, "audio/x-mpegurl"))]
    ).json()
    assert body["imported_count"] == 0, "the logo host is unreachable in a test"
    assert body["skipped_count"] == 1, "but the channel was matched and attempted"
    assert body["unmatched_count"] == 0


# -- mapping source and channel contents ---------------------------------


def test_mapping_source_distinguishes_who_placed_a_title(client):
    items = {i["title"]: i for i in client.get("/api/library").json()["items"]}
    assert items["Zeta Show"]["mapping_source"] == "lineup", "came from channels.csv"
    assert items["Alpha Show"]["mapping_source"] == "auto", "placed by the cascade"
    assert items["Epsilon Show"]["mapping_source"] == "none", "nothing placed it"

    client.post("/api/override", json={"uid": "tmdb:show:2", "channels": [1044]})
    assert client.get("/api/item/tmdb:show:2").json()["mapping_source"] == "manual"


def test_library_can_be_filtered_by_mapping_source(client):
    assert client.get("/api/library", params={"source": "lineup"}).json()["total"] == 1
    assert client.get("/api/library", params={"source": "none"}).json()["total"] == 1
    both = client.get("/api/library", params={"source": "lineup,none"}).json()
    assert both["total"] == 2


def test_channel_contents_list_library_titles(client):
    body = client.get("/api/channels/1068/titles").json()
    assert body["channel"]["name"] == "H.B.Yo Min"
    titles = {t["title"] for t in body["titles"]}
    assert "Alpha Show" in titles
    row = next(t for t in body["titles"] if t["title"] == "Alpha Show")
    assert row["uid"] == "tmdb:show:1"
    assert row["mapping_source"] == "auto"
    assert row["reason"]


def test_channel_contents_separate_titles_you_do_not_own(client):
    """The lineup file places titles that are not in the library. They explain a
    channel's count but there is nothing to edit, so they are listed apart."""
    body = client.get("/api/channels/1068/titles").json()
    assert body["counts"]["not_in_library"] > 0
    assert body["counts"]["in_library"] == len(body["titles"])
    assert all("uid" not in row for row in body["not_in_library"])


def test_channel_contents_show_a_titles_other_channels(client):
    client.post("/api/override", json={"uid": "tmdb:show:1", "channels": [1068, 1044]})
    row = next(
        t for t in client.get("/api/channels/1068/titles").json()["titles"]
        if t["uid"] == "tmdb:show:1"
    )
    assert [c["number"] for c in row["other_channels"]] == [1044]


def test_channel_contents_404_for_an_unknown_channel(client):
    assert client.get("/api/channels/4242/titles").status_code == 404


# -- what really aired on the station ------------------------------------


def test_network_catalog_marks_what_you_already_have(client, monkeypatch):
    """The point of the view: separate what the station ran from what you own."""
    state = client.server_module.state
    state.result.network_ids = {"HBO": 49}

    async def fake_catalog(self, network_id, pages=2):
        assert network_id == 49
        return [
            {"tmdb_id": 1, "name": "Alpha Show", "poster_path": "/a.jpg",
             "first_air_date": "2020-01-01", "vote_average": 8.1, "overview": ""},
            {"tmdb_id": 999, "name": "A Show You Lack", "poster_path": "/b.jpg",
             "first_air_date": "2019-01-01", "vote_average": 7.4, "overview": ""},
            {"tmdb_id": 2, "name": "Beta Show", "poster_path": "/c.jpg",
             "first_air_date": "2021-01-01", "vote_average": 7.9, "overview": ""},
        ]

    monkeypatch.setattr(
        "nostalgia_line.server.TMDBClient.network_catalog", fake_catalog, raising=True
    )
    body = client.get("/api/channels/1068/network-catalog").json()

    assert body["network"] == "HBO"
    assert body["counts"] == {"total": 3, "in_library": 2, "missing": 1}

    rows = {t["name"]: t for t in body["titles"]}
    assert rows["Alpha Show"]["in_library"] is True
    assert rows["Alpha Show"]["uid"] == "tmdb:show:1"
    assert rows["Alpha Show"]["elsewhere"] is False, "it is on 1068 already"
    assert rows["A Show You Lack"]["in_library"] is False
    assert rows["A Show You Lack"]["uid"] is None
    # Beta Show is in the library but assigned to Netflicks, not this channel.
    assert rows["Beta Show"]["in_library"] is True
    assert rows["Beta Show"]["elsewhere"] is True
    state.result.network_ids = {}


def test_network_catalog_says_so_when_no_station_maps_here(client):
    state = client.server_module.state
    state.result.network_ids = {}
    body = client.get("/api/channels/1099/network-catalog").json()
    assert body["titles"] == []
    assert "no real-world station" in body["note"]


def test_network_catalog_404s_for_an_unknown_channel(client):
    assert client.get("/api/channels/4242/network-catalog").status_code == 404


def test_the_busiest_mapped_network_is_chosen_not_the_shortest_named(client, monkeypatch):
    """Cartoon Net maps from both Cartoon Network and YTV. Picking on name length
    handed it YTV's catalogue and a 1-of-40 match; pick on evidence instead."""
    state = client.server_module.state
    state.result.network_ids = {"YTV": 77, "Cartoon Network": 56}

    # Two of the library's titles on 1006 came from Cartoon Network, none from YTV.
    for uid, network in (("tmdb:show:1", "Cartoon Network"), ("tmdb:show:2", "Cartoon Network")):
        entry = next(e for e in state.result.entries if e.uid == uid)
        entry.network = network
        entry.resolution.assignments[0].channel_number = 1006

    chosen = {}

    async def fake_catalog(self, network_id, pages=2):
        chosen["id"] = network_id
        return []

    monkeypatch.setattr(
        "nostalgia_line.server.TMDBClient.network_catalog", fake_catalog, raising=True
    )
    body = client.get("/api/channels/1006/network-catalog").json()
    assert chosen["id"] == 56, "should follow the evidence, not the shorter name"
    assert body["network"] == "Cartoon Network"
    state.result.network_ids = {}


# -- accuracy (HANDOVER item 1) -------------------------------------------


def seed_accuracy_fixture(client):
    """Two lineup-placed shows with cached TMDB records: one the cascade will
    agree with, one it will not."""
    from nostalgia_line.tmdb import TMDBCache, TMDBSeries

    state = client.server_module.state
    cache = TMDBCache(state.cfg.path(state.cfg.data.cache_dir))
    cache.put("series", 7, TMDBSeries(tmdb_id=7, networks=["HBO"]).to_dict())
    cache.put("series", 8, TMDBSeries(tmdb_id=8, networks=["HBO"]).to_dict())
    cache.flush()
    state.result.entries += [
        make_entry("tmdb:show:7", "Eta Show", "HBO", [1068], status=STATUS_APP),
        make_entry("tmdb:show:8", "Theta Show", "HBO", [1044], status=STATUS_APP),
    ]


def test_accuracy_reports_per_rule_agreement_with_sample_counts(client):
    seed_accuracy_fixture(client)
    body = client.get("/api/accuracy").json()

    assert body["scanned"] is True
    # Zeta Show is lineup-placed but has no cached record; Eta and Theta probe.
    assert body["ground_truth"] == 3
    assert body["sampled"] == 2
    assert body["agree"] == 1
    assert body["skipped"]["no_cached_record"] == 1
    network = next(r for r in body["by_rule"] if r["rule"] == "network")
    assert network["agree"] == 1 and network["n"] == 2
    assert network["sufficient"] is False, "n=2 must not render a verdict"
    assert body["min_samples"] == 20

    assert body["disagreements_total"] == 1
    miss = body["disagreements"][0]
    assert miss["title"] == "Theta Show"
    assert miss["ours"][0]["number"] == 1068
    assert miss["theirs"][0]["number"] == 1044


def test_accuracy_compares_all_three_routing_modes(client):
    seed_accuracy_fixture(client)
    body = client.get("/api/accuracy").json()
    assert [m["mode"] for m in body["modes"]] == ["streaming_first", "hybrid", "themed"]
    streaming = body["modes"][0]
    themed = body["modes"][2]
    assert streaming["sampled"] == 2
    assert themed["sampled"] == 0, "themed skips the network step; these records offer nothing else"
    assert all("disagreements" not in m for m in body["modes"])


def test_accuracy_is_cached_per_scan(client):
    seed_accuracy_fixture(client)
    first = client.get("/api/accuracy").json()
    second = client.get("/api/accuracy").json()
    assert second["computed_at"] == first["computed_at"], "a poll must not recompute"

    # A routing change invalidates the cache.
    client.post("/api/settings", json={"routing_mode": "hybrid"})
    third = client.get("/api/accuracy").json()
    assert third["computed_at"] != first["computed_at"]
    assert third["mode"] == "hybrid"
    client.post("/api/settings", json={"routing_mode": "streaming_first"})


def test_accuracy_without_a_scan_says_so(client):
    client.server_module.state.result = None
    assert client.get("/api/accuracy").json() == {"scanned": False}


# -- the delta between scans (HANDOVER item 2) ----------------------------


def test_the_library_filters_to_what_the_last_scan_changed(client):
    state = client.server_module.state
    by_uid = {e.uid: e for e in state.result.entries}
    by_uid["tmdb:show:1"].delta = "new"
    by_uid["tmdb:show:2"].delta = "changed"
    by_uid["tmdb:show:3"].delta = "unchanged"
    state.result.previous_scan_at = 123.0

    data = client.get("/api/library", params={"since_last_scan": True}).json()
    assert {i["uid"] for i in data["items"]} == {"tmdb:show:1", "tmdb:show:2"}
    assert all(i["delta"] in ("new", "changed") for i in data["items"])

    stats = client.get("/api/status").json()["stats"]
    assert stats["since_last_scan"] == 2
    assert stats["delta"] == {
        "tracked": True, "new": 1, "changed": 1, "departed": 0, "since": 123.0,
    }


def test_the_workflow_names_the_arrivals(client):
    state = client.server_module.state
    next(e for e in state.result.entries if e.uid == "tmdb:show:1").delta = "new"
    state.result.previous_scan_at = 123.0
    body = client.get("/api/workflow").json()
    scan_step = next(s for s in body["steps"] if s["key"] == "scan")
    assert "1 title(s) are new or moved" in scan_step["detail"]


def test_a_completed_scan_derives_the_delta_and_persists_it(client, monkeypatch):
    """The worker owns the diff: previous in-memory scan vs the new one, then
    the result (delta included) is written to disk so a restart keeps it."""
    import copy as _copy
    import time as _time

    server = client.server_module
    state = server.state
    previous_finished = state.result.finished_at = 555.0

    arrived = ScanResult(
        entries=_copy.deepcopy(SCAN), sections=["Shows"], finished_at=999.0
    )
    arrived.entries[0].uid = "tmdb:show:41"
    arrived.entries[0].tmdb_id = 41

    async def fake_run_scan(*args, **kwargs):
        return arrived

    monkeypatch.setattr(server, "run_scan", fake_run_scan)
    assert client.post("/api/scan").json()["started"] is True
    for _ in range(100):
        if not client.get("/api/status").json()["scanning"]:
            break
        _time.sleep(0.05)

    assert state.result is arrived
    assert state.result.entries[0].delta == "new"
    assert state.result.previous_scan_at == previous_finished
    assert [d["uid"] for d in state.result.departed] == ["tmdb:show:1"]
    assert state.accuracy is None, "accuracy was measured against the previous scan"

    reloaded = ScanResult.load(state.scan_path)
    assert reloaded is not None
    assert reloaded.entries[0].delta == "new"
    assert reloaded.previous_scan_at == previous_finished


# -- scheduled scans (HANDOVER item 2) ------------------------------------


def test_schedule_settings_round_trip_and_do_not_stale_the_results(client):
    body = client.post(
        "/api/settings",
        json={"schedule": {"enabled": True, "interval_hours": 6, "quiet_start": 23, "quiet_end": 7}},
    ).json()
    assert body["schedule"] == {
        "enabled": True, "interval_hours": 6.0, "quiet_start": 23, "quiet_end": 7,
    }
    assert client.get("/api/status").json()["stale"] is False, (
        "a schedule change routes nothing differently, so nothing is out of date"
    )
    client.post("/api/settings", json={"schedule": {}})
    assert client.get("/api/settings").json()["schedule"]["enabled"] is False


def test_schedule_validation_rejects_nonsense_without_saving_it(client):
    bad = [
        {"enabled": True, "interval_hours": 0},
        {"enabled": True, "quiet_start": 5},
        {"quiet_start": 26, "quiet_end": 2},
    ]
    for payload in bad:
        assert client.post("/api/settings", json={"schedule": payload}).status_code == 400, payload
    assert client.server_module.state.cfg.schedule.enabled is False


def test_status_reports_when_the_next_scan_is_due(client):
    import time as _time

    state = client.server_module.state
    client.post("/api/settings", json={"schedule": {"enabled": True, "interval_hours": 6}})
    state.result.finished_at = _time.time() - 100
    body = client.get("/api/status").json()["schedule"]
    assert body["enabled"] is True
    assert abs(body["next_due_at"] - (state.result.finished_at + 6 * 3600)) < 1
    assert body["waiting_on_quiet"] is False
    client.post("/api/settings", json={"schedule": {}})


def test_quiet_window_wraps_midnight(client):
    quiet = client.server_module._in_quiet_window
    assert quiet(23, 22, 8) and quiet(3, 22, 8) and quiet(22, 22, 8)
    assert not quiet(12, 22, 8) and not quiet(8, 22, 8)
    assert quiet(3, 2, 4) and not quiet(5, 2, 4)
    assert not quiet(3, None, None), "no window configured"
    assert not quiet(3, 5, 5), "equal bounds disable the window"


def test_the_scheduler_decision_ladder(client):
    """Every reason not to scan, in order - and the one case that scans."""
    server = client.server_module
    state = server.state
    sched = state.cfg.schedule
    state.result.finished_at = 1_000.0

    sched.enabled = False
    assert server._schedule_decision(9_000.0, 12) == "disabled"

    sched.enabled = True
    sched.interval_hours = 1
    sched.quiet_start = sched.quiet_end = None

    key = state.cfg.tmdb.api_key
    state.cfg.tmdb.api_key = ""
    assert server._schedule_decision(9_000.0, 12) == "unconfigured"
    state.cfg.tmdb.api_key = key

    class Running:
        def done(self):
            return False

    state.scan_task = Running()
    assert server._schedule_decision(9_000.0, 12) == "scan_running"
    state.scan_task = None

    assert server._schedule_decision(1_000.0 + 1800, 12) == "not_due"

    sched.quiet_start, sched.quiet_end = 22, 8
    assert server._schedule_decision(9_000.0, 23) == "quiet"
    assert server._schedule_decision(9_000.0, 12) == "scan"

    # Never scanned at all: due immediately once enabled and configured.
    state.result = None
    assert server._schedule_decision(9_000.0, 12) == "scan"

    sched.enabled = False
    sched.quiet_start = sched.quiet_end = None


# -- station mapping page -------------------------------------------------


def test_networks_report_a_station_claim(client):
    """The mapping page shows custom channels first; a network claimed by one
    must say so instead of showing the map route the cascade would ignore."""
    created = client.post(
        "/api/stations", json={"name": "Claimer", "source_networks": ["Weird Service"]}
    ).json()
    try:
        body = client.get("/api/networks").json()
        weird = next(n for n in body["networks"] if n["network"] == "Weird Service")
        assert weird["status"] == "station"
        assert weird["channel_number"] == created["number"]
        assert weird["channel_name"] == "Claimer"
    finally:
        client.delete(f"/api/stations/{created['number']}")


def test_library_filters_by_channel(client):
    body = client.get("/api/library", params={"channel": 1068}).json()
    assert {i["title"] for i in body["items"]} == {"Alpha Show"}
    assert client.get("/api/library", params={"channel": 1044}).json()["total"] == 0
