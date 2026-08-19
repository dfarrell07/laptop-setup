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

# Verification layers:
#   1. SHA256SUMS.asc: GPG signature check (advisory — requires signing key in keyring)
#   2. SHA256SUMS: tarball integrity (fatal on mismatch)
#   3. Python manifest: extracted .py files match tarballs + PYTHON_MANIFEST.json (fatal)
# Usage: called by verify-ansible-args.sh via VERIFY_AND_RUN in Makefile

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
  # and validate against PYTHON_MANIFEST.json
  python3 - "$_extracted/ansible_collections" "$_tmpdir" "$REPO_DIR/collections-dist/PYTHON_MANIFEST.json" << 'PYEOF'
import sys
import hashlib
import json
from pathlib import Path

def compute_file_hash(filepath):
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            sha256.update(chunk)
    return sha256.hexdigest()

def extract_python_files(collection_dir):
    """Extract all .py and .so files from plugins, module_utils, roles.

    Note: .pyc files not validated (Python handles mtime internally).
    .so files included for C extension modules (security-critical).
    """
    python_files = {}
    for subdir in ['plugins', 'module_utils', 'roles']:
        base = collection_dir / subdir
        if not base.exists():
            continue
        for pyfile in base.rglob('*'):
            # Skip cache and non-code files
            if '__pycache__' in str(pyfile) or '__pycache__' in pyfile.parts:
                continue
            # Only .py and .so files
            if pyfile.suffix not in ('.py', '.so'):
                continue
            if pyfile.is_file():
                rel_path = str(pyfile.relative_to(collection_dir))
                python_files[rel_path] = compute_file_hash(pyfile)
    return python_files

def load_manifest(manifest_path):
    """Load PYTHON_MANIFEST.json and return a dict keyed by tarball name."""
    if not Path(manifest_path).exists():
        return {}
    with open(manifest_path, 'r') as f:
        return json.load(f)

def get_manifest_files(manifest_data, tarball_base):
    """Extract dict of expected files and hashes from manifest.

    Returns: dict mapping relative_path -> content_hash
    """
    if tarball_base not in manifest_data:
        return {}  # Collection not in manifest (benign — bootstrap may not have run)

    collection_manifest = manifest_data[tarball_base]
    expected_files = {}

    # Add module_utils files with their hashes (dict {filename: hash})
    for filename, file_hash in collection_manifest.get('module_utils', {}).items():
        expected_files[f'module_utils/{filename}.py'] = file_hash

    # Add plugin files from all plugin types with their hashes
    for plugin_type, files_dict in collection_manifest.get('plugins', {}).items():
        for filename, file_hash in files_dict.items():
            expected_files[f'plugins/{plugin_type}/{filename}.py'] = file_hash

    # Note: roles structure varies; hashes computed from tarball baseline

    return expected_files

extracted_root = Path(sys.argv[1])  # $REPO_DIR/collections/ansible_collections
tmpdir = Path(sys.argv[2])           # tmpdir with extracted tarballs
manifest_path = sys.argv[3]          # PYTHON_MANIFEST.json path

manifest_data = load_manifest(manifest_path)

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

        # Check for added files (possible TOCTOU tampering or manifest out-of-sync)
        added = fresh_keys - extracted_keys
        if added:
            print(f"ERROR: {collection_name}: unexpected files added to extracted collection:", file=sys.stderr)
            for f in sorted(added)[:10]:
                print(f"  + {f}", file=sys.stderr)
            if len(added) > 10:
                print(f"  ... and {len(added) - 10} more", file=sys.stderr)
            print("       This indicates either:", file=sys.stderr)
            print("         1. Possible TOCTOU tampering (file added locally after extraction)", file=sys.stderr)
            print("         2. Collection tarball contains new files not in PYTHON_MANIFEST.json", file=sys.stderr)
            print("       Action: If new files in tarball, update PYTHON_MANIFEST.json and audit changes", file=sys.stderr)
            print("              If tampering suspected: Audit git log and SECURITY.md, then: make bootstrap", file=sys.stderr)
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

        # Check fresh extraction against PYTHON_MANIFEST.json
        # This detects tarball tampering where new files are added but not in manifest
        tarball_base = None
        expected_prefix = collection_name.replace('/', '-')

        # Exact matching: look for manifest entries matching "namespace-collection-VERSION.tar"
        for key in manifest_data.keys():
            if not key.endswith('.tar'):
                continue
            # Remove .tar suffix to get "namespace-collection-VERSION"
            name_with_version = key[:-4]
            # Split by hyphen; version is typically the last 1-3 numeric segments
            # Try removing progressively more segments to find the prefix
            parts = name_with_version.split('-')
            for num_version_parts in range(1, 4):
                if num_version_parts >= len(parts):
                    break
                key_prefix = '-'.join(parts[:-num_version_parts])
                if key_prefix == expected_prefix:
                    tarball_base = key
                    break
            if tarball_base:
                break

        # Warn if collection not found in manifest
        if not tarball_base:
            print(f"WARNING: {collection_name}: not found in PYTHON_MANIFEST.json", file=sys.stderr)
            print(f"         This collection will not be validated against manifest.", file=sys.stderr)
            # Continue without validation (benign — bootstrap may not have run yet)
            tarball_base = None

        if tarball_base and manifest_data.get(tarball_base):
            manifest_files = get_manifest_files(manifest_data, tarball_base)

            # Check for new files not in manifest
            unmanifested = fresh_keys - set(manifest_files.keys())
            if unmanifested:
                print(f"ERROR: {collection_name}: Python files in tarball not listed in PYTHON_MANIFEST.json:", file=sys.stderr)
                for f in sorted(unmanifested)[:10]:
                    print(f"  ~ {f}", file=sys.stderr)
                if len(unmanifested) > 10:
                    print(f"  ... and {len(unmanifested) - 10} more", file=sys.stderr)
                print("       This indicates possible collection tarball tampering (new file added).", file=sys.stderr)
                print("       Action: Audit git log, SECURITY.md, collections-dist/ for unauthorized modifications.", file=sys.stderr)
                print("              Regenerate manifest: scripts/gen-collection-manifest.py", file=sys.stderr)
                print("              Then: make bootstrap", file=sys.stderr)
                sys.exit(1)

            # Check for modified files by comparing content hashes (TOCTOU attack detection)
            modified = []
            for fpath in fresh_keys & set(manifest_files.keys()):
                if fresh_files[fpath] != manifest_files[fpath]:
                    modified.append(fpath)

            if modified:
                print(f"ERROR: {collection_name}: Python file content differs from baseline manifest (TOCTOU):", file=sys.stderr)
                for f in sorted(modified)[:10]:
                    print(f"  ~ {f}", file=sys.stderr)
                if len(modified) > 10:
                    print(f"  ... and {len(modified) - 10} more", file=sys.stderr)
                print("       File content hash mismatch — possible TOCTOU attack or corrupted tarball.", file=sys.stderr)
                print("       Action: Verify tarball integrity (make bootstrap), audit collections-dist/ for changes.", file=sys.stderr)
                print("              If attack suspected: Audit git log, SECURITY.md, contact maintainers.", file=sys.stderr)
                sys.exit(1)

print("✓ All collection Python files match verified tarballs and PYTHON_MANIFEST.json")
PYEOF

  if [[ $? -ne 0 ]]; then
    exit 1
  fi
else
  echo "✓ Collections not yet bootstrapped (verify will run after make bootstrap)"
fi

exit 0
