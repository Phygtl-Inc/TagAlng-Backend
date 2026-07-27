#!/usr/bin/env bash
# db-push.sh — run repo migrations against dev or prod, safely.
#
#   ./scripts/db-push.sh dev            # link dev → list → dry-run → confirm → push
#   ./scripts/db-push.sh prod           # same, but requires typing "push to prod"
#   ./scripts/db-push.sh prod --list    # just show applied-vs-pending, no push
#
# Requirements:
#   * SUPABASE_ACCESS_TOKEN in the env (or a prior `npx supabase login`)
#   * the target's DB password — the CLI prompts for it, or export SUPABASE_DB_PASSWORD
#
# Safety properties:
#   * prod push demands a typed confirmation, never a bare y/N
#   * whatever happens (error, Ctrl-C), the repo is re-linked to DEV on exit,
#     so a later casual `supabase db push` can never land on prod by accident

set -euo pipefail

# Prefer the native CLI (brew install supabase/tap/supabase) — the npx package
# resolves a per-platform binary and breaks when the npm cache lacks darwin-arm64.
if command -v supabase >/dev/null 2>&1; then
  SUPA=(supabase)
else
  SUPA=(npx supabase)
fi

DEV_REF="rjlcyvwogmfmngemhbmn"                       # tagalng-dev
PROD_REF="${SUPABASE_PROD_REF:-kmetmatfxdkrialwrnzj}"  # tagalng prod

cd "$(dirname "$0")/.."

TARGET="${1:-}"
MODE="${2:-}"

case "$TARGET" in
  dev)  REF="$DEV_REF" ;;
  prod)
    if [[ -z "$PROD_REF" ]]; then
      echo "✗ Set SUPABASE_PROD_REF to the prod project ref (the kmetmat… id in the prod dashboard URL)." >&2
      exit 1
    fi
    REF="$PROD_REF"
    ;;
  *) echo "usage: $0 dev|prod [--list]" >&2; exit 1 ;;
esac

relink_dev() {
  # Leave the repo pointing at dev no matter how we exit — and verify it stuck:
  # a silently-failed relink would leave prod as the default push target.
  "${SUPA[@]}" link --project-ref "$DEV_REF" >/dev/null 2>&1 || true
  local now
  now="$(cat supabase/.temp/project-ref 2>/dev/null || true)"
  if [[ "$now" != "$DEV_REF" ]]; then
    echo "" >&2
    echo "🚨 RE-LINK TO DEV FAILED — repo is still linked to '$now'." >&2
    echo "   Run:  npx supabase link --project-ref $DEV_REF" >&2
  fi
}
trap relink_dev EXIT

echo "── linking to $TARGET ($REF)…"
"${SUPA[@]}" link --project-ref "$REF"

echo
echo "── migration status ($TARGET):"
"${SUPA[@]}" migration list --linked

if [[ "$MODE" == "--list" ]]; then
  exit 0
fi

echo
echo "── dry run:"
"${SUPA[@]}" db push --dry-run

echo
if [[ "$TARGET" == "prod" ]]; then
  echo "⚠️  You are about to migrate PRODUCTION ($REF)."
  echo "    Make sure a backup / PITR point exists (dashboard → Database → Backups)."
  read -r -p '    Type exactly "push to prod" to continue: ' answer
  [[ "$answer" == "push to prod" ]] || { echo "aborted."; exit 1; }
else
  read -r -p "Push the above to $TARGET? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "aborted."; exit 1; }
fi

"${SUPA[@]}" db push

echo
echo "── verifying:"
"${SUPA[@]}" migration list --linked
echo "✓ done — repo will be re-linked to dev on exit."
