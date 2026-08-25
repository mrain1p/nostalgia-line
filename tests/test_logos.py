"""Channel artwork import.

Filenames come from wherever the user got the art, so the matcher has to be
generous about naming while never guessing wrong. Real NostalgiaTV filenames are
used as fixtures.
"""
import io
import zipfile

import pytest

from nostalgia_line.channels import load_network_map
from nostalgia_line.logos import LogoImporter, normalize

from .conftest import DATA


@pytest.fixture
def importer(catalog, tmp_path):
    return LogoImporter(catalog, tmp_path, load_network_map(DATA / "network_map.csv"))


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


# -- filename matching ---------------------------------------------------


def test_normalize_strips_prefixes_and_punctuation():
    assert normalize("logo_seaw.png") == "seaw"
    assert normalize("H.B.Yo Min.PNG") == "hbyomin"
    assert normalize("channel-1068.webp") == "1068"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("1068.png", 1068),          # by channel number
        ("H.B.Yo Min.png", 1068),    # by channel name
        ("Munchyroll.webp", 1071),
        ("LACKLUSTER.PNG", 1054),
        ("app_adult_skim.png", 1051),  # by app key
        ("logo_seaw.png", 1021),       # NostalgiaTV's own convention
        ("logo_benchmark_hits.png", 1112),
    ],
)
def test_matches_parody_names(importer, filename, expected):
    assert importer.match(filename) == expected


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("logo_tnt.png", 1027),        # TNT -> T.N.Tea
        ("logo_metv.png", 1040),       # MeTV -> Watch-On-Repeat
        ("logo_pbs_kids.png", 1010),   # PBS Kids -> P.B.Yes Tots
        ("logo_hbo.png", 1068),
        ("logo_cartoon_network.png", 1006),
        ("logo_nickelodeon.png", 1008),
    ],
)
def test_matches_real_network_names(importer, filename, expected):
    """NostalgiaTV files its artwork under the network being parodied."""
    assert importer.match(filename) == expected


def test_an_unrelated_filename_matches_nothing(importer):
    assert importer.match("random_thing.png") is None
    assert importer.match("IMG_4821.png") is None


def test_without_a_network_map_parody_names_still_work(catalog, tmp_path):
    plain = LogoImporter(catalog, tmp_path)
    assert plain.match("logo_seaw.png") == 1021
    assert plain.match("logo_tnt.png") is None, "network matching needs the map"


# -- importing -----------------------------------------------------------


def test_files_are_stored_under_the_channel_number(importer, tmp_path):
    report = importer.import_files([("logo_seaw.png", PNG)])
    assert report.imported[0]["channel"] == 1021
    assert report.imported[0]["stored_as"] == "1021.png"
    assert (tmp_path / "1021.png").read_bytes() == PNG


def test_reimporting_in_another_format_replaces_the_first(importer, tmp_path):
    importer.import_files([("logo_seaw.png", PNG)])
    importer.import_files([("SeaW.webp", PNG)])
    assert not (tmp_path / "1021.png").exists()
    assert (tmp_path / "1021.webp").exists()


def test_unmatched_files_are_reported_not_dropped(importer):
    report = importer.import_files([("random_thing.png", PNG)])
    assert report.imported == []
    assert report.unmatched == ["random_thing.png"]


def test_non_images_are_skipped_with_a_reason(importer):
    report = importer.import_files([("notes.txt", b"hello"), ("logo_seaw.png", PNG)])
    assert report.imported_count if False else len(report.imported) == 1
    assert report.skipped[0]["why"] == "not an image"


def test_oversized_and_empty_files_are_skipped(importer):
    report = importer.import_files(
        [("logo_seaw.png", b"x" * (5 * 1024 * 1024)), ("logo_faux.png", b"")]
    )
    reasons = {s["why"] for s in report.skipped}
    assert reasons == {"larger than 4 MB", "empty"}
    assert report.imported == []


def test_a_zip_of_artwork_imports(importer, tmp_path):
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as z:
        z.writestr("logos/logo_seaw.png", PNG)
        z.writestr("logos/logo_tnt.png", PNG)
        z.writestr("logos/readme.txt", b"ignore me")
    report = importer.import_zip(blob.getvalue())
    assert {r["channel"] for r in report.imported} == {1021, 1027}
    assert (tmp_path / "1021.png").exists()
    assert any(s["why"] == "not an image" for s in report.skipped)


def test_zip_entries_cannot_escape_the_directory(importer, tmp_path):
    """A crafted archive must not write outside /config/logos."""
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as z:
        z.writestr("../../evil.png", PNG)
    report = importer.import_zip(blob.getvalue())
    assert report.imported == []
    assert not (tmp_path.parent / "evil.png").exists()


def test_a_corrupt_zip_is_reported(importer):
    report = importer.import_zip(b"this is not a zip")
    assert report.imported == []
    assert report.skipped[0]["why"] == "not a readable zip"


def test_installed_and_clear(importer):
    importer.import_files([("logo_seaw.png", PNG), ("logo_tnt.png", PNG)])
    assert set(importer.installed()) == {1021, 1027}
    assert importer.clear() == 2
    assert importer.installed() == {}


# -- M3U import ----------------------------------------------------------

M3U = '''#EXTM3U url-tvg="https://tv.example/xmltv.xml"
#EXTINF:-1 tvg-id="101" tvg-chno="101" tvg-name="Dizzy Channel" tvg-logo="https://tv.example/api/channels/app_dizzy_channel/logo" group-title="NostalgiaTV",Dizzy Channel
https://tv.example/stream/app_dizzy_channel
#EXTINF:-1 tvg-id="167" tvg-chno="167" tvg-name="H.B.Yo Min" tvg-logo="https://tv.example/api/channels/app_hb_yo_min/logo" group-title="NostalgiaTV",H.B.Yo Min
https://tv.example/stream/app_hb_yo_min
#EXTINF:-1 tvg-id="9001" tvg-name="Bloomberg TV" tvg-logo="https://tv.example/api/channels/plex_bloomberg/logo" group-title="Live",Bloomberg TV
https://tv.example/stream/plex_bloomberg
'''


def test_m3u_parsing_extracts_what_matters():
    from nostalgia_line.logos import parse_m3u

    entries = parse_m3u(M3U)
    assert len(entries) == 3
    assert entries[0].name == "Dizzy Channel"
    assert entries[0].number == 101
    assert entries[0].app_key == "app_dizzy_channel"
    assert entries[0].logo_url.endswith("/app_dizzy_channel/logo")


def test_m3u_entries_match_by_app_key(importer):
    """The app key is in the logo URL and is definitive - names can collide."""
    from nostalgia_line.logos import parse_m3u

    entries = parse_m3u(M3U)
    assert importer.match_channel(entries[0]) == 1001
    assert importer.match_channel(entries[1]) == 1068


def test_live_tv_entries_do_not_match_a_channel(importer):
    """A NostalgiaTV playlist also carries Plex Live TV; those are not ours."""
    from nostalgia_line.logos import parse_m3u

    assert importer.match_channel(parse_m3u(M3U)[2]) is None


def test_an_m3u_entry_matches_by_name_without_an_app_key(importer):
    from nostalgia_line.logos import M3UChannel

    assert importer.match_channel(M3UChannel(name="Munchyroll")) == 1071


def test_a_non_http_playlist_url_is_refused(importer):
    import asyncio

    report = asyncio.run(importer.import_from_m3u("file:///etc/passwd"))
    assert report.imported == []
    assert "http" in report.skipped[0]["why"]
