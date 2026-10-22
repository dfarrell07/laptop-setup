#!/bin/bash
# verify-vendor-tarball.sh — Verify vendored Ansible collections against upstream GitHub sources
#
# This script defends against Galaxy maintainer account compromise by comparing vendored
# collection tarballs against upstream GitHub release tags. For the runtime paths
# (plugins/, module_utils/, roles/, meta/runtime.yml) it detects if:
#   1. Files were added to the tarball (malicious code injection)
#   2. Files were removed from the tarball
#   3. Files were modified in the tarball
# Other members (docs, MANIFEST.json, ...) are not compared.
#
# SECURITY: Galaxy account compromise allows attacker to control both CDN tarball and API hash.
# This script provides defense-in-depth by verifying content matches upstream GitHub release.
# None of the four vendored collections publish built tarballs in GitHub releases, so this
# script compares runtime filenames and SHA256 content hashes from:
#   - Upstream: cloned GitHub tag (or v-prefixed tag) + extracted source tree
#   - Vendored: existing collection-dist tarball
#
# Usage: scripts/verify-vendor-tarball.sh
# Exits with status 0 if all collections verified, 1 on any mismatch or rejected archive member, missing vendored
# file, tag that could not be cloned (verification incomplete), or missing timeout.
#
set -euo pipefail

if command -v timeout >/dev/null 2>&1; then
	_timeout=timeout
elif command -v gtimeout >/dev/null 2>&1; then
	_timeout=gtimeout
else
	echo "ERROR: 'timeout' not found. On macOS: brew install coreutils" >&2
	exit 1
fi

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
	"containers/podman/1.21.2/containers/ansible-podman-collections/containers-podman-1.21.2.tar.gz"
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
	if ! GIT_TERMINAL_PROMPT=0 "$_timeout" 120 git clone --depth 1 --branch "$version" "https://github.com/${github_repo}.git" "$upstream_dir" 2>/dev/null; then
		rm -rf "$upstream_dir"
		if GIT_TERMINAL_PROMPT=0 "$_timeout" 120 git clone --depth 1 --branch "v${version}" "https://github.com/${github_repo}.git" "$upstream_dir" 2>/dev/null; then
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
	if ! python3 -I - "$vendored_file" "$upstream_dir" <<'PYEOF'
import sys
import tarfile
import os
import posixpath
from pathlib import Path

vendored_tar = sys.argv[1]
upstream_dir = sys.argv[2]

import hashlib

runtime_dirs = ('plugins/', 'module_utils/', 'roles/')
runtime_files = ('meta/runtime.yml',)


def is_runtime(name):
    return name.startswith(runtime_dirs) or name in runtime_files


def is_runtime_path(name):
    """Runtime file, runtime directory itself, or a case variant of either."""
    lowered = name.lower()
    return is_runtime(lowered) or lowered in [d.rstrip('/') for d in runtime_dirs]

def digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(block)
    return result.hexdigest()

vendored_files = {}
try:
    with tarfile.open(vendored_tar, 'r:gz') as tar:
        members = tar.getmembers()
        # Names of links: nothing may be stored below one, because extraction would
        # write through the link to wherever it points (e.g. 'x -> .' aliases the root)
        link_names = {posixpath.normpath(m.name).lower() for m in members if m.issym() or m.islnk()}
        seen_names = set()
        for member in members:
            raw = member.name
            # Absolute names and '..' segments are never legitimate in a collection tarball
            if raw.startswith('/') or '..' in raw.split('/'):
                raise ValueError(f'Unsafe archive member name: {raw}')
            # tar extraction collapses '.', '//' and a leading './'
            name = posixpath.normpath(raw)
            if name == '.':
                continue
            # Case-folded: macOS extracts onto a case-insensitive filesystem
            if name.lower() in seen_names:
                raise ValueError(f'Duplicate archive member: {raw}')
            seen_names.add(name.lower())
            parts = name.lower().split('/')
            for depth in range(1, len(parts)):
                if '/'.join(parts[:depth]) in link_names:
                    raise ValueError(f'Archive member below a link: {raw}')
            if member.issym() or member.islnk():
                # A link must not point outside its own tree or into a runtime path,
                # otherwise later members would be written through it
                base = posixpath.dirname(name) if member.issym() else ''
                joined = posixpath.join(base, member.linkname)
                # Resolving through another link (or '..' after one) could land anywhere
                walked = joined.lower().split('/')
                for depth in range(1, len(walked) + 1):
                    if '/'.join(walked[:depth]) in link_names:
                        raise ValueError(f'Archive link resolves through another link: {raw} -> {member.linkname}')
                target = posixpath.normpath(joined)
                if target in ('', '.') or target.startswith('/') or target == '..' or target.startswith('../') or is_runtime_path(target):
                    raise ValueError(f'Archive link into runtime or outside tree: {raw} -> {member.linkname}')
            # Runtime detection is case-insensitive: macOS extracts onto a case-insensitive
            # filesystem, so 'Plugins/x' would land in 'plugins/'. A cased variant is then
            # recorded under its own name and reported as an added file.
            if not is_runtime_path(name):
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f'Invalid archive member: {raw}')
            with tar.extractfile(member) as stream:
                vendored_files[name] = digest(stream)

    upstream_files = {}
    for root, dirs, files in os.walk(upstream_dir):
        dirs[:] = [directory for directory in dirs if directory != '.git']
        for filename in files:
            path = Path(root) / filename
            relative = path.relative_to(upstream_dir).as_posix()
            if not is_runtime(relative):
                continue
            if path.is_symlink():
                raise ValueError(f'Unexpected upstream symlink: {relative}')
            with path.open('rb') as stream:
                upstream_files[relative] = digest(stream)
except Exception as error:
    print(f'ERROR: Cannot compare runtime files: {error}', file=sys.stderr)
    sys.exit(1)

if not vendored_files or not upstream_files:
    print(f'ERROR: No runtime files found to compare (vendored={len(vendored_files)}, upstream={len(upstream_files)})', file=sys.stderr)
    sys.exit(1)

added = vendored_files.keys() - upstream_files.keys()
removed = upstream_files.keys() - vendored_files.keys()
modified = {path for path in vendored_files.keys() & upstream_files.keys()
            if vendored_files[path] != upstream_files[path]}
if added or removed or modified:
    print(f'ERROR: Runtime files differ in {Path(vendored_tar).name}', file=sys.stderr)
    for label, paths in [('added', added), ('removed', removed), ('modified', modified)]:
        for path in sorted(paths)[:5]:
            print(f'  {label}: {path}', file=sys.stderr)
    sys.exit(1)
print(f'PASS: {len(vendored_files)} runtime files match upstream SHA256 hashes')
sys.exit(0)
PYEOF
	then
		echo "  FAIL: Comparison failed (mismatch or rejected archive member, see above)"
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

echo "All runtime files verified against upstream GitHub release tags."
echo "Review and commit: git add collections-dist/ && git commit -s (add -S when .gpg-enforcement is enabled)"
exit 0
