-- discovery_includes_chapters — applied directly to PROD on 2026-10-06 19:53 UTC (by hand, not from this repo).
--
-- Kept here as a NO-OP so the repo's migration history matches prod's and `supabase db
-- push` stops refusing ("Remote migration versions not found in local migrations").
-- Prod will never re-run it.
--
-- Why not the original SQL: its version sorts BEFORE migrations it depends on (is_test,
-- blurb_embedding, events.embedding arrive later in this repo's order), so verbatim it
-- fails on a fresh database — and on dev it would overwrite newer function bodies
-- (private meets, chapter rules) with this older copy. Its effect is carried instead by
-- 20270122120000_restore_prod_oct6_changes.sql (discover_communities; discover_communities_near
-- was superseded on prod by 20270119120000_community_meaning_search_and_chapters), re-applied on top of the current definitions.
-- The original SQL is quoted in that migration's header.

select 1;
