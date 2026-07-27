-- Lingo scrub follow-up: 20260909 caught the four "mom" rows but missed one
-- "block" — registry text is pasted into copy-authoring prompts, so backstage
-- vocabulary here teaches the model the banned word.

update public.capability_index
   set description = 'Find someone nearby willing to swap kids gear, clothes, or equipment'
 where capability_id = 'looking.swap';
