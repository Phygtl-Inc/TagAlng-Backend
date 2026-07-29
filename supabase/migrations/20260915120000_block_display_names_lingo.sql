-- 20260912 fixed the "(placeholder)" leak but picked names containing "Block"
-- ("Lake Nona — Block A") — a lingo-banned word (constitution rule 3). Every peer
-- preview / activity header that embeds the label trips the final-mile guard,
-- whose rewrite garbles the sentence ("I found 3 neighbors near Lake Nona — your
-- area A:", observed in prod 2026-07-29). Rename at the source so user-facing
-- labels are lexicon-clean before they ever reach copy. Idempotent.

update public.blocks
set display_name = regexp_replace(display_name, '\mblocks?\M', 'Area', 'gi')
where display_name ~* '\mblocks?\M';
