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
            assignments=[
                Assignment(c, f"Ch{c}", "network", conf, "test") for c in channels
            ],
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
