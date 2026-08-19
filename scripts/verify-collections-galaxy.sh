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

  api_hash=$(curl -sf "${GALAXY_API}/${ns}/${name}/versions/${ver}/" | python3 -c "import sys,json; print(json.load(sys.stdin)['artifact']['sha256'])" 2>/dev/null || echo "")
  local_hash=$(sha256sum "$filepath" 2>/dev/null | awk '{print $1}' || echo "")

  if [ -z "$api_hash" ]; then
    echo "WARN: ${file} — could not fetch Galaxy API hash"
    continue
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
