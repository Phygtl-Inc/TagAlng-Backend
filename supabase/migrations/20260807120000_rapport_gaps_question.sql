-- Rapport: store the contextual question on each gap.
-- Questions are now AI-generated per turn (the extractor's warm followup_question), not
-- static templates — so "I play FIFA" gets a FIFA-specific ask, not "solo or with moms".
-- See LANA_RAPPORT_STRATEGY_v1 §1/§4: semantic questioning, WHY-frame never templated.

alter table public.rapport_gaps add column if not exists question text;
