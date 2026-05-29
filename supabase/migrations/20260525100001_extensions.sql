-- TagAlng Phase 1: required Postgres extensions
create extension if not exists postgis with schema extensions;
create extension if not exists vector with schema extensions;
-- H3: add h3-pg extension when available on hosted Supabase, or compute block_id in app/worker.
-- Phase 1 stores block_id as text (H3 cell index).;
