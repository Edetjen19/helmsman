#!/usr/bin/env bash
# Create the scanner's findings as labeled issues on the fork, using the host's authed gh.
# Idempotent: skips any title that already exists open. Reads the JSON the container scanner
# emits (the scanner needs pydantic, which lives in the image; gh + auth live on the host).
#
#   1) docker compose run --rm --no-deps \
#        -v "$SUPERSET_CLONE":/clone:ro web \
#        python -m src.scanner --root /clone --emit-json data/issues.json
#   2) bash scripts/create_issues.sh                 # uses data/issues.json
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="${GITHUB_REPO:-Edetjen19/superset}"
JSON="${1:-data/issues.json}"
[ -f "$JSON" ] || { echo "missing $JSON (run the scanner --emit-json first)"; exit 1; }

# Ensure the labels exist (idempotent via --force).
ensure_label() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null; }
ensure_label devin-remediate      5319e7 "Autonomous remediation candidate (Helmsman)"
ensure_label dependency-upgrade   0e8a16 "Dependency cap / EOL upgrade"
ensure_label deprecation-migration fbca04 "Deprecated-API migration"
ensure_label lint-graduation      1d76db "Lint rule graduation (warn -> error)"

existing="$(gh issue list --repo "$REPO" --label devin-remediate --state all --json title --limit 100)"

count="$(jq length "$JSON")"
for i in $(seq 0 $((count - 1))); do
  title="$(jq -r ".[$i].title" "$JSON")"
  if echo "$existing" | jq -e --arg t "$title" 'any(.[]; .title == $t)' >/dev/null; then
    echo "[exists ] $title"
    continue
  fi
  body="$(jq -r ".[$i].body" "$JSON")"
  # labels are always ["devin-remediate", <class>]; read them without mapfile (bash 3.2 on macOS)
  label_args=()
  while IFS= read -r l; do label_args+=(--label "$l"); done < <(jq -r ".[$i].labels[]" "$JSON")
  url="$(gh issue create --repo "$REPO" --title "$title" --body "$body" "${label_args[@]}")"
  echo "[created] $url"
done
