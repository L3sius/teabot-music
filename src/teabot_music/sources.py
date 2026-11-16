"""Where the music comes from: YouTube, YouTube Music and SoundCloud links, Spotify tracks, and searches.

- `find` turns what a member typed into `Found` tracks without reading their audio: a link, a YouTube search, or
  a YouTube playlist or SoundCloud set, up to a limit. A Spotify track is found on YouTube by its title and artist,
  read from its public page, since Spotify gives bots no audio.
- `open` returns a track's audio as 20 ms Opus packets for `ctx.voice.play`, from `start_s` seconds in. YouTube
  already serves Opus at 48 kHz, which goes out as it is; anything else is re-encoded with PyAV's libopus.
- `prefetch` resolves a track's stream ahead of time. YouTube refuses a stream for a few seconds after handing out
  its address (S8), so resolving the next track while one plays hides that wait for every track after the first.
- Every one of them blocks for seconds, so the plugin calls them in a thread (D33).
"""

import html
import re
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Protocol, cast

import av
import yt_dlp
from av.container import InputContainer
from av.error import FFmpegError, HTTPForbiddenError
from yt_dlp.utils import DownloadError

YOUTUBE = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
SOUNDCLOUD = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
SPOTIFY = {"open.spotify.com"}
FRAME = 960  # 20 ms at 48 kHz, what Discord takes per packet
# A resolved stream address lasts hours; a prefetched one is used within a track or two.
STREAM_TTL_S = 1800
WEB_TIMEOUT_S = 10
SEARCH = "ytsearch1:"


class SourceError(Exception):
    """Why a track cannot be found or played, in words for the member."""


@dataclass(frozen=True)
class Found:
    """A track to queue: its title, the page it was found at, and its length when known."""

    title: str
    url: str
    duration_s: int | None


class Source(Protocol):
    """Finds tracks and opens their audio. The plugin's tests replace it with a fake."""

    def find(self, query: str, /, limit: int = 1) -> list[Found]:
        """The tracks a query names: one, or for a playlist link up to `limit`, leaving out ones that cannot play."""
        ...

    def open(self, url: str, /, start_s: int = 0) -> Iterator[bytes]: ...

    def prefetch(self, url: str, /) -> None: ...


def target_of(query: str) -> str:
    """What to ask yt-dlp for: a supported link as it is, a Spotify link as a search, and words as a search.
    Other links are refused, so members cannot point the bot at any site yt-dlp can read."""
    text = query.strip()
    if not re.match(r"^https?://", text, re.IGNORECASE):
        return SEARCH + text
    parsed = urllib.parse.urlsplit(text)
    host = (parsed.hostname or "").lower()
    if host in YOUTUBE or host in SOUNDCLOUD:
        return text
    if host in SPOTIFY and parsed.path.startswith("/track/"):
        return text
    if host in SPOTIFY:
        raise SourceError("Only single Spotify tracks can be played, not albums or playlists.")
    raise SourceError("Links can be from YouTube, YouTube Music, SoundCloud or Spotify.")


def is_spotify(target: str) -> bool:
    """Whether `target_of` gave a Spotify track, which has no audio to play: its page names the song to search for."""
    return urllib.parse.urlsplit(target).hostname in SPOTIFY


def is_playlist(target: str) -> bool:
    """A YouTube playlist page, or a SoundCloud set. A video opened from a playlist plays on its own."""
    parsed = urllib.parse.urlsplit(target)
    host = (parsed.hostname or "").lower()
    if host in YOUTUBE:
        return parsed.path.rstrip("/") == "/playlist"
    return host in SOUNDCLOUD and "/sets/" in parsed.path


def spotify_search(page: str) -> str:
    """Words to search YouTube for a Spotify track, from its page's title and description, which name the song and
    the artist, as in `Rick Astley · Song · 1987`."""
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    description = re.search(r'<meta property="og:description" content="([^"]*)"', page)
    if title is None:
        raise SourceError("That Spotify track could not be read.")
    artist = html.unescape(description.group(1)).split("·")[0].strip() if description else ""
    return f"{html.unescape(title.group(1))} {artist}".strip()


def _first(info: dict[str, Any]) -> dict[str, Any]:
    entries = info.get("entries")
    if entries is None:
        return info
    for entry in entries:
        if entry:
            return cast(dict[str, Any], entry)
    raise SourceError("Nothing was found for that.")


def tracks_from(info: dict[str, Any], limit: int) -> list[Found]:
    """A playlist's tracks, up to `limit`, leaving out live streams and the private or deleted videos YouTube lists
    without a length."""
    tracks: list[Found] = []
    for entry in cast(list[dict[str, Any] | None], info.get("entries") or []):
        if len(tracks) >= limit:
            break
        if not entry or not entry.get("duration"):
            continue
        try:
            tracks.append(found_from(entry))
        except SourceError:
            continue
    if not tracks:
        raise SourceError("That playlist has nothing that can be played.")
    return tracks


def found_from(info: dict[str, Any]) -> Found:
    """The track yt-dlp describes, refusing a live stream, which never ends."""
    if info.get("is_live") or info.get("live_status") == "is_live":
        raise SourceError("Live streams cannot be queued.")
    url = info.get("webpage_url") or info.get("url")
    if not url:
        raise SourceError("Nothing was found for that.")
    duration = info.get("duration")
    return Found(str(info.get("title") or url), str(url), int(duration) if duration else None)


class YtDlpSource:
    """Tracks through yt-dlp, which needs Deno to solve YouTube's challenges, and audio through PyAV."""

    def __init__(self) -> None:
        self._streams: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def find(self, query: str, /, limit: int = 1) -> list[Found]:
        target = target_of(query)
        if is_spotify(target):
            # The plugin reads a Spotify page through ctx.http, and passes on words to search for.
            raise SourceError("A Spotify link is searched for by its title first.")
        playlist = is_playlist(target)
        options: dict[str, Any] = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
        # A playlist is read only as far as the limit, so a long one costs no more than a short one.
        options |= {"playlistend": limit} if playlist else {"noplaylist": True}
        try:
            with yt_dlp.YoutubeDL(cast(Any, options)) as ydl:
                info = cast(dict[str, Any], ydl.extract_info(target, download=False))
        except DownloadError as exc:
            raise SourceError("That could not be found, or is not available.") from exc
        if playlist:
            return tracks_from(info, limit)
        return [found_from(_first(info))]

    def _resolve(self, url: str) -> dict[str, Any]:
        with self._lock:
            kept = self._streams.pop(url, None)
        if kept is not None and kept[0] > time.monotonic():
            return kept[1]
        options = {"format": "bestaudio[acodec=opus]/bestaudio", "quiet": True, "no_warnings": True}
        asked = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(cast(Any, options | {"noplaylist": True})) as ydl:
                info = _first(cast(dict[str, Any], ydl.extract_info(url, download=False)))
        except DownloadError as exc:
            raise SourceError("That track is no longer available.") from exc
        time.sleep(stream_wait(info, time.monotonic() - asked))
        return info

    def prefetch(self, url: str, /) -> None:
        """Resolve a track's stream now, so `open` finds it ready. A failure is left for `open` to report."""
        try:
            info = self._resolve(url)
        except SourceError:
            return
        now = time.monotonic()
        with self._lock:
            # A prefetched track that was removed or skipped is never opened, so expired ones go here.
            self._streams = {u: kept for u, kept in self._streams.items() if kept[0] > now}
            self._streams[url] = (now + STREAM_TTL_S, info)

    def open(self, url: str, /, start_s: int = 0) -> Iterator[bytes]:
        info = self._resolve(url)
        try:
            container = _open(info)
        except HTTPForbiddenError:
            # A stream refused once is usually let through a moment later; a fresh address settles the rest.
            time.sleep(1)
            container = _reopen(self._resolve(url))
        except FFmpegError as exc:
            raise SourceError("That track's audio could not be opened.") from exc
        return packets(container, start_s)


def _reopen(info: dict[str, Any]) -> InputContainer:
    try:
        return _open(info)
    except FFmpegError as exc:
        raise SourceError("YouTube refused that track's audio. Try it again in a minute.") from exc


def stream_wait(info: dict[str, Any], since_s: float) -> float:
    """How long to wait before reading the stream: YouTube answers 403 until its `available_at`, a few seconds
    after handing it out (S8). yt-dlp records when it asked as `epoch`, so the wait is the gap between the two,
    less the time already spent since, plus a little margin."""
    available, asked = info.get("available_at"), info.get("epoch")
    if not available or not asked:
        return 0.0
    return max(0.0, float(available) - float(asked) - since_s + 0.3)


def _open(info: dict[str, Any]) -> InputContainer:
    sent = cast(dict[str, str], info.get("http_headers") or {})
    headers = "".join(f"{k}: {v}\r\n" for k, v in sent.items())
    options = {"headers": headers, "reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "5"}
    return av.open(str(info["url"]), options=options, timeout=WEB_TIMEOUT_S)


def packets(container: InputContainer, start_s: int = 0) -> Iterator[bytes]:
    """The container's audio as 20 ms Opus packets, from `start_s` seconds in, closing it when done."""
    try:
        stream = container.streams.audio[0]
        if start_s > 0:
            container.seek(start_s * 1_000_000)
        codec = stream.codec_context
        if codec.name == "opus" and codec.sample_rate == 48000:
            for packet in container.demux(stream):
                if packet.size:
                    yield bytes(packet)
        else:
            yield from _encoded(container, stream)
    finally:
        container.close()


def _encoded(container: InputContainer, stream: Any) -> Iterator[bytes]:
    encoder = cast(Any, av.CodecContext.create("libopus", "w"))
    encoder.sample_rate, encoder.layout, encoder.format = 48000, "stereo", "s16"
    encoder.bit_rate, encoder.time_base = 128_000, Fraction(1, 48000)
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
    fifo = av.AudioFifo()
    for frame in cast(Iterator[av.AudioFrame], container.decode(stream)):
        # The resampler refuses the uneven timestamps an Opus pre-skip leaves, so they are dropped (S8).
        frame.pts = None
        for out in resampler.resample(frame):
            fifo.write(out)
        while (chunk := fifo.read(FRAME)) is not None:
            chunk.pts = None
            for packet in encoder.encode(chunk):
                yield bytes(packet)
