#!/usr/bin/env bash
# verify_setup.sh — one-shot preflight for the Devin × Superset control plane.
# Confirms the environment is green before building. Prints PASS/FAIL/WARN per
# check and exits non-zero if any hard check fails. Never prints secret values.
#
# Usage:  bash scripts/verify_setup.sh   (run from the repo root)

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2

PASS=0; FAIL=0; WARN=0
ok()   { echo "  [PASS] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN+1)); }

echo "== .env =="
if [ -f .env ]; then
  set -a; . ./.env; set +a
  ok ".env present"
  case "${DEVIN_API_KEY:-}" in cog_*) ok "DEVIN_API_KEY is a cog_ (v3) key";; "") bad "DEVIN_API_KEY is empty";; *) warn "DEVIN_API_KEY is not a cog_ key (v3 expects cog_)";; esac
  [ -n "${DEVIN_ORG_ID:-}" ] && ok "DEVIN_ORG_ID set ($DEVIN_ORG_ID)" || bad "DEVIN_ORG_ID empty"
  [ -n "${WEBHOOK_SECRET:-}" ] && ok "WEBHOOK_SECRET set" || warn "WEBHOOK_SECRET empty"
  : "${DEVIN_BASE_URL:=https://api.devin.ai/v3}"
else
  bad ".env missing (copy .env.example and fill DEVIN_API_KEY)"
fi

echo "== secret hygiene =="
git check-ignore -q .env 2>/dev/null && ok ".env is gitignored" || bad ".env is NOT gitignored"
git ls-files --error-unmatch .env >/dev/null 2>&1 && bad ".env is tracked by git" || ok ".env is not tracked"
if [ ! -f .env.example ]; then warn ".env.example missing (should exist as a template)"; elif grep -qE 'cog_[A-Za-z0-9]{20,}|gh[op]_[A-Za-z0-9]{20,}' .env.example; then bad "real secret found in .env.example"; else ok ".env.example present, no real secrets"; fi

echo "== Devin v3 auth (GET /v3/self, zero ACU) =="
if [ -n "${DEVIN_API_KEY:-}" ]; then
  tmp="$(mktemp)"; code=$(curl -s -o "$tmp" -w "%{http_code}" "$DEVIN_BASE_URL/self" -H "Authorization: Bearer $DEVIN_API_KEY")
  if [ "$code" = "200" ]; then
    who=$(python3 -c "import json;j=json.load(open('$tmp'));print(j.get('principal_type'),j.get('service_user_name'),j.get('org_id'))" 2>/dev/null)
    ok "GET /v3/self → 200 ($who)"
    echo "$who" | grep -q "${DEVIN_ORG_ID:-NOORG}" && ok "org_id matches .env" || warn "org_id from /self does not match DEVIN_ORG_ID"
  else bad "GET /v3/self → HTTP $code (check key/org)"; fi
  rm -f "$tmp"
else warn "skipped (no key)"; fi

echo "== GitHub fork =="
REPO="${GITHUB_REPO:-Edetjen19/superset}"
gh auth status >/dev/null 2>&1 && ok "gh authenticated" || bad "gh not authenticated"
if gh api "repos/$REPO" >/dev/null 2>&1; then
  ok "fork $REPO reachable"
  en=$(gh api "repos/$REPO/actions/permissions" --jq '.enabled' 2>/dev/null)
  [ "$en" = "true" ] && ok "Actions enabled on fork" || bad "Actions NOT enabled on fork"
  active=$(gh api "repos/$REPO/actions/workflows" --paginate --jq '[.workflows[]|select(.path|test("python-unittest|pre-commit|pr-lint"))|select(.state=="active")]|length' 2>/dev/null)
  [ "${active:-0}" -ge 3 ] && ok "gating workflows active ($active/3)" || warn "only ${active:-0}/3 gating workflows active"
else bad "fork $REPO not reachable (create with: gh repo fork apache/superset --default-branch-only)"; fi

echo "== Devin sees the fork =="
if [ -n "${DEVIN_API_KEY:-}" ] && [ -n "${DEVIN_ORG_ID:-}" ]; then
  tmp="$(mktemp)"
  code=$(curl -s -o "$tmp" -w "%{http_code}" "https://api.devin.ai/v3beta1/organizations/$DEVIN_ORG_ID/repositories?first=100" -H "Authorization: Bearer $DEVIN_API_KEY")
  if [ "$code" = "200" ]; then
    grep -qi 'superset' "$tmp" && ok "fork is in Devin's connected repos (can open PRs)" || bad "fork NOT connected in Devin (connect GitHub in Devin settings)"
  else warn "available-repos check → HTTP $code"; fi
  code=$(curl -s -o "$tmp" -w "%{http_code}" "https://api.devin.ai/v3beta1/organizations/$DEVIN_ORG_ID/repositories/indexing?first=200" -H "Authorization: Bearer $DEVIN_API_KEY")
  if [ "$code" = "200" ]; then
    grep -qi 'superset' "$tmp" && ok "fork is indexed in Devin" || warn "fork not yet indexed (PUT .../repositories/Edetjen19%2Fsuperset/indexing) — optional"
  fi
  rm -f "$tmp"
else warn "skipped (no key/org)"; fi

echo "== Docker =="
command -v docker >/dev/null 2>&1 && { docker info >/dev/null 2>&1 && ok "docker daemon running" || warn "docker installed but daemon not running (start Docker Desktop)"; } || warn "docker not found"

echo
echo "== summary: $PASS pass, $WARN warn, $FAIL fail =="
[ "$FAIL" -eq 0 ] && { echo "Environment is GREEN. Live-run prerequisites satisfied (SIMULATE needs none)."; exit 0; } || { echo "Environment has FAILURES. Fix the [FAIL] items above."; exit 1; }
