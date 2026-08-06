-- Swap is not shipped. Stop offering it.
--
-- Lana offered "I can help you swap or pass along kids gear with other families
-- nearby" with a "See who's swapping nearby" chip. Both swap rows were is_active
-- true with required_state '{}', i.e. offerable on every turn to anyone, so the
-- policy picked cap:looking.swap as a bridge_offer the moment the user declined
-- something else. The prompt's rule "only promise what AVAILABLE CAPABILITIES
-- lists" was obeyed — these rows put swap on that list.
--
-- Deactivate rather than delete: the rows keep their triggers and priority, so
-- turning swap back on when it ships is one flag, not a re-seed. The
-- looking.swap / sharing.swap linear intents and local_signals plumbing are left
-- alone — this only stops Lana VOLUNTEERING swap.

update public.capability_index
set is_active = false,
    updated_at = now()
where capability_id in ('looking.swap', 'sharing.swap');

-- Latent suggestions (suggestion_queue -> _offer_goals) are a SECOND source of
-- capability offers and never checked is_active. That is fixed in code, at the
-- goal merge (app/policy/goals.py::candidate_goals), which drops any goal naming
-- an inactive capability whatever source it came from. Nothing to do here:
-- queued swap rows stay as they are and are simply never surfaced.
--
-- Deliberately NOT writing user_action on those rows. It is constrained to
-- ('accepted','dismissed','ignored','converted') and is the Layer 3 training
-- LABEL — marking them 'dismissed' would teach the model a user rejected a
-- suggestion they were never shown.
