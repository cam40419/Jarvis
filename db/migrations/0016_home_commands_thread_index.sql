CREATE INDEX home_commands_thread_recent
    ON home_commands (household_id, actor_id, thread_id, created_at DESC, id DESC);

CREATE INDEX action_proposals_actor_recent
    ON action_proposals (actor_id, ((snapshot->>'created_at')), id DESC);
