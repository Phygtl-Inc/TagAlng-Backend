#!/usr/bin/env bash
# Verifies GOOGLE_MAPS_API_KEY works for the APIs the Places features use.
# Usage: ./scripts/check-google-places.sh
# Reads the key from deploy/lana-worker.env (same file the worker uses).

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/deploy/lana-worker.env}"

[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
KEY="${GOOGLE_MAPS_API_KEY:-}"
if [[ -z "$KEY" ]]; then
  echo "❌ GOOGLE_MAPS_API_KEY is not set in $ENV_FILE — Places/geocoding will return nothing."
  exit 1
fi

# Places API (New) — POST with field-mask header. A `places` array (or empty) = success;
# an `error` object = the API/key isn't set up right.
echo -n "Places API New (nearby chips + place search): "
PLACES=$(curl -s -X POST "https://places.googleapis.com/v1/places:searchText" \
  -H "Content-Type: application/json" \
  -H "X-Goog-Api-Key: $KEY" \
  -H "X-Goog-FieldMask: places.displayName" \
  -d '{"textQuery":"park near Lake Nona FL","maxResultCount":3}')
echo "$PLACES" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if "error" in d:
    e = d["error"]
    print("❌ " + e.get("status","ERROR"))
    print("     -> " + e.get("message",""))
else:
    n = len(d.get("places", []))
    print("✅ OK (%d results)" % n)
'

echo -n "Geocoding (event venue → lat/lng): "
GEO=$(curl -s "https://maps.googleapis.com/maps/api/geocode/json?address=Lake+Nona&key=$KEY")
echo "$GEO" | python3 -c '
import sys, json
d = json.load(sys.stdin)
s = d.get("status","NO_STATUS")
if s in ("OK","ZERO_RESULTS"):
    print("✅ " + s)
else:
    print("❌ " + s)
    print("     -> " + d.get("error_message",""))
'
