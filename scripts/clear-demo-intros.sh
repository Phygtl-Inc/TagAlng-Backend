#!/usr/bin/env bash
# Clear intros + nudges + relationship tiers between Lake Nona demo neighbors
# (Sofia, Ada, Kashaf) so you can re-test propose_intro / nudge flows.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
source "${ROOT}/deploy/lana-worker.env"

SOFIA=1b18263a-e0c6-4589-a4cf-d8f42da525c7
ADA=31db97ef-bb48-46c5-acef-556ba5f3d3ef
KASHAF=b5f26595-3bbc-44ce-9a36-77e6cd292c49
IDS="${SOFIA},${ADA},${KASHAF}"

auth=(-H "apikey: ${SUPABASE_SERVICE_ROLE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_ROLE_KEY}")

echo "Deleting intros among Sofia / Ada / Kashaf (proposed + accepted)..."
curl -s -o /dev/null -w "intros: %{http_code}\n" -X DELETE \
  "${SUPABASE_URL}/rest/v1/intros?or=(and(initiator_id.in.(${IDS}),candidate_id.in.(${IDS})))&status=in.(proposed,accepted)" \
  "${auth[@]}"

echo "Deleting nudges among Sofia / Ada / Kashaf..."
curl -s -o /dev/null -w "nudges: %{http_code}\n" -X DELETE \
  "${SUPABASE_URL}/rest/v1/nudges?or=(and(sender_id.in.(${IDS}),recipient_id.in.(${IDS})))" \
  "${auth[@]}"

echo "Resetting relationship tiers..."
for pair in "${SOFIA}:${ADA}" "${SOFIA}:${KASHAF}" "${ADA}:${KASHAF}"; do
  a="${pair%%:*}"
  b="${pair##*:}"
  low=$([ "$a" \< "$b" ] && echo "$a" || echo "$b")
  high=$([ "$a" \< "$b" ] && echo "$b" || echo "$a")
  curl -s -o /dev/null -w "  ${low} <-> ${high}: %{http_code}\n" -X DELETE \
    "${SUPABASE_URL}/rest/v1/user_relationships?user_low=eq.${low}&user_high=eq.${high}" \
    "${auth[@]}"
done

echo "Done. Hard-refresh PWA or start a fresh Lana session to drop stale pending_intro_offer."
