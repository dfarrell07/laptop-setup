#!/bin/bash
# verify-collections-galaxy.sh — Verify collections against Galaxy API at bootstrap time
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
GALAXY_API="https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index"
if command -v sha256sum >/dev/null 2>&1; then
  _checksum=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
  _checksum=(shasum -a 256)
else
  echo "FATAL: SHA256 verification requires sha256sum or shasum" >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 is required to parse the Galaxy API response" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "FATAL: curl is required to query the Galaxy API" >&2; exit 1; }
FAIL=0

SUMS="${REPO_DIR}/collections-dist/SHA256SUMS"
[ -f "$SUMS" ] || { echo "ERROR: ${SUMS} not found" >&2; exit 1; }
COUNT=0

while read -r _ file; do
  [ -z "$file" ] && continue
  file="${file#\*}"
  if [[ ! "$file" =~ ^([^-]+)-(.+)-([0-9][^-]*)\.tar\.gz$ ]]; then
    echo "ERROR: ${file} — unrecognized name in SHA256SUMS" >&2
    FAIL=1
    continue
  fi
  ns="${BASH_REMATCH[1]}"
  name="${BASH_REMATCH[2]}"
  ver="${BASH_REMATCH[3]}"
  COUNT=$((COUNT + 1))
  filepath="${REPO_DIR}/collections-dist/${file}"

  if [ ! -f "$filepath" ]; then
    echo "ERROR: ${file} — listed in SHA256SUMS but tarball is missing" >&2
    FAIL=1
    continue
  fi

  # Fetch Galaxy API hash with exponential backoff retry for transient failures
  # Fail fatally on API errors: any network failure, timeout, or malformed JSON is fatal
  api_hash=""
  api_error=""
  for attempt in 1 2 3; do
    # Capture both stdout and stderr to detect all error conditions
    api_hash=$(curl -sf --max-time 10 --connect-timeout 5 "${GALAXY_API}/${ns}/${name}/versions/${ver}/" 2>&1 | python3 -I -c "import sys,json; print(json.load(sys.stdin)['artifact']['sha256'])" 2>&1) && {
      api_error=""
      break
    } || {
      api_error="$?"
      api_hash=""
      if [ "$attempt" -lt 3 ]; then
        sleep $((2 ** (attempt - 1)))
      fi
    }
  done

  # If API fetch still failed after retries, treat as fatal error (possible compromise)
  if [ -n "$api_error" ] && [ -z "$api_hash" ]; then
    echo "ERROR: ${file} — failed to fetch/parse Galaxy API hash after 3 retries" >&2
    echo "       Possible API compromise or persistent network error. Do not proceed." >&2
    FAIL=1
    continue
  fi

  local_hash=$("${_checksum[@]}" "$filepath" 2>/dev/null | awk '{print $1}' || echo "")

  if [ -z "$api_hash" ]; then
    # Reached when the API answers successfully but with an empty artifact hash
    echo "ERROR: ${file} — Galaxy API hash is empty (unexpected)" >&2
    FAIL=1
  elif [ -z "$local_hash" ]; then
    echo "ERROR: ${file} — local file missing or unreadable" >&2
    FAIL=1
  elif [ "$api_hash" = "$local_hash" ]; then
    echo "✓ ${file} matches Galaxy API hash"
  else
    echo "ERROR: ${file} — MISMATCH (possible supply chain tampering)" >&2
    FAIL=1
  fi
done < "$SUMS"

[ "$COUNT" -gt 0 ] || { echo "ERROR: no collections listed in SHA256SUMS" >&2; FAIL=1; }
[ "$FAIL" -eq 1 ] && { echo "Collection Galaxy API verification failed" >&2; exit 1; }
exit 0
