-- 20270120120000_peer_terms_whole_words · checks (local validation container only)

begin;

do $$
begin
  -- The prod case: "ai" must never match inside another word.
  assert not public._claim_term_hit('Enjoys boardwalk trails', 'ai'), 'trails';
  assert not public._claim_term_hit('faith', 'ai'), 'faith';
  assert not public._claim_term_hit('Enjoys arcade entertainment', 'ai'), 'entertainment';
  assert not public._claim_term_hit('Trains alone', 'ai'), 'trains';
  -- … and must match the word itself, in any case or position.
  assert public._claim_term_hit('Works in AI', 'ai'), 'AI word';
  assert public._claim_term_hit('ai researcher', 'ai'), 'ai first';
  assert public._claim_term_hit('Builds AI-powered tools', 'ai'), 'AI-powered';
  -- Up to 3 letters is a whole word only — "tea" must not find "teacher"; the classifier
  -- sends the forms it means ("run", "running", "runner") as separate terms.
  assert public._claim_term_hit('Morning run club', 'run'), 'run word';
  assert not public._claim_term_hit('Loves running at dawn', 'run'), 'run is not running';
  assert not public._claim_term_hit('Primary school teacher', 'tea'), 'tea is not teacher';
  -- Longer terms reach their inflections from the start of a word only.
  assert public._claim_term_hit('Loves swimming laps', 'swim'), 'swim → swimming';
  assert public._claim_term_hit('Open-water swimmer', 'swim'), 'swim → swimmer';
  assert not public._claim_term_hit('Enjoys the outdoors', 'door'), 'door inside outdoors';
  assert public._claim_term_hit('Badminton player', 'badminton'), 'exact';
  assert not public._claim_term_hit('Plays badminton', 'minton'), 'no mid-word';
  -- Regex characters in a term are literal, never a pattern.
  assert public._claim_term_hit('Writes c++ daily', 'c++'), 'c++ literal';
  assert not public._claim_term_hit('anything', '.*'), 'dot-star literal';
  assert not public._claim_term_hit(null, 'ai') and not public._claim_term_hit('x', ''), 'empty';
  raise notice 'peer_terms_whole_words: all checks passed';
end;
$$;

-- The four functions exist and no longer carry a substring predicate.
do $$
declare
  fn text;
begin
  foreach fn in array array['find_peers_by_claim_filters', 'find_peers_by_attr_filter',
                            'find_peers_by_attr_filter_near', 'find_peers_by_claim_filters_near'] loop
    assert exists (select 1 from pg_proc where proname = fn), fn || ' missing';
    assert not exists (
      select 1 from pg_proc where proname = fn and prosrc like '%like ''%%'' ||%'
    ), fn || ' still uses like %term%';
    assert exists (
      select 1 from pg_proc where proname = fn and prosrc like '%_claim_term_hit%'
    ), fn || ' does not use _claim_term_hit';
  end loop;
  raise notice 'peer_terms_whole_words: functions re-emitted';
end;
$$;

rollback;
