"""HTTP surface, driven through a real FastAPI test client.

The module builds its own throwaway config so the project's own config.yaml,
stations.json and state.json are never touched by a test run.
"""
import importlib
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from .conftest import DATA


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("api")
    config = {
        "plex": {"url": "http://fake:32400", "token": "", "libraries": []},
        "tmdb": {"api_key": "", "rate_limit": 50},
        "routing": {
            "mode": "streaming_first",
            "multi_channel": "sanctioned_pairs_only",
            "orphan_network": "parent_fallback",
        },
        "output": {"additions_only": "additions.csv", "merged": "merged.csv"},
        "data": {
            "channels_csv": str(DATA / "channels.csv"),
            "network_map": str(DATA / "network_map.csv"),
            "orphan_networks": str(DATA / "orphan_networks.csv"),
            "channel_catalog": str(DATA / "channel_catalog.csv"),
            "cache_dir": str(workdir / "cache"),
            "state_file": str(workdir / "state.json"),
        },
        "server": {"host": "127.0.0.1", "port": 8777},
    }
    config_path = workdir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    previous = os.environ.get("NOSTALGIA_CONFIG")
    os.environ["NOSTALGIA_CONFIG"] = str(config_path)
    # Env vars must not leak a real token into an isolated test run.
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


# -- status and settings -------------------------------------------------


def test_status_reports_reference_data(client):
    body = client.get("/api/status").json()
    assert body["defaults"]["channels"] == 113
    assert body["defaults"]["rows"] == 4651
    assert body["configured"] is False
    assert body["stats"] is None


def test_settings_round_trip_without_leaking_secrets(client):
    body = client.get("/api/settings").json()
    assert body["plex_token_set"] is False
    assert "plex_token" not in body
    assert "tmdb_api_key" not in body

    updated = client.post(
        "/api/settings",
        json={"plex_url": "http://10.0.0.5:32400", "plex_token": "secret", "tmdb_api_key": "key"},
    ).json()
    assert updated["plex_url"] == "http://10.0.0.5:32400"
    assert updated["plex_token_set"] is True
    assert updated["tmdb_api_key_set"] is True
    assert "secret" not in str(updated)


def test_settings_rejects_an_invalid_routing_mode(client):
    response = client.post("/api/settings", json={"routing_mode": "sideways"})
    assert response.status_code == 400


def test_settings_persist_to_disk(client):
    client.post("/api/settings", json={"routing_mode": "hybrid"})
    saved = yaml.safe_load(client.server_module.state.config_path.read_text(encoding="utf-8"))
    assert saved["routing"]["mode"] == "hybrid"
    client.post("/api/settings", json={"routing_mode": "streaming_first"})


# -- channels ------------------------------------------------------------


def test_channels_listed_before_any_scan(client):
    channels = client.get("/api/channels").json()["channels"]
    assert len(channels) == 113
    hbo = next(c for c in channels if c["number"] == 1068)
    assert hbo["name"] == "H.B.Yo Min"
    assert hbo["existing"] > 0
    assert hbo["added"] == 0


def test_music_channels_are_marked_no_content(client):
    channels = client.get("/api/channels").json()["channels"]
    tune = next(c for c in channels if c["number"] == 1074)
    assert tune["accepts_content"] is False
    assert tune["empty"] is False


# -- library and review before a scan ------------------------------------


def test_library_is_empty_but_well_formed_before_a_scan(client):
    body = client.get("/api/library").json()
    assert body["scanned"] is False
    assert body["items"] == []


def test_review_is_empty_before_a_scan(client):
    assert client.get("/api/review").json()["total"] == 0


def test_scan_refuses_when_unconfigured(client):
    client.post("/api/settings", json={"plex_url": "http://fake:32400"})
    client.server_module.state.cfg.plex.token = ""
    client.server_module.state.cfg.tmdb.api_key = ""
    response = client.post("/api/scan")
    assert response.status_code == 400
    assert "token" in response.json()["detail"].lower()


def test_export_refuses_before_a_scan(client):
    response = client.post("/api/export", json={"include_review": False})
    assert response.status_code == 400


def test_override_refuses_before_a_scan(client):
    response = client.post("/api/override", json={"uid": "tmdb:show:1", "channels": [1068]})
    assert response.status_code == 400


def test_download_404s_before_an_export(client):
    assert client.get("/api/download/merged").status_code == 404
    assert client.get("/api/download/nonsense").status_code == 404


# -- custom stations -----------------------------------------------------


def test_station_create_list_and_delete(client):
    created = client.post(
        "/api/stations",
        json={"name": "Retro Gaming", "source_networks": ["G4", "TechTV"], "mode": "claim"},
    ).json()
    assert created["number"] >= 1200
    assert created["name"] == "Retro Gaming"

    listing = client.get("/api/stations").json()
    assert any(s["name"] == "Retro Gaming" for s in listing["stations"])
    assert "hbo" in listing["known_networks"]

    # it becomes a real routing target
    channels = client.get("/api/channels").json()["channels"]
    assert any(c["number"] == created["number"] for c in channels)

    assert client.delete(f"/api/stations/{created['number']}").status_code == 200
    assert not any(
        s["number"] == created["number"] for s in client.get("/api/stations").json()["stations"]
    )


def test_station_cannot_squat_a_stock_channel(client):
    response = client.post(
        "/api/stations", json={"name": "Fake HBO", "number": 1068, "source_networks": ["G4"]}
    )
    assert response.status_code == 400
    assert "H.B.Yo Min" in response.json()["detail"]


def test_station_rejects_a_bad_mode_over_http(client):
    response = client.post(
        "/api/stations", json={"name": "Bad", "source_networks": ["G4"], "mode": "sideways"}
    )
    assert response.status_code == 400


def test_deleting_an_unknown_station_404s(client):
    assert client.delete("/api/stations/9999").status_code == 404


def test_station_without_sources_is_reported_as_a_problem(client):
    created = client.post("/api/stations", json={"name": "Pointless"}).json()
    problems = client.get("/api/stations").json()["problems"]
    assert any("no sources" in p for p in problems)
    client.delete(f"/api/stations/{created['number']}")


# -- static UI -----------------------------------------------------------


def test_the_ui_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Nostalgia Line" in response.text


def test_static_assets_are_served(client):
    assert client.get("/app.js").status_code == 200
    assert client.get("/styles.css").status_code == 200
