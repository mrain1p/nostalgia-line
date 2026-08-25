import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nostalgia_line.cascade import Cascade  # noqa: E402
from nostalgia_line.channels import (  # noqa: E402
    ChannelCatalog,
    DefaultAssignments,
    load_network_map,
    load_orphan_networks,
)
from nostalgia_line.stations import StationBook  # noqa: E402
from nostalgia_line.tmdb import TMDBSeries  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture(scope="session")
def catalog() -> ChannelCatalog:
    return ChannelCatalog.load(DATA / "channel_catalog.csv")


@pytest.fixture(scope="session")
def defaults() -> DefaultAssignments:
    return DefaultAssignments.load(DATA / "channels.csv")


@pytest.fixture(scope="session")
def network_map() -> dict:
    return load_network_map(DATA / "network_map.csv")


@pytest.fixture(scope="session")
def orphan_map() -> dict:
    return load_orphan_networks(DATA / "orphan_networks.csv")


@pytest.fixture
def cascade(catalog, defaults, network_map, orphan_map) -> Cascade:
    return Cascade(
        catalog=catalog,
        defaults=defaults,
        network_map=network_map,
        orphan_map=orphan_map,
        stations=StationBook(),
    )


def series(**kwargs) -> TMDBSeries:
    """Build a TMDBSeries with sane defaults for the fields a test does not care about."""
    kwargs.setdefault("tmdb_id", 1)
    kwargs.setdefault("name", "Test Show")
    kwargs.setdefault("first_air_date", "2015-01-01")
    return TMDBSeries(**kwargs)
