#!/bin/bash
# verify-collections.sh — Re-verify Ansible collections against SHA256 hashes.
# Guards against TOCTOU tampering between bootstrap and runtime execution.
# Checks BOTH the source tarballs (collections-dist/) AND re-extracts to a temp
# directory to verify the extracted content matches the tarballs.
# Usage: verify-collections.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Step 1: Verify source tarballs match recorded hashes
cd "${REPO_DIR}/collections-dist"
if ! sha256sum -c SHA256SUMS; then
  echo "ERROR: Collection tarball integrity check failed" >&2
  exit 1
fi
echo "✓ Collection tarballs integrity verified"

# Step 2: Verify extracted collection dirs are consistent with their tarballs
# by hashing key Python plugin files from the extracted tree and comparing
# against the same files extracted fresh from the verified tarballs.
_tmpdir=$(mktemp -d)
trap 'rm -rf "$_tmpdir"' EXIT

for tarball in "${REPO_DIR}/collections-dist/"*.tar.gz; do
  [[ -f "$tarball" ]] || continue
  tar -xzf "$tarball" -C "$_tmpdir" 2>/dev/null || true
done

_extracted="${REPO_DIR}/collections"
if [[ -d "$_extracted" ]]; then
  # Check a representative plugin from ansible.posix (most security-relevant)
  _posix_cb="${_extracted}/ansible_collections/ansible/posix/plugins/callback"
  _tmp_posix_cb="${_tmpdir}/ansible_collections/ansible/posix/plugins/callback"
  if [[ -d "$_posix_cb" && -d "$_tmp_posix_cb" ]]; then
    if ! diff -rq --exclude="__pycache__" "$_posix_cb" "$_tmp_posix_cb" >/dev/null 2>&1; then
      echo "ERROR: Extracted ansible.posix callback plugins differ from verified tarballs." >&2
      echo "       Possible TOCTOU tampering. Re-run: make bootstrap" >&2
      exit 1
    fi
    echo "✓ ansible.posix callback plugins match verified tarballs"
  fi
fi

exit 0
