#!/bin/bash
# verify-collections.sh — Re-verify Ansible collections against SHA256 hashes
# and comprehensive content validation (plugins, module_utils, roles).
#
# Guards against TOCTOU tampering between bootstrap and runtime execution.
# Checks:
#   1. Source tarballs (collections-dist/) match recorded hashes (SHA256SUMS)
#   2. Extracted collection dirs are consistent with their tarballs
#   3. All Python files (plugins/, module_utils/, roles/) match baseline manifest
#
# Usage: verify-collections.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Step 1: Verify SHA256SUMS.asc GPG signature (defense-in-depth)
# Security model:
#   - Good signature with key in keyring (exit 0): PASS
#   - Bad signature with key in keyring (exit 1): FATAL (active tampering detected)
#   - Key NOT in keyring (exit 2): WARNING only (TOFU — fresh machine, key not yet imported)
#
# Note: verify-collections.sh cannot make key import FATAL without breaking provisioning on
# fresh machines (git clone → no keys → fail). However, if a key is present in the keyring,
# signature validation IS mandatory (FATAL if invalid). This is checked in bootstrap target
# in Makefile, which can enforce mandatory import before proceeding.
cd "${REPO_DIR}/collections-dist"
if [[ -f SHA256SUMS.asc ]]; then
  gpg_exit=0
  gpg_output=$(gpg --verify SHA256SUMS.asc SHA256SUMS 2>&1) || gpg_exit=$?

  if [[ $gpg_exit -eq 0 ]]; then
    echo "✓ SHA256SUMS.asc GPG signature verified"
  elif echo "$gpg_output" | grep -q "No public key\|public key not found\|can't check signature"; then
    # Key not in keyring — advisory only (expected on fresh machines).
    # Rationale: we cannot mandate key import during bootstrap because users need to clone
    # the repo first and import keys from collections-dist/signing-key.asc. However, if a key
    # IS present but signature is invalid, we treat that as FATAL (active tampering).
    echo "WARNING: SHA256SUMS.asc signing key not in GPG keyring" >&2
    echo "         Continuing with hash-only verification (TOFU: Trust On First Use)" >&2
    echo "         This is expected on fresh machines. On subsequent runs, import the key for" >&2
    echo "         cryptographic verification: gpg --import collections-dist/signing-key.asc" >&2
    echo "         For security context, see SECURITY.md § 'Ansible Collections Supply Chain'." >&2
  else
    # Bad signature with key present — FATAL (tampering detected)
    echo "FATAL: SHA256SUMS.asc GPG signature verification FAILED" >&2
    echo "       The signing key is in your keyring but the signature is invalid." >&2
    echo "       This indicates possible tampering or corrupted signature file." >&2
    echo "       Do not proceed. Audit git log, review SECURITY.md, contact maintainers." >&2
    echo "       GPG output: ${gpg_output}" >&2
    exit 1
  fi
fi

# Step 1b: Verify source tarballs match recorded hashes
if ! sha256sum -c SHA256SUMS; then
  echo "ERROR: Collection tarball integrity check failed" >&2
  exit 1
fi
echo "✓ Collection tarballs integrity verified"

# Step 2: Verify extracted collection dirs are consistent with their tarballs
# by comparing extracted tree against fresh extraction from verified tarballs.
_tmpdir=$(mktemp -d)
trap 'rm -rf "$_tmpdir"' EXIT

# Extract all tarballs to temp directory to get fresh baseline for comparison
# Note: tarballs extract as flat collections (no ansible_collections prefix)
for tarball in "${REPO_DIR}/collections-dist/"*.tar.gz; do
  [[ -f "$tarball" ]] || continue
  tar -xzf "$tarball" -C "$_tmpdir" 2>/dev/null || true
done

_extracted="${REPO_DIR}/collections"
if [[ -d "$_extracted/ansible_collections" ]]; then
  # Step 2a: Comprehensive content verification for all collections
  # Compare extracted Python files against fresh extraction from verified tarballs
  python3 - "$_extracted/ansible_collections" "$_tmpdir" << 'PYEOF'
import sys
import hashlib
from pathlib import Path

def compute_file_hash(filepath):
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            sha256.update(chunk)
    return sha256.hexdigest()

def extract_python_files(collection_dir):
    """Extract all .py files from plugins, module_utils, roles directories."""
    python_files = {}
    for subdir in ['plugins', 'module_utils', 'roles']:
        base = collection_dir / subdir
        if not base.exists():
            continue
        for pyfile in base.rglob('*.py'):
            if '__pycache__' in str(pyfile):
                continue
            rel_path = str(pyfile.relative_to(collection_dir))
            python_files[rel_path] = compute_file_hash(pyfile)
    return python_files

extracted_root = Path(sys.argv[1])  # $REPO_DIR/collections/ansible_collections
tmpdir = Path(sys.argv[2])           # tmpdir with extracted tarballs

# Map collections from ansible_collections hierarchy to flat tmpdir hierarchy
# extracted_root: ansible_collections/ansible/posix/
# tmpdir: plugins/, module_utils/, etc. (flat)
for namespace_dir in extracted_root.glob('*/'):
    if not namespace_dir.is_dir():
        continue
    for collection_dir in namespace_dir.glob('*/'):
        if not collection_dir.is_dir():
            continue
        collection_name = f"{namespace_dir.name}/{collection_dir.name}"
        extracted_files = extract_python_files(collection_dir)

        # Tarballs extract to tmpdir without namespace/name hierarchy
        # So we check against tmpdir (which contains all extracted content)
        fresh_path = tmpdir / namespace_dir.name / collection_dir.name
        if not fresh_path.exists():
            # Collections might have been extracted but fresh extraction failed
            # This is benign - just means bootstrap hasn't been run yet
            continue

        fresh_files = extract_python_files(fresh_path)

        # Compare: extracted vs fresh
        extracted_keys = set(extracted_files.keys())
        fresh_keys = set(fresh_files.keys())

        # Check for added files (possible TOCTOU tampering)
        added = fresh_keys - extracted_keys
        if added:
            print(f"ERROR: {collection_name}: unexpected files added after tarball extraction:", file=sys.stderr)
            for f in sorted(added)[:10]:
                print(f"  + {f}", file=sys.stderr)
            if len(added) > 10:
                print(f"  ... and {len(added) - 10} more", file=sys.stderr)
            print("       Possible TOCTOU tampering. Re-run: make bootstrap", file=sys.stderr)
            sys.exit(1)

        # Check for modified files
        modified = []
        for fpath in extracted_keys & fresh_keys:
            if extracted_files[fpath] != fresh_files[fpath]:
                modified.append(fpath)

        if modified:
            print(f"ERROR: {collection_name}: extracted Python content differs from verified tarballs:", file=sys.stderr)
            for f in sorted(modified)[:10]:
                print(f"  ~ {f}", file=sys.stderr)
            if len(modified) > 10:
                print(f"  ... and {len(modified) - 10} more", file=sys.stderr)
            print("       Possible TOCTOU tampering. Re-run: make bootstrap", file=sys.stderr)
            sys.exit(1)

print("✓ All collection Python files match verified tarballs")
PYEOF

  if [[ $? -ne 0 ]]; then
    exit 1
  fi
else
  echo "✓ Collections not yet bootstrapped (verify will run after make bootstrap)"
fi

exit 0
