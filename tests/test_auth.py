"""Optional access control.

Off by default on purpose: turning it on silently would lock people out of their
own tool on upgrade. These pin both halves - that it stays open until asked, and
that it genuinely closes once asked.
"""
import importlib
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from nostalgia_line.auth import Sessions, hash_password, verify_password

from .conftest import DATA


# -- hashing --------------------------------------------------------------


def test_a_password_verifies_against_its_own_hash():
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored) is True
    assert verify_password("wrong horse", stored) is False


def test_the_password_is_not_recoverable_from_the_hash():
    stored = hash_password("hunter2")
    assert "hunter2" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_the_same_password_hashes_differently_each_time():
    assert hash_password("same") != hash_password("same"), "salted"


def test_a_malformed_hash_never_verifies():
    for junk in ("", "nonsense", "md5$salt$deadbeef", "pbkdf2_sha256$only-two"):
        assert verify_password("anything", junk) is False


# -- sessions -------------------------------------------------------------


def test_a_session_is_valid_until_it_expires():
    sessions = Sessions(ttl=60)
    token = sessions.issue()
    assert sessions.valid(token) is True
    assert sessions.valid("not-a-token") is False
    assert sessions.valid(None) is False


def test_an_expired_session_is_rejected_and_forgotten():
    sessions = Sessions(ttl=-1)
    assert sessions.valid(sessions.issue()) is False


def test_revoking_and_clearing():
    sessions = Sessions()
    token = sessions.issue()
    sessions.revoke(token)
    assert sessions.valid(token) is False
    other = sessions.issue()
    sessions.clear()
    assert sessions.valid(other) is False


# -- the gate over HTTP ---------------------------------------------------


@pytest.fixture
def client(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("auth")
    (workdir / "channels.csv").write_bytes((DATA / "channels.csv").read_bytes())
    config = {
        "plex": {"url": "http://fake:32400", "token": "t", "libraries": []},
        "tmdb": {"api_key": "k", "rate_limit": 50},
        "routing": {
            "mode": "streaming_first",
            "multi_channel": "sanctioned_pairs_only",
            "orphan_network": "parent_fallback",
        },
        "output": {"additions_only": "a.csv", "merged": "m.csv"},
        "data": {
            "channels_csv": str(workdir / "channels.csv"),
            "network_map": str(DATA / "network_map.csv"),
            "orphan_networks": str(DATA / "orphan_networks.csv"),
            "channel_catalog": str(DATA / "channel_catalog.csv"),
            "cache_dir": str(workdir / "cache"),
            "state_file": str(workdir / "state.json"),
        },
        "server": {"host": "127.0.0.1", "port": 8777, "password_hash": ""},
    }
    path = workdir / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    previous = os.environ.get("NOSTALGIA_CONFIG")
    os.environ["NOSTALGIA_CONFIG"] = str(path)
    os.environ.pop("NOSTALGIA_PASSWORD", None)

    from nostalgia_line import server

    importlib.reload(server)
    with TestClient(server.app) as test_client:
        test_client.server_module = server
        yield test_client

    if previous is None:
        os.environ.pop("NOSTALGIA_CONFIG", None)
    else:
        os.environ["NOSTALGIA_CONFIG"] = previous


def test_it_starts_open(client):
    body = client.get("/api/auth/status").json()
    assert body["enabled"] is False
    assert body["authenticated"] is True
    assert client.get("/api/channels").status_code == 200


def test_setting_a_password_closes_the_api(client):
    assert client.post("/api/auth/password", json={"password": "letmein!"}).json()["enabled"]
    assert client.get("/api/auth/status").json()["enabled"] is True
    client.cookies.clear()
    assert client.get("/api/channels").status_code == 401
    client.post("/api/auth/password", json={"password": ""})


def test_the_right_password_opens_it_again(client):
    client.post("/api/auth/password", json={"password": "letmein!"})
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "letmein!"}).status_code == 200
    assert client.get("/api/channels").status_code == 200
    client.post("/api/auth/password", json={"password": ""})


def test_status_stays_reachable_so_the_healthcheck_survives(client):
    client.post("/api/auth/password", json={"password": "letmein!"})
    client.cookies.clear()
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").status_code == 200
    client.post("/api/auth/password", json={"password": ""})


def test_logging_out_closes_the_session(client):
    client.post("/api/auth/password", json={"password": "letmein!"})
    client.post("/api/auth/login", json={"password": "letmein!"})
    assert client.get("/api/channels").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/channels").status_code == 401
    client.post("/api/auth/login", json={"password": "letmein!"})
    client.post("/api/auth/password", json={"password": ""})


def test_a_short_password_is_refused(client):
    assert client.post("/api/auth/password", json={"password": "abc"}).status_code == 400


def test_clearing_the_password_reopens_it(client):
    client.post("/api/auth/password", json={"password": "letmein!"})
    client.post("/api/auth/login", json={"password": "letmein!"})
    client.post("/api/auth/password", json={"password": ""})
    client.cookies.clear()
    assert client.get("/api/channels").status_code == 200


def test_the_password_hash_is_never_sent_to_the_browser(client):
    client.post("/api/auth/password", json={"password": "letmein!"})
    client.post("/api/auth/login", json={"password": "letmein!"})
    text = client.get("/api/settings").text + client.get("/api/status").text
    assert "pbkdf2" not in text
    assert "letmein" not in text
    client.post("/api/auth/password", json={"password": ""})


def test_a_password_in_the_environment_locks_the_app(client, monkeypatch):
    """Setting it in compose takes effect without touching config.yaml."""
    monkeypatch.setenv("NOSTALGIA_PASSWORD", "from-compose")
    client.cookies.clear()
    assert client.get("/api/auth/status").json()["enforced_by_env"] is True
    assert client.get("/api/channels").status_code == 401
    assert client.post("/api/auth/login", json={"password": "from-compose"}).status_code == 200
    assert client.get("/api/channels").status_code == 200


def test_a_password_in_the_environment_cannot_be_changed_from_the_ui(client, monkeypatch):
    """Compose can enforce one; the UI must not quietly override it.

    Signing in first is the point - the refusal has to come from the handler
    rather than from the gate, or it would be indistinguishable from being
    locked out."""
    monkeypatch.setenv("NOSTALGIA_PASSWORD", "from-compose")
    client.cookies.clear()
    client.post("/api/auth/login", json={"password": "from-compose"})
    response = client.post("/api/auth/password", json={"password": "something-else"})
    assert response.status_code == 400
    assert "NOSTALGIA_PASSWORD" in response.json()["detail"]
