# teabot-music

A [teabot](https://github.com/hballington12/teabot) plugin that plays music in a voice channel: members queue
YouTube, YouTube Music, SoundCloud or Spotify links, or search YouTube by name.

It is a third-party plugin for self-hosted instances. Streaming YouTube breaks YouTube's terms of service, so it
is not one of teabot's first-party plugins and is not meant for a hosted catalogue (D47, D53).

## Commands

| Command | What it does |
|---|---|
| `/music play query [channel]` | Queue a link, a playlist or a search. Joins your voice channel, or the one named, when the bot is in none. |
| `/music queue` | What plays now, how far it has got, and what plays next. |
| `/music skip` | Skip the song playing. |
| `/music remove number` | Take a song out of the queue, by its number in `/music queue`. |
| `/music pause`, `/music resume` | Pause and carry on. |
| `/music stop` | Stop, clear the queue and leave. |

The same actions are MCP tools and service API routes, as every teabot action is. An MCP client names the
voice channel, since its token's member is rarely in one.

## Config

- `dj_role`: when set, only members with this role skip, pause, resume, remove or stop, besides a track's
  requester for their own track. Anyone may queue.
- `max_minutes`: the longest track that can be queued, 30 by default. Live streams are refused, and a playlist's
  tracks over the limit are left out.
- `max_playlist`: how many tracks a playlist link queues, 25 by default.
- `alone_minutes`: how long the bot stays once nobody else is in its channel, 2 by default; 0 leaves at once.
- `idle_minutes`: how long the bot waits with nothing to play before leaving, 5 by default.

## How it plays

- It needs teabot 1.6 or later with its voice extra, `teabot[voice]` (D53), which this package depends on.
- Beyond teabot's own packages it installs PyAV, yt-dlp, yt-dlp-ejs and Deno, which reads YouTube's player code,
  and requests, without which YouTube refused about four streams in nine rather than one.
- PyAV reads the audio. YouTube serves Opus at 48 kHz, which goes to Discord as it is, at about 1% of a CPU
  core per server playing; other audio is re-encoded with PyAV's own libopus, at about 2%.
- YouTube holds a stream back for 5 to 6 seconds after handing out its address, so the first song takes about
  8 seconds to start. The next song's stream is resolved while one plays, so the songs after it start at once.
- A track that cannot be opened is marked failed and the next one plays.
- Playlists: a YouTube playlist page (`youtube.com/playlist?list=...`) or a SoundCloud set queues its tracks,
  leaving out private, deleted and live ones. A video opened from inside a playlist plays on its own. Spotify
  albums and playlists are refused: each track would need a YouTube search.
- The bot leaves on its own: `alone_minutes` after the last member other than bots leaves its channel, unless
  someone comes back, and `idle_minutes` after the queue runs out, unless something is queued.
- After a restart the bot rejoins the channel and carries on with the song from where it had got to.
- yt-dlp follows YouTube's changes; when YouTube stops playing, update it with `uv lock --upgrade-package yt-dlp`.

## External connections

- `youtube.com`, `music.youtube.com`, `youtu.be` and `*.googlevideo.com`: finding and streaming YouTube tracks.
- `soundcloud.com` and its media hosts (`*.sndcdn.com`): SoundCloud tracks.
- `open.spotify.com`: the public page of a Spotify track, for its title and artist, through `ctx.http`, which is
  the host in `Plugin(..., hosts=[...])`. No Spotify account or key.

yt-dlp and PyAV reach YouTube and SoundCloud themselves, since a stream's host, such as
`rr1---sn-4p8xjp.googlevideo.com`, changes with each track and cannot be declared by name.

Links to any other site are refused, so members cannot point the bot at an arbitrary address.

## Developing

- Until teabot releases voice, the package builds against its pull request's branch,
  [teabot-dev/teabot#45](https://github.com/teabot-dev/teabot/pull/45); `uv lock --upgrade-package teabot` follows
  the branch.
- Tests need Postgres, as teabot's own do: `uv run pytest`. They use a fake source and teabot's fake voice
  connection, so they need no network; `tests/test_sources.py` checks PyAV with audio files it writes.
- `tests/snapshot.json` records the commands, tools and job payloads; accept a change with `--teabot-accept`.
- `uv run teabot check src/teabot_music` runs teabot's review checklist; the tests run it too.
- `uv run ruff check .` and `uv run pyright` keep it clean.
