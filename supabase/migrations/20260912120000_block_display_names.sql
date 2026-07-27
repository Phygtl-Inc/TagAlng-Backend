-- The phase1 seed blocks (20260525100002) shipped with "(placeholder)" in their
-- display_name and were never renamed — the name leaks verbatim into Lana's copy
-- ("I found 2 neighbors near Lake Nona — Block A (placeholder)"), including in prod.
-- Real names, guarded so a manual rename is never clobbered. Idempotent.

update public.blocks
set display_name = 'Lake Nona — Block A'
where id = '8a2a1072b59ffff'
  and display_name like '%(placeholder)%';

update public.blocks
set display_name = 'Lake Nona — Block B'
where id = '8a2a1072b5affff'
  and display_name like '%(placeholder)%';

-- Belt-and-braces: no other block anywhere should show "(placeholder)" to users.
update public.blocks
set display_name = trim(trailing ' —–-' from replace(display_name, '(placeholder)', ''))
where display_name like '%(placeholder)%';
