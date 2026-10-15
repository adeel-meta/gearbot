#!/usr/bin/env bash
# Upload local .env keys to a linked Vercel project (never commits .env to git).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from .env.example and fill secrets." >&2
  exit 1
fi

if ! command -v vercel >/dev/null 2>&1; then
  VC="npx vercel@latest"
else
  VC="vercel"
fi

if [[ ! -f "$ROOT/.vercel/project.json" ]]; then
  echo "Link this folder first: (cd $ROOT && $VC link)" >&2
  exit 1
fi

TARGETS=(production preview development)
added=0
skipped=0

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -z "$line" ]] && continue
  [[ "$line" != *=* ]] && continue
  key="${line%%=*}"
  value="${line#*=}"
  key="$(echo "$key" | sed 's/[[:space:]]//g')"
  # Strip optional quotes
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  if [[ -z "$value" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  for env_name in "${TARGETS[@]}"; do
    printf '%s' "$value" | $VC env add "$key" "$env_name" --force >/dev/null
  done
  echo "set $key (${#value} chars) → production, preview, development"
  added=$((added + 1))
done < "$ENV_FILE"

echo "Done. Variables set: $added, skipped empty: $skipped."
echo "Redeploy on Vercel so new env is picked up."
