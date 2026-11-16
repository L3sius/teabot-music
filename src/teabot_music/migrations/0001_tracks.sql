-- The queue of each server: every track queued, in order, with what became of it. `resume_at_s` is where a track
-- carries on from after a restart cut it off, and `started_at` is shifted by each pause, so the time since it is
-- how far the track has played.
CREATE TABLE tracks (
    guild_id bigint NOT NULL,
    id bigint GENERATED ALWAYS AS IDENTITY,
    title text NOT NULL,
    url text NOT NULL,
    duration_s integer,
    requested_by bigint NOT NULL,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'playing', 'played', 'skipped', 'failed', 'cleared')),
    error text,
    resume_at_s integer NOT NULL DEFAULT 0,
    added_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    paused_at timestamptz,
    PRIMARY KEY (guild_id, id)
);
CREATE INDEX tracks_waiting ON tracks (guild_id, id) WHERE state IN ('queued', 'playing');
