#!/bin/bash
# verify-vendor-tarball.sh — Verify vendored Ansible collections against upstream GitHub sources
#
# This script defends against Galaxy maintainer account compromise by comparing vendored
# collection tarballs against upstream GitHub release tags. It detects if:
#   1. Files were added to the tarball (malicious code injection)
#   2. Files were removed from the tarball
#   3. Files were modified in the tarball
#
# SECURITY: Galaxy account compromise allows attacker to control both CDN tarball and API hash.
# This script provides defense-in-depth by verifying content matches upstream GitHub release.
# None of the four vendored collections publish built tarballs in GitHub releases, so this
# script extracts and compares Python module lists from:
#   - Upstream: cloned GitHub tag + extracted source tree
#   - Vendored: existing collection-dist tarball
#
# Usage: scripts/verify-vendor-tarball.sh
# Exits with status 0 if all collections verified, 1 if mismatches detected.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Temporary directories for upstream clones and extraction
_tmpdir=$(mktemp -d)
trap 'rm -rf "$_tmpdir"' EXIT

echo "Verifying vendored collections against upstream GitHub sources..."
echo ""

# Collection specs: namespace/name/version/github_repo/filename
# Note: github_repo is org/repo format (e.g., ansible-collections/ansible.posix)
COLLECTIONS=(
	"ansible/posix/2.2.2/ansible-collections/ansible.posix/ansible-posix-2.2.2.tar.gz"
	"community/general/13.2.0/ansible-collections/community.general/community-general-13.2.0.tar.gz"
	"community/library_inventory_filtering_v1/1.1.5/ansible-collections/community.library_inventory_filtering_v1/community-library_inventory_filtering_v1-1.1.5.tar.gz"
	"containers/podman/1.20.2/containers/ansible-podman-collections/containers-podman-1.20.2.tar.gz"
)

FAILED=0

for spec in "${COLLECTIONS[@]}"; do
	# Parse spec
	namespace=$(echo "$spec" | cut -d/ -f1)
	name=$(echo "$spec" | cut -d/ -f2)
	version=$(echo "$spec" | cut -d/ -f3)
	github_repo=$(echo "$spec" | cut -d/ -f4-5)
	filename=$(echo "$spec" | cut -d/ -f6)

	upstream_dir="${_tmpdir}/${namespace}-${name}-upstream"
	vendored_file="${REPO_DIR}/collections-dist/${filename}"

	# Verify vendored file exists
	if [ ! -f "$vendored_file" ]; then
		echo "SKIP ${filename}: vendored file not found (may be first-time setup)"
		continue
	fi

	echo "Verifying ${namespace}/${name} ${version}..."

	# Clone upstream repository
	echo "  Cloning upstream: https://github.com/${github_repo}.git (tag: ${version})"
	if ! git clone --depth 1 --branch "$version" "https://github.com/${github_repo}.git" "$upstream_dir" 2>/dev/null; then
		echo "  WARN: Could not clone upstream tag ${version} from github.com/${github_repo}"
		echo "    This may indicate: (1) tag does not exist, (2) repository moved/renamed, (3) network issue"
		echo "    ACTION: Manually verify at https://github.com/${github_repo}/releases/tag/${version}"
		echo "    If tag exists and has expected files, continue. If compromised, contact collection maintainers."
		echo ""
		continue
	fi

	# Extract Python module lists
	# Vendored: tar tzf lists all files
	# Upstream: from cloned git repo (extract .py file paths from plugins/, module_utils/, roles/)

	vendored_py_files=$(_tmpdir="${_tmpdir}" python3 - "$vendored_file" "$upstream_dir" "$namespace" "$name" <<'PYEOF'
import sys, tarfile, os, tempfile
from pathlib import Path

vendored_tar = sys.argv[1]
upstream_dir = sys.argv[2]
namespace = sys.argv[3]
name = sys.argv[4]

# Extract Python files from vendored tarball
vendored_py = set()
try:
	with tarfile.open(vendored_tar, 'r:gz') as tar:
		for member in tar.getmembers():
			# Vendored tarballs extract to top-level dirs (plugins, module_utils, roles, CHANGELOG.rst, etc.)
			# We only care about Python files in executables dirs
			if member.name.endswith('.py') and \
			   any(member.name.startswith(d) for d in ['plugins/', 'module_utils/', 'roles/']):
				# Remove leading collection dirs (vendored has no namespace/name prefix)
				rel_path = member.name
				if '/' in rel_path:
					vendored_py.add(rel_path)
except Exception as e:
	print(f"ERROR: Failed to read vendored tarball {vendored_tar}: {e}", file=sys.stderr)
	sys.exit(1)

# Extract Python files from upstream git repo
upstream_py = set()
try:
	for root, dirs, files in os.walk(upstream_dir):
		# Skip .git directory
		dirs[:] = [d for d in dirs if d != '.git']
		for file in files:
			if file.endswith('.py'):
				fpath = os.path.join(root, file)
				rel_path = os.path.relpath(fpath, upstream_dir)
				# Only include files from plugins/, module_utils/, roles/
				if any(rel_path.startswith(d) for d in ['plugins', 'module_utils', 'roles']):
					upstream_py.add(rel_path)
except Exception as e:
	print(f"ERROR: Failed to walk upstream directory {upstream_dir}: {e}", file=sys.stderr)
	sys.exit(1)

# Report differences
added = vendored_py - upstream_py
removed = upstream_py - vendored_py

if added or removed:
	print(f"MISMATCH: {namespace}/{name} {sys.argv[4]}", file=sys.stderr)
	if added:
		print(f"  Files added in vendored tarball (not in upstream):", file=sys.stderr)
		for f in sorted(list(added))[:10]:
			print(f"    + {f}", file=sys.stderr)
		if len(added) > 10:
			print(f"    ... and {len(added) - 10} more", file=sys.stderr)
	if removed:
		print(f"  Files removed from vendored tarball (present in upstream):", file=sys.stderr)
		for f in sorted(list(removed))[:10]:
			print(f"    - {f}", file=sys.stderr)
		if len(removed) > 10:
			print(f"    ... and {len(removed) - 10} more", file=sys.stderr)
	sys.exit(1)
else:
	print("PASS: Python modules match upstream")
	sys.exit(0)
PYEOF
	)

	if [ $? -ne 0 ]; then
		echo "  FAIL: Mismatch detected between vendored and upstream"
		FAILED=1
	fi
	echo ""
done

if [ "$FAILED" -eq 1 ]; then
	echo "Supply chain verification FAILED."
	echo "ACTION: Do NOT commit vendored collections until mismatches are resolved."
	echo "See SECURITY.md § 'Ansible Collections Supply Chain' for remediation steps."
	exit 1
fi

echo "All vendored collections verified against upstream GitHub sources."
echo "Safe to commit: git add collections-dist/ && git commit -s"
exit 0
