"""teabot-music, through the real loader, migrations and call path, with a fake source and fake voice (D54)."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field

import discord
import pytest
from teabot.testing import ActionFailed, FakeMember, FakeVoiceChannel, HarnessFactory, PluginHarness

import teabot_music
from teabot_music import Cfg, Queued, pause, play, plugin, queue, remove, resume, skip, stop
from teabot_music.sources import Found, SourceError

MINUTE = 60


@dataclass
class FakeSource:
    """Tracks by what a member types. Each opened track yields three packets naming its URL and start."""

    tracks: dict[str, list[Found]] = field(default_factory=dict[str, list[Found]])
    broken: set[str] = field(default_factory=set[str])
    opened: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    prefetched: list[str] = field(default_factory=list[str])

    def add(self, query: str, title: str, duration_s: int | None = 3 * MINUTE) -> Found:
        found = Found(title, f"https://youtu.be/{query}", duration_s)
        self.tracks[query] = [found]
        return found

    def add_playlist(self, query: str, lengths: list[int]) -> None:
        self.tracks[query] = [
            Found(f"{query} {n}", f"https://youtu.be/{query}{n}", s) for n, s in enumerate(lengths, 1)
        ]

    def find(self, query: str, /, limit: int = 1) -> list[Found]:
        if query not in self.tracks:
            raise SourceError("Nothing was found for that.")
        return self.tracks[query][:limit]

    def open(self, url: str, /, start_s: int = 0) -> Iterator[bytes]:
        self.opened.append((url, start_s))
        if url in self.broken:
            raise SourceError("That track is no longer available.")
        return iter([f"{url}@{start_s}:{n}".encode() for n in range(3)])

    def prefetch(self, url: str, /) -> None:
        self.prefetched.append(url)


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> FakeSource:
    fake = FakeSource()
    fake.add("shanty", "Sea Shanty 2")
    fake.add("harmony", "Harmony")
    fake.add("autumn", "Autumn Voyage")
    monkeypatch.setattr(teabot_music, "SOURCE", fake)
    return fake


async def in_voice(teabot_harness: HarnessFactory) -> tuple[PluginHarness, FakeVoiceChannel]:
    harness = await teabot_harness(plugin)
    lounge = harness.guild.add_channel(FakeVoiceChannel(name="lounge"))
    await harness.voice(harness.member, joined=lounge)
    return harness, lounge


async def background() -> None:
    """Let prefetches started in the background finish."""
    await asyncio.gather(*teabot_music._background)  # pyright: ignore[reportPrivateUsage]


def playing(harness: PluginHarness) -> list[bytes]:
    return harness.voice_connection.take()


async def test_play_joins_the_members_channel_and_starts_the_track(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    assert await harness.call(play, query="shanty") == Queued(
        title="Sea Shanty 2", position=0, duration_s=180
    )
    await harness.run_jobs()
    assert harness.voice_connection.channel is lounge
    assert playing(harness) == [
        b"https://youtu.be/shanty@0:0",
        b"https://youtu.be/shanty@0:1",
        b"https://youtu.be/shanty@0:2",
    ]


async def test_the_next_track_is_queued_and_resolved_ahead(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    queued = await harness.call(play, query="harmony")
    assert queued.position == 1
    await background()
    assert source.prefetched == ["https://youtu.be/harmony"]


async def test_each_track_plays_after_the_one_before(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.call(play, query="harmony")
    await harness.run_jobs()
    await harness.end_track()
    assert [url for url, _ in source.opened] == ["https://youtu.be/shanty", "https://youtu.be/harmony"]
    assert playing(harness)[0] == b"https://youtu.be/harmony@0:0"
    await harness.end_track()
    assert harness.voice_connection.current is None
    assert harness.voice_connection.is_connected()


async def test_a_track_that_cannot_be_opened_is_passed_over(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    source.broken.add("https://youtu.be/shanty")
    await harness.call(play, query="shanty")
    await harness.call(play, query="harmony")
    await harness.run_jobs()
    assert playing(harness)[0] == b"https://youtu.be/harmony@0:0"
    failed = "SELECT error FROM plugin_music.tracks WHERE title = 'Sea Shanty 2'"
    assert await harness.admin.fetchval(failed) == "That track is no longer available."


async def test_skip_moves_on_to_the_next_track(teabot_harness: HarnessFactory, source: FakeSource) -> None:
    harness, _ = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.call(play, query="harmony")
    await harness.run_jobs()
    assert await harness.call(skip) == "Skipped **Sea Shanty 2**."
    await harness.run_jobs()
    assert playing(harness)[0] == b"https://youtu.be/harmony@0:0"


async def test_pause_and_resume(teabot_harness: HarnessFactory, source: FakeSource) -> None:
    harness, _ = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    await harness.call(pause)
    assert harness.voice_connection.paused
    assert (await harness.call(queue)).paused
    await harness.call(resume)
    assert not harness.voice_connection.paused


async def test_stop_clears_the_queue_and_leaves(teabot_harness: HarnessFactory, source: FakeSource) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.call(play, query="harmony")
    await harness.run_jobs()
    await harness.call(stop)
    assert harness.guild.me not in lounge.members
    assert (await harness.call(queue)).render() == "Nothing is playing, and nothing is queued."


async def test_the_queue_shows_what_plays_now_and_next(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    for query in ("shanty", "harmony", "autumn"):
        await harness.call(play, query=query)
    await harness.run_jobs()
    await harness.advance(seconds=65)
    assert (await harness.call(queue)).render() == (
        "Now playing: **Sea Shanty 2** (1:05 / 3:00)\n1. Harmony (3:00)\n2. Autumn Voyage (3:00)"
    )


async def test_after_a_restart_the_track_carries_on_from_where_it_was(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.call(play, query="harmony")
    await harness.run_jobs()
    await harness.advance(seconds=42)
    await harness.restart_voice()
    assert harness.voice_connection.channel is lounge
    assert source.opened[-1] == ("https://youtu.be/shanty", 42)
    await harness.end_track()
    assert source.opened[-1] == ("https://youtu.be/harmony", 0)


async def test_a_restart_with_nothing_left_to_play_gives_the_connection_up(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    await harness.call(stop)
    await harness.restart_voice()
    assert harness.guild.me not in lounge.members


async def test_a_member_outside_voice_is_asked_to_join_one(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness = await teabot_harness(plugin)
    with pytest.raises(ActionFailed, match="Join a voice channel first"):
        await harness.call(play, query="shanty")


async def test_nothing_found_says_so(teabot_harness: HarnessFactory, source: FakeSource) -> None:
    harness, _ = await in_voice(teabot_harness)
    with pytest.raises(ActionFailed, match="Nothing was found"):
        await harness.call(play, query="silence")
    assert harness.voice_connector is not None and harness.voice_connector.joins == []


async def test_a_track_over_the_servers_limit_is_refused(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    source.add("epic", "Ten hours of rain", duration_s=600 * MINUTE)
    with pytest.raises(ActionFailed, match="limit of 30 minutes"):
        await harness.call(play, query="epic")


async def test_queueing_into_another_channel_is_refused(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    raids = harness.guild.add_channel(FakeVoiceChannel(name="raids"))
    with pytest.raises(ActionFailed, match="I am playing in"):
        await harness.call(play, query="harmony", channel=raids)


async def with_dj(harness: PluginHarness) -> tuple[FakeMember, FakeMember]:
    dj = harness.guild.add_role("DJ")
    await harness.configure(Cfg(dj_role=dj.id))
    listener = harness.guild.add_member(discord.Permissions.none(), name="listener")
    spinner = harness.guild.add_member(discord.Permissions.none(), name="spinner", roles=[dj])
    return listener, spinner


async def test_with_a_dj_role_only_djs_skip_others_tracks(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    listener, spinner = await with_dj(harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    with pytest.raises(ActionFailed, match="Only members with"):
        await harness.as_member(listener).call(skip)
    assert await harness.as_member(spinner).call(skip) == "Skipped **Sea Shanty 2**."


async def test_with_a_dj_role_a_requester_may_skip_their_own_track(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    listener, _ = await with_dj(harness)
    listener.voice_channel = lounge
    await harness.as_member(listener).call(play, query="shanty")
    await harness.run_jobs()
    assert await harness.as_member(listener).call(skip) == "Skipped **Sea Shanty 2**."


async def test_remove_takes_a_track_out_of_the_queue(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    for query in ("shanty", "harmony", "autumn"):
        await harness.call(play, query=query)
    await harness.run_jobs()
    assert await harness.call(remove, number=1) == "Took **Harmony** out of the queue."
    assert [t.title for t in (await harness.call(queue)).up_next] == ["Autumn Voyage"]
    with pytest.raises(ActionFailed, match="Nothing is queued at that number"):
        await harness.call(remove, number=5)


async def test_with_a_dj_role_only_djs_remove_others_tracks(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    listener, _ = await with_dj(harness)
    await harness.call(play, query="shanty")
    await harness.call(play, query="harmony")
    with pytest.raises(ActionFailed, match="Only members with"):
        await harness.as_member(listener).call(remove, number=1)


async def test_a_playlist_queues_its_tracks_up_to_the_limit(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    await harness.configure(Cfg(max_playlist=3))
    source.add_playlist("mix", [200, 9000, 180, 240, 100])
    queued = await harness.call(play, query="mix")
    assert queued == Queued(title="mix 1", position=0, duration_s=200, more=1, left_out=1)
    assert (
        queued.render()
        == "Starting **mix 1** and 1 more in a moment. Left out 1 over this server's length limit."
    )
    await harness.run_jobs()
    assert [t.title for t in (await harness.call(queue)).up_next] == ["mix 3"]


async def test_when_everyone_leaves_the_bot_leaves_after_a_while(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    await harness.voice(harness.member, left=lounge)
    assert harness.guild.me in lounge.members
    await harness.advance(minutes=2)
    await harness.run_jobs()
    assert harness.guild.me not in lounge.members
    assert (await harness.call(queue)).render() == "Nothing is playing, and nothing is queued."


async def test_someone_coming_back_keeps_the_bot(teabot_harness: HarnessFactory, source: FakeSource) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    await harness.voice(harness.member, left=lounge)
    await harness.voice(harness.member, joined=lounge)
    await harness.advance(minutes=2)
    await harness.run_jobs()
    assert harness.guild.me in lounge.members


async def test_with_no_wait_set_the_bot_leaves_at_once(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.configure(Cfg(alone_minutes=0))
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    await harness.voice(harness.member, left=lounge)
    assert harness.guild.me not in lounge.members


async def test_with_nothing_left_to_play_the_bot_leaves_once_idle(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    await harness.end_track()
    await harness.advance(minutes=4)
    await harness.run_jobs()
    assert harness.guild.me in lounge.members
    await harness.advance(minutes=1)
    await harness.run_jobs()
    assert harness.guild.me not in lounge.members


async def test_a_song_queued_while_idle_keeps_the_bot(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, lounge = await in_voice(teabot_harness)
    await harness.call(play, query="shanty")
    await harness.run_jobs()
    await harness.end_track()
    await harness.advance(minutes=3)
    await harness.call(play, query="harmony")
    await harness.run_jobs()
    await harness.advance(minutes=3)
    await harness.run_jobs()
    assert harness.guild.me in lounge.members
    assert playing(harness)[0] == b"https://youtu.be/harmony@0:0"


SPOTIFY_TRACK = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


async def test_a_spotify_link_is_searched_for_by_its_page(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    page = (
        '<meta property="og:title" content="Never Gonna Give You Up"/>'
        '<meta property="og:description" content="Rick Astley · Song · 1987"/>'
    )
    harness.web.reply(SPOTIFY_TRACK, text=page)
    source.add("Never Gonna Give You Up Rick Astley", "Never Gonna Give You Up")
    queued = await harness.call(play, query=SPOTIFY_TRACK)
    assert queued.title == "Never Gonna Give You Up"
    assert [r.url for r in harness.web.requests] == [SPOTIFY_TRACK]


async def test_a_spotify_page_that_cannot_be_read_is_refused(
    teabot_harness: HarnessFactory, source: FakeSource
) -> None:
    harness, _ = await in_voice(teabot_harness)
    harness.web.reply(SPOTIFY_TRACK, status=404)
    with pytest.raises(ActionFailed, match="could not be read"):
        await harness.call(play, query=SPOTIFY_TRACK)
