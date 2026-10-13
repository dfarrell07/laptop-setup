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
# script compares runtime Python filenames and SHA256 content hashes from:
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
	"community/general/13.5.0/ansible-collections/community.general/community-general-13.5.0.tar.gz"
	"community/library_inventory_filtering_v1/1.1.5/ansible-collections/community.library_inventory_filtering/community-library_inventory_filtering_v1-1.1.5.tar.gz"
	"containers/podman/1.21.1/containers/ansible-podman-collections/containers-podman-1.21.1.tar.gz"
)

FAILED=0
SKIPPED=0

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
		echo "FAIL ${filename}: vendored file not found"
		FAILED=1
		continue
	fi

	echo "Verifying ${namespace}/${name} ${version}..."

	# Clone upstream repository
	echo "  Cloning upstream: https://github.com/${github_repo}.git (tag: ${version})"
	if ! git clone --depth 1 --branch "$version" "https://github.com/${github_repo}.git" "$upstream_dir" 2>/dev/null; then
		rm -rf "$upstream_dir"
		if git clone --depth 1 --branch "v${version}" "https://github.com/${github_repo}.git" "$upstream_dir" 2>/dev/null; then
			: # Upstream uses a v-prefixed release tag.
		else
			echo "  WARN: Could not clone upstream tag ${version} from github.com/${github_repo}"
			echo "    This may indicate: (1) tag does not exist, (2) repository moved/renamed, (3) network issue"
			echo "    ACTION: Manually verify at https://github.com/${github_repo}/releases/tag/${version}"
			echo "    If tag exists and has expected files, continue. If compromised, contact collection maintainers."
			echo ""
			SKIPPED=$((SKIPPED + 1))
			continue
		fi
	fi

	# Extract Python module lists and compare using Python
	if ! python3 - "$vendored_file" "$upstream_dir" <<'PYEOF'
import sys
import tarfile
import os
from pathlib import Path

vendored_tar = sys.argv[1]
upstream_dir = sys.argv[2]

import hashlib

runtime_dirs = ('plugins/', 'module_utils/', 'roles/')

def digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(block)
    return result.hexdigest()

vendored_py = {}
try:
    with tarfile.open(vendored_tar, 'r:gz') as tar:
        for member in tar.getmembers():
            if not (member.name.endswith('.py') and member.name.startswith(runtime_dirs)):
                continue
            if not member.isfile() or member.name in vendored_py or '..' in Path(member.name).parts:
                raise ValueError(f'Invalid or duplicate Python archive member: {member.name}')
            with tar.extractfile(member) as stream:
                vendored_py[member.name] = digest(stream)

    upstream_py = {}
    for root, dirs, files in os.walk(upstream_dir):
        dirs[:] = [directory for directory in dirs if directory != '.git']
        for filename in files:
            path = Path(root) / filename
            relative = path.relative_to(upstream_dir).as_posix()
            if not (relative.endswith('.py') and relative.startswith(runtime_dirs)):
                continue
            if path.is_symlink():
                raise ValueError(f'Unexpected upstream Python symlink: {relative}')
            with path.open('rb') as stream:
                upstream_py[relative] = digest(stream)
except Exception as error:
    print(f'ERROR: Cannot compare runtime Python files: {error}', file=sys.stderr)
    sys.exit(1)

added = vendored_py.keys() - upstream_py.keys()
removed = upstream_py.keys() - vendored_py.keys()
modified = {path for path in vendored_py.keys() & upstream_py.keys()
            if vendored_py[path] != upstream_py[path]}
if added or removed or modified:
    print(f'ERROR: Runtime Python differs in {Path(vendored_tar).name}', file=sys.stderr)
    for label, paths in [('added', added), ('removed', removed), ('modified', modified)]:
        for path in sorted(paths)[:5]:
            print(f'  {label}: {path}', file=sys.stderr)
    sys.exit(1)
print(f'PASS: {len(vendored_py)} runtime Python files match upstream SHA256 hashes')
sys.exit(0)
PYEOF
	then
		echo "  FAIL: Mismatch detected"
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

if [ "$SKIPPED" -gt 0 ]; then
	echo "WARNING: Verification INCOMPLETE — ${SKIPPED} collection(s) could not be cloned from GitHub."
	echo "  An attacker who compromises the Galaxy CDN/API and blocks GitHub can exploit this gap."
	echo "  ACTION: Do NOT commit vendored collections until all upstream clones succeed."
	echo "  Retry when GitHub is reachable, or manually audit the affected collection(s) above."
	echo "  See SECURITY.md § 'Ansible Collections Supply Chain' for remediation steps."
	exit 1
fi

echo "All runtime Python files verified against upstream GitHub release tags."
echo "Review and commit: git add collections-dist/ && git commit -s (add -S when .gpg-enforcement is enabled)"
exit 0
