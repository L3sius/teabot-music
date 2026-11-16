-- A track taken out of the queue with /music remove is 'removed', so the history tells it apart from a skip.
ALTER TABLE tracks DROP CONSTRAINT tracks_state_check;
ALTER TABLE tracks ADD CONSTRAINT tracks_state_check
    CHECK (state IN ('queued', 'playing', 'played', 'skipped', 'removed', 'failed', 'cleared'));
