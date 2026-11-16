"""teabot-music: members queue songs, and the bot plays them in a voice channel (D53). Imports only `teabot.sdk`.

- `music.play` queues a YouTube, YouTube Music, SoundCloud or Spotify link, a YouTube playlist or SoundCloud set,
  or a YouTube search, joining the member's voice channel first.
- `music.skip`, `music.pause`, `music.resume`, `music.remove` and `music.stop` control what plays. With a DJ role
  set, only its members may, besides a track's requester for their own track.
- `music.queue` shows what plays now and next.
- The `play_next` job starts the next track, `on_track_end` starts one after each track, and `on_voice_resume`
  carries on after a restart from where the track had got to.
- The bot leaves on its own when nobody but bots is left with it, after `alone_minutes`, and when nothing has
  played for `idle_minutes`.
"""

import asyncio
from collections.abc import Awaitable
from datetime import timedelta
from typing import Annotated

from pydantic import BaseModel, Field
from teabot.sdk import READ, ActionError, Context, Plugin, RoleRef, UntrustedText, VoiceChannel

from teabot_music.sources import Source, SourceError, YtDlpSource, is_spotify, spotify_search, target_of


class Cfg(BaseModel):
    dj_role: RoleRef | None = Field(default=None, description="Who may skip, pause and stop others' tracks")
    max_minutes: int = Field(default=30, ge=1, le=600, description="The longest track that can be queued")
    max_playlist: int = Field(default=25, ge=1, le=100, description="How many tracks a playlist link queues")
    alone_minutes: int = Field(
        default=2, ge=0, le=60, description="How long the bot stays once nobody else is in its channel"
    )
    idle_minutes: int = Field(
        default=5, ge=1, le=120, description="How long the bot waits with nothing to play"
    )


plugin = Plugin("music", config=Cfg, hosts=["open.spotify.com"])
type Ctx = Context[Cfg]

SOURCE: Source = YtDlpSource()
"""Where tracks come from. Tests replace it with a fake."""
UP_NEXT = 10
KEEP_FINISHED = 50

# One track start at a time in each server. A server's voice connection lives in one process, so a lock in it is
# enough; the queue itself is in the database.
_starting: dict[int, asyncio.Lock] = {}
_background: set[asyncio.Future[None]] = set()


class Track(BaseModel):
    id: int
    title: UntrustedText
    url: str
    duration_s: int | None
    requested_by: int
    resume_at_s: int
    elapsed_s: int


class Queued(BaseModel):
    """What `play` queued: its first track, where that is in line, and for a playlist how many more came with
    it and how many were left out for being too long."""

    title: UntrustedText
    position: int
    duration_s: int | None
    more: int = 0
    left_out: int = 0

    def render(self) -> str:
        queued = f"**{self.title}**" + (f" and {self.more} more" if self.more else "")
        said = (
            f"Starting {queued} in a moment."
            if self.position == 0
            else f"Queued {queued}, number {self.position} in line."
        )
        if self.left_out:
            said += f" Left out {self.left_out} over this server's length limit."
        return said


class QueueView(BaseModel):
    playing: Track | None
    paused: bool
    up_next: list[Track]
    more: int

    def render(self) -> str:
        if self.playing is None and not self.up_next:
            return "Nothing is playing, and nothing is queued."
        lines: list[str] = []
        if self.playing is not None:
            mark = "Paused" if self.paused else "Now playing"
            lines.append(
                f"{mark}: **{self.playing.title}** {clock(self.playing.elapsed_s, self.playing.duration_s)}"
            )
        lines += [f"{n}. {t.title} {clock(0, t.duration_s)}" for n, t in enumerate(self.up_next, start=1)]
        if self.more:
            lines.append(f"…and {self.more} more.")
        return "\n".join(lines)


def minutes(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def clock(elapsed_s: int, duration_s: int | None) -> str:
    if duration_s is None:
        return f"({minutes(elapsed_s)})" if elapsed_s else ""
    return f"({minutes(elapsed_s)} / {minutes(duration_s)})" if elapsed_s else f"({minutes(duration_s)})"


COLUMNS = """
id, title, url, duration_s, requested_by, resume_at_s,
CASE WHEN state = 'playing'
    THEN greatest(0, floor(extract(epoch FROM coalesce(paused_at, now()) - started_at)))::int ELSE 0 END AS elapsed_s
"""
ADD = """
INSERT INTO tracks (guild_id, title, url, duration_s, requested_by) VALUES ($1, $2, $3, $4, $5) RETURNING id
"""
AHEAD = "SELECT count(*) FROM tracks WHERE guild_id = $1 AND state IN ('queued', 'playing') AND id < $2"
PLAYING = f"SELECT {COLUMNS} FROM tracks WHERE guild_id = $1 AND state = 'playing'"
PAUSED = "SELECT paused_at IS NOT NULL FROM tracks WHERE guild_id = $1 AND state = 'playing'"
WAITING = f"SELECT {COLUMNS} FROM tracks WHERE guild_id = $1 AND state = 'queued' ORDER BY id LIMIT $2"
WAITING_COUNT = "SELECT count(*) FROM tracks WHERE guild_id = $1 AND state = 'queued'"
NTH = f"SELECT {COLUMNS} FROM tracks WHERE guild_id = $1 AND state = 'queued' ORDER BY id OFFSET $2 LIMIT 1"
START = """
UPDATE tracks SET state = 'playing', started_at = now() - make_interval(secs => resume_at_s), paused_at = NULL
WHERE guild_id = $1 AND id = $2
"""
FINISH = """
UPDATE tracks SET state = $3, error = $4, resume_at_s = 0
WHERE guild_id = $1 AND id = $2 AND state IN ('queued', 'playing')
"""
CLEAR = "UPDATE tracks SET state = 'cleared' WHERE guild_id = $1 AND state IN ('queued', 'playing')"
PAUSE = "UPDATE tracks SET paused_at = now() WHERE guild_id = $1 AND state = 'playing' AND paused_at IS NULL"
RESUME = """
UPDATE tracks SET started_at = started_at + (now() - paused_at), paused_at = NULL
WHERE guild_id = $1 AND state = 'playing' AND paused_at IS NOT NULL
"""
# After a restart the playing track goes back to the front of the queue, to carry on from where it had got to.
REQUEUE = """
UPDATE tracks SET state = 'queued', paused_at = NULL,
    resume_at_s = greatest(0, floor(extract(epoch FROM coalesce(paused_at, now()) - started_at)))::int
WHERE guild_id = $1 AND state = 'playing'
"""
PRUNE = """
DELETE FROM tracks WHERE guild_id = $1 AND state NOT IN ('queued', 'playing')
AND id < (SELECT max(id) FROM tracks WHERE guild_id = $1) - $2
"""


def _lock(guild_id: int) -> asyncio.Lock:
    return _starting.setdefault(guild_id, asyncio.Lock())


def _in_background(work: Awaitable[None]) -> None:
    task = asyncio.ensure_future(work)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _prefetch_next(ctx: Ctx) -> None:
    """Resolve the next track's stream while this one plays, so it starts without YouTube's wait (S8)."""
    following = await ctx.db.fetch(Track, WAITING, ctx.guild.id, 1)
    if following:
        _in_background(asyncio.to_thread(SOURCE.prefetch, following[0].url))


async def start_next(ctx: Ctx) -> Track | None:
    """Play the first queued track, unless something is playing or the bot is not in voice for this plugin. A
    track that cannot be opened is marked failed, and the one after it tried."""
    async with _lock(ctx.guild.id):
        state = ctx.voice.state
        if state is None or state.plugin_id != plugin.id or state.track is not None:
            return None
        while (waiting := await ctx.db.fetch(Track, WAITING, ctx.guild.id, 1)) != []:
            track = waiting[0]
            try:
                audio = await asyncio.to_thread(SOURCE.open, track.url, track.resume_at_s)
            except Exception as exc:  # a broken track must not stop the queue
                reason = str(exc) if isinstance(exc, SourceError) else f"{type(exc).__name__}: {exc}"
                await ctx.db.execute(FINISH, ctx.guild.id, track.id, "failed", reason)
                continue
            await ctx.db.execute(START, ctx.guild.id, track.id)
            await ctx.voice.play(audio, track=str(track.id))
            await _prefetch_next(ctx)
            return track
        return None


async def run_queue(ctx: Ctx) -> None:
    """Start the next track; with none to start and nothing playing, check back once the idle time is up."""
    if await start_next(ctx) is not None:
        return
    state = ctx.voice.state
    if state is not None and state.plugin_id == plugin.id and state.track is None:
        async with ctx.db.transaction() as tx:
            await tx.schedule(leave_if_idle.bind(), at=ctx.now + timedelta(minutes=ctx.config.idle_minutes))


async def _give_up(ctx: Ctx) -> None:
    await ctx.db.execute(CLEAR, ctx.guild.id)
    await ctx.voice.leave()


def _ours(ctx: Ctx) -> bool:
    state = ctx.voice.state
    return state is not None and state.plugin_id == plugin.id


async def _searchable(ctx: Ctx, query: str) -> str:
    """`query`, or for a Spotify track the song and artist its public page names, since Spotify has no audio to
    play. The page comes through `ctx.http`, which keeps to the hosts the plugin declares."""
    target = target_of(query)
    if not is_spotify(target):
        return query
    page = await ctx.http.get(target)
    if not page.ok:
        raise SourceError("That Spotify track could not be read.")
    return spotify_search(page.text)


async def _members_channel(ctx: Ctx) -> VoiceChannel | None:
    member = await ctx.guild.get_member(ctx.actor.user_id)
    found = member.voice_channel_id if member is not None else None
    return ctx.guild.get_voice_channel(found) if found is not None else None


async def _check_dj(ctx: Ctx, requested_by: int | None) -> None:
    """With a DJ role set, only its members control what plays, besides a track's requester for their own."""
    role = ctx.config.dj_role
    if role is None or ctx.actor.user_id == requested_by:
        return
    member = await ctx.guild.get_member(ctx.actor.user_id)
    if member is None or role not in member.role_ids:
        raise ActionError(f"Only members with <@&{role}> can do that here, or whoever queued the track.")


async def _playing(ctx: Ctx) -> Track:
    track = await ctx.db.fetchrow(Track, PLAYING, ctx.guild.id)
    if track is None or ctx.voice.state is None:
        raise ActionError("Nothing is playing.")
    return track


@plugin.action()
async def play(
    ctx: Ctx, query: Annotated[str, Field(max_length=300)], channel: VoiceChannel | None = None
) -> Queued:
    """Queue a song: a YouTube, SoundCloud or Spotify link, or words to search YouTube for.

    Args:
        query: A link, or words to search YouTube for.
        channel: The voice channel to play in; the one you are in when left out.
    """
    state = ctx.voice.state
    if state is not None and state.plugin_id != plugin.id:
        raise ActionError(f"I am in a voice channel here for the {state.plugin_id} plugin.")
    if state is not None and channel is not None and channel.id != state.channel_id:
        raise ActionError(f"I am playing in <#{state.channel_id}>. Queue there, or stop me first.")
    target = channel or (await _members_channel(ctx) if state is None else None)
    if state is None and target is None:
        raise ActionError("Join a voice channel first, or name one.")
    try:
        searchable = await _searchable(ctx, query)
        found = await asyncio.to_thread(SOURCE.find, searchable, ctx.config.max_playlist)
    except SourceError as exc:
        raise ActionError(str(exc)) from None
    limit_s = ctx.config.max_minutes * 60
    fits = [f for f in found if f.duration_s is None or f.duration_s <= limit_s]
    if not fits:
        raise ActionError(f"That is longer than this server's limit of {ctx.config.max_minutes} minutes.")
    if state is None and target is not None:
        # A queue left from before the bot was disconnected belongs to a session that is over.
        await ctx.db.execute(CLEAR, ctx.guild.id)
        await ctx.voice.join(target)
    async with ctx.db.transaction() as tx:
        ids = [
            await tx.fetchval(int, ADD, ctx.guild.id, f.title, f.url, f.duration_s, ctx.actor.user_id)
            for f in fits
        ]
        ahead = await tx.fetchval(int, AHEAD, ctx.guild.id, ids[0])
        if ahead == 0:
            await tx.schedule(play_next.bind())
    if ahead == 1:
        _in_background(asyncio.to_thread(SOURCE.prefetch, fits[0].url))
    first = fits[0]
    return Queued(
        title=first.title,
        position=ahead,
        duration_s=first.duration_s,
        more=len(fits) - 1,
        left_out=len(found) - len(fits),
    )


@plugin.action()
async def skip(ctx: Ctx) -> str:
    """Skip the song that is playing."""
    track = await _playing(ctx)
    await _check_dj(ctx, track.requested_by)
    await ctx.voice.stop()
    async with ctx.db.transaction() as tx:
        await tx.execute(FINISH, ctx.guild.id, track.id, "skipped", None)
        await tx.schedule(play_next.bind())
    return f"Skipped **{track.title}**."


@plugin.action()
async def pause(ctx: Ctx) -> str:
    """Pause the song that is playing."""
    track = await _playing(ctx)
    await _check_dj(ctx, track.requested_by)
    await ctx.voice.pause()
    await ctx.db.execute(PAUSE, ctx.guild.id)
    return f"Paused **{track.title}**."


@plugin.action()
async def resume(ctx: Ctx) -> str:
    """Carry on with the paused song."""
    track = await _playing(ctx)
    await _check_dj(ctx, track.requested_by)
    await ctx.voice.resume()
    await ctx.db.execute(RESUME, ctx.guild.id)
    return f"Playing **{track.title}** again."


@plugin.action()
async def remove(ctx: Ctx, number: Annotated[int, Field(ge=1, le=1000)]) -> str:
    """Take a song out of the queue.

    Args:
        number: Its number in /music queue.
    """
    track = await ctx.db.fetchrow(Track, NTH, ctx.guild.id, number - 1)
    if track is None:
        raise ActionError("Nothing is queued at that number. /music queue shows the numbers.")
    await _check_dj(ctx, track.requested_by)
    await ctx.db.execute(FINISH, ctx.guild.id, track.id, "removed", None)
    return f"Took **{track.title}** out of the queue."


@plugin.action()
async def stop(ctx: Ctx) -> str:
    """Stop the music, clear the queue, and leave the voice channel."""
    await _check_dj(ctx, None)
    if ctx.voice.state is None:
        raise ActionError("I am not in a voice channel.")
    await ctx.db.execute(CLEAR, ctx.guild.id)
    await ctx.voice.leave()
    return "Stopped, cleared the queue and left."


@plugin.action(risk=READ, reply="public")
async def queue(ctx: Ctx) -> QueueView:
    """What is playing, and what plays next."""
    playing = await ctx.db.fetchrow(Track, PLAYING, ctx.guild.id) if ctx.voice.state is not None else None
    paused = bool(await ctx.db.fetchval(PAUSED, ctx.guild.id)) if playing is not None else False
    up_next = await ctx.db.fetch(Track, WAITING, ctx.guild.id, UP_NEXT)
    more = await ctx.db.fetchval(int, WAITING_COUNT, ctx.guild.id) - len(up_next)
    return QueueView(playing=playing, paused=paused, up_next=up_next, more=more)


@plugin.job
async def play_next(ctx: Ctx) -> None:
    await run_queue(ctx)


@plugin.job
async def leave_if_idle(ctx: Ctx) -> None:
    """Leave when, since this was scheduled, nothing has started playing and nothing has been queued."""
    state = ctx.voice.state
    if state is None or state.plugin_id != plugin.id or state.track is not None:
        return
    if await ctx.db.fetchval(int, WAITING_COUNT, ctx.guild.id) == 0:
        await _give_up(ctx)


@plugin.job
async def leave_if_alone(ctx: Ctx, channel: int) -> None:
    """Leave `channel` when nobody but bots is in it still: someone may have come back during the wait."""
    state = ctx.voice.state
    if state is None or state.plugin_id != plugin.id or state.channel_id != channel:
        return
    found = ctx.guild.get_voice_channel(channel)
    if found is None or not found.listener_ids:
        await _give_up(ctx)


@plugin.on_track_end
async def track_ended(ctx: Ctx, track: str, error: str | None) -> None:
    await ctx.db.execute(FINISH, ctx.guild.id, int(track), "failed" if error else "played", error)
    await ctx.db.execute(PRUNE, ctx.guild.id, KEEP_FINISHED)
    await run_queue(ctx)


@plugin.on_voice_alone
async def left_alone(ctx: Ctx, channel: VoiceChannel | None) -> None:
    """Everyone else has left the bot's channel: give them `alone_minutes` to come back before leaving."""
    if channel is None or not _ours(ctx):
        return
    if ctx.config.alone_minutes == 0:
        await leave_if_alone.func(ctx, channel.id)
        return
    async with ctx.db.transaction() as tx:
        at = ctx.now + timedelta(minutes=ctx.config.alone_minutes)
        await tx.schedule(leave_if_alone.bind(channel=channel.id), at=at)


@plugin.on_voice_resume
async def voice_resumed(ctx: Ctx, channel: VoiceChannel | None) -> None:
    """After a restart: back into the channel, carrying on with the track from where it was. With the channel
    gone, nothing queued, or no way back in, the queue is cleared and the connection given up."""
    await ctx.db.execute(REQUEUE, ctx.guild.id)
    waiting = await ctx.db.fetchval(int, WAITING_COUNT, ctx.guild.id)
    if channel is not None and waiting:
        try:
            await ctx.voice.join(channel)
        except ActionError:
            pass
        else:
            await run_queue(ctx)
            return
    await ctx.db.execute(CLEAR, ctx.guild.id)
    await ctx.voice.leave()
