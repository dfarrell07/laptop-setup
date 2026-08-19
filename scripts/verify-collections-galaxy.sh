#!/bin/bash
# verify-collections-galaxy.sh — Verify collections against Galaxy API at bootstrap time
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
GALAXY_API="https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index"
FAIL=0

for spec in \
  "ansible/posix/2.2.2/ansible-posix-2.2.2.tar.gz" \
  "community/general/13.2.0/community-general-13.2.0.tar.gz" \
  "community/library_inventory_filtering_v1/1.1.5/community-library_inventory_filtering_v1-1.1.5.tar.gz" \
  "containers/podman/1.20.2/containers-podman-1.20.2.tar.gz"; do

  ns=$(echo "$spec" | cut -d/ -f1)
  name=$(echo "$spec" | cut -d/ -f2)
  ver=$(echo "$spec" | cut -d/ -f3)
  file=$(echo "$spec" | cut -d/ -f4)
  filepath="${REPO_DIR}/collections-dist/${file}"

  [ ! -f "$filepath" ] && { echo "SKIP: ${file} — file not yet present"; continue; }

  # Fetch Galaxy API hash with explicit error handling
  # Any network error, timeout, malformed JSON, or missing 'artifact' key causes fatal failure
  api_hash=$(curl -sf --max-time 10 --connect-timeout 5 "${GALAXY_API}/${ns}/${name}/versions/${ver}/" 2>&1 | python3 -c "import sys,json; print(json.load(sys.stdin)['artifact']['sha256'])" 2>&1) || {
    echo "ERROR: ${file} — failed to fetch/parse Galaxy API hash (possible API compromise or network error)" >&2
    echo "       Verify network connectivity and API availability before retrying." >&2
    FAIL=1
    continue
  }

  local_hash=$(sha256sum "$filepath" 2>/dev/null | awk '{print $1}' || echo "")

  if [ -z "$api_hash" ]; then
    # This branch should never execute now (errors are caught above), but retained for defense-in-depth
    echo "ERROR: ${file} — Galaxy API hash is empty (unexpected)" >&2
    FAIL=1
  elif [ -z "$local_hash" ]; then
    echo "ERROR: ${file} — local file missing or unreadable"
    FAIL=1
  elif [ "$api_hash" = "$local_hash" ]; then
    echo "✓ ${file} matches Galaxy API hash"
  else
    echo "ERROR: ${file} — MISMATCH (possible supply chain tampering)"
    FAIL=1
  fi
done

[ "$FAIL" -eq 1 ] && { echo "Collection Galaxy API verification failed"; exit 1; }
exit 0
