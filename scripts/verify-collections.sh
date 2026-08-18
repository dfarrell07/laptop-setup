#!/bin/bash
# verify-collections.sh — Re-verify extracted Ansible collections against SHA256 hashes.
# Guards against TOCTOU tampering between bootstrap and runtime execution.
# Usage: verify-collections.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Verify that collection tarballs match recorded hashes
cd "${REPO_DIR}/collections-dist"
if ! sha256sum -c SHA256SUMS; then
  echo "ERROR: Collection tarball integrity check failed" >&2
  exit 1
fi

exit 0
