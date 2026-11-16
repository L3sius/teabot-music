"""The sources, without the network: which links are taken, Spotify pages, and PyAV's packets from real files."""

import math
import struct
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import av
import certifi
import pytest
from av.error import HTTPForbiddenError

from teabot_music import sources
from teabot_music.sources import (
    SourceError,
    YtDlpSource,
    found_from,
    is_playlist,
    packets,
    spotify_search,
    stream_options,
    stream_wait,
    target_of,
    tracks_from,
)

SECOND = 50  # packets of 20 ms


@pytest.mark.parametrize(
    "query",
    [
        "https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
        "https://youtu.be/kJQP7kiw5Fk",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://soundcloud.com/forss/flickermood",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    ],
)
def test_supported_links_are_asked_for_as_they_are(query: str) -> None:
    assert target_of(query) == query


def test_words_are_a_youtube_search() -> None:
    assert target_of("  sea shanty 2 ") == "ytsearch1:sea shanty 2"


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("https://example.com/song.mp3", "Links can be from YouTube"),
        ("http://169.254.169.254/latest/meta-data", "Links can be from YouTube"),
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "Only single Spotify tracks"),
    ],
)
def test_other_links_are_refused(query: str, reason: str) -> None:
    with pytest.raises(SourceError, match=reason):
        target_of(query)


def test_a_spotify_track_is_searched_by_its_title_and_artist() -> None:
    page = (
        '<meta property="og:title" content="Don&#x27;t Stop Me Now"/>'
        '<meta property="og:description" content="Queen · Jazz · Song · 1978"/>'
    )
    assert spotify_search(page) == "Don't Stop Me Now Queen"


def test_a_spotify_page_without_a_title_is_refused() -> None:
    with pytest.raises(SourceError, match="could not be read"):
        spotify_search("<html></html>")


def test_a_live_stream_is_refused() -> None:
    with pytest.raises(SourceError, match="Live streams"):
        found_from({"title": "radio", "webpage_url": "https://youtu.be/x", "is_live": True})


def test_what_yt_dlp_finds_becomes_a_track() -> None:
    found = found_from({"title": "Harmony", "webpage_url": "https://youtu.be/h", "duration": 201.4})
    assert (found.title, found.url, found.duration_s) == ("Harmony", "https://youtu.be/h", 201)


def opus_file(path: Path, seconds: int) -> Path:
    """An Ogg Opus file of a 440 Hz tone, as YouTube's audio is Opus at 48 kHz."""
    with av.open(str(path), "w") as opened:
        out = cast(Any, opened)
        stream = out.add_stream("libopus", rate=48000)
        stream.layout = "stereo"
        resampler = av.AudioResampler(format=stream.format.name, layout="stereo", rate=48000)
        for n in range(seconds * SECOND):
            tone = (int(8000 * math.sin(2 * math.pi * 440 * (n * 960 + i) / 48000)) for i in range(960))
            frame = av.AudioFrame(format="s16", layout="stereo", samples=960)
            frame.planes[0].update(b"".join(struct.pack("<hh", v, v) for v in tone))
            frame.sample_rate = 48000
            frame.pts = n * 960
            frame.time_base = Fraction(1, 48000)
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted):
                    out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return path


def wav_file(path: Path, seconds: int) -> Path:
    """A 44.1 kHz WAV file, which is not Opus, so it is re-encoded."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44100)
        tone = (int(8000 * math.sin(2 * math.pi * 440 * n / 44100)) for n in range(44100 * seconds))
        out.writeframes(b"".join(struct.pack("<hh", v, v) for v in tone))
    return path


def test_opus_at_48_khz_is_passed_through_packet_by_packet(tmp_path: Path) -> None:
    audio = list(packets(av.open(str(opus_file(tmp_path / "tone.opus", 2)))))
    assert abs(len(audio) - 2 * SECOND) <= 2
    assert all(0 < len(p) < 1500 for p in audio)


def test_other_audio_is_re_encoded_as_20_ms_opus_packets(tmp_path: Path) -> None:
    audio = list(packets(av.open(str(wav_file(tmp_path / "tone.wav", 2)))))
    assert abs(len(audio) - 2 * SECOND) <= 3


def test_a_track_can_start_partway_through(tmp_path: Path) -> None:
    whole = list(packets(av.open(str(opus_file(tmp_path / "tone.opus", 3)))))
    later = list(packets(av.open(str(tmp_path / "tone.opus")), start_s=2))
    assert len(later) < len(whole) - SECOND


def test_a_stream_is_waited_for_until_youtube_lets_it_be_read() -> None:
    assert stream_wait({"available_at": 1006, "epoch": 1000}, since_s=2) == pytest.approx(4.3)
    assert stream_wait({"available_at": 1006, "epoch": 1000}, since_s=9) == 0
    assert stream_wait({"epoch": 1000}, since_s=0) == 0


@pytest.mark.parametrize(
    ("url", "playlist"),
    [
        ("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", True),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", False),
        ("https://soundcloud.com/forss/sets/soulhack", True),
        ("https://soundcloud.com/forss/flickermood", False),
    ],
)
def test_a_playlist_link_is_told_from_a_track_in_one(url: str, playlist: bool) -> None:
    assert is_playlist(url) is playlist


def test_a_playlist_leaves_out_what_cannot_play_and_stops_at_the_limit() -> None:
    entries = [
        {"title": "one", "url": "https://youtu.be/1", "duration": 100},
        None,
        {"title": "[Private video]", "url": "https://youtu.be/2", "duration": None},
        {"title": "radio", "url": "https://youtu.be/3", "duration": 1, "live_status": "is_live"},
        {"title": "two", "url": "https://youtu.be/4", "duration": 200},
        {"title": "three", "url": "https://youtu.be/5", "duration": 300},
    ]
    assert [t.title for t in tracks_from({"entries": entries}, limit=2)] == ["one", "two"]
    with pytest.raises(SourceError, match="nothing that can be played"):
        tracks_from({"entries": [None]}, limit=5)


def no_wait(_: float) -> None:
    pass


def refuse(info: dict[str, Any]) -> Any:
    raise HTTPForbiddenError(-858797304, "Forbidden", str(info["url"]))


def test_a_stream_refused_twice_is_a_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved: list[str] = []

    def resolve(_: YtDlpSource, url: str) -> dict[str, Any]:
        resolved.append(url)
        return {"url": "https://rr1.googlevideo.com/videoplayback"}

    monkeypatch.setattr(YtDlpSource, "_resolve", resolve)
    monkeypatch.setattr(sources, "_open", refuse)
    monkeypatch.setattr(sources.time, "sleep", no_wait)
    with pytest.raises(SourceError, match="refused"):
        YtDlpSource().open("https://youtu.be/x")
    assert resolved == ["https://youtu.be/x", "https://youtu.be/x"]


def test_streams_are_opened_with_certifis_certificates() -> None:
    options = stream_options({"http_headers": {"User-Agent": "x"}})
    assert options["ca_file"] == certifi.where()
    assert options["headers"] == "User-Agent: x\r\n"
