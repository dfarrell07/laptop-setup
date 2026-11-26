#!/bin/bash
# verify-collections.sh — Re-verify Ansible collections against SHA256 hashes
# and comprehensive content validation (plugins, module_utils, roles).
#
# Guards against TOCTOU tampering between bootstrap and runtime execution.
# Checks:
#   1. Source tarballs (collections-dist/) match recorded hashes (SHA256SUMS)
#   2. Extracted collection dirs are consistent with their tarballs
#   3. Every installed entry (any file type, symlink, special file) matches its tarball; runtime Python matches the
#      committed baseline manifest
#
# Usage: verify-collections.sh
set -euo pipefail

# Verification layers:
#   1. SHA256SUMS.asc: GPG signature check when repository policy enables it
#   2. SHA256SUMS: tarball integrity (fatal on mismatch)
#   3. Installed tree (once collections/ exists): every entry of every collection, whatever its type or extension,
#      must be in its re-extracted tarball with the same content (files), target (symlinks) and type; runtime .py/.so
#      also match PYTHON_MANIFEST.json. Anything else in ansible_collections/ or a namespace directory, a symlink in
#      plugins/, module_utils/ or roles/, a vendored collection that is not installed, or a collections/ without
#      ansible_collections/ (an interrupted install) is fatal. Ansible's __pycache__/*.pyc caches are removed first.
# Usage: called by verify-ansible-args.sh (the Makefile's VERIFY_AND_RUN wrapper)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Step 1: Follow tracked GPG policy; the hash layers always run (the installed-tree layer needs collections/).
# Security model when .gpg-enforcement is enabled:
#   - Good signature with key in keyring (exit 0): PASS
#   - Bad signature with key in keyring (exit 1): FATAL (active tampering detected)
#   - Key NOT in keyring (gpg reports no public key): FATAL — SHA256SUMS is git-tracked and can be replaced
#     atomically with a malicious tarball; GPG is the only backstop against this.
#
# The bundled key and signature are placeholders. Enforced mode needs a real owner
# key and a replacement checksum signature; see SECURITY.md for setup.
# To bypass the missing-key case only (non-production, CI without key material): set SKIP_GPG_VERIFY=1
# (a bad signature with the key present is always fatal)
cd "${REPO_DIR}/collections-dist"
_gpg_policy=$("${REPO_DIR}/scripts/gpg-policy.sh")
if [[ "$_gpg_policy" == disabled ]]; then
  echo "INFO: Collection GPG verification disabled by .gpg-enforcement; hashes and content checks remain active"
else
  if [[ ! -f SHA256SUMS.asc ]]; then
    echo "FATAL: Required checksum signature SHA256SUMS.asc is missing" >&2
    exit 1
  fi
  if ! command -v gpg >/dev/null 2>&1; then
    echo "FATAL: gpg is required for collection signature verification; install GnuPG and ensure it is on PATH" >&2
    exit 1
  fi
  if [[ -f SHA256SUMS.asc ]]; then
    # Pin GPG to the real user keyring regardless of inherited environment.
    # An attacker can set GNUPGHOME=/tmp/evil, pre-import their own key, and
    # sign a malicious SHA256SUMS — gpg would exit 0 without this guard.
    unset GNUPGHOME GPG_AGENT_INFO
    gpg_exit=0
    gpg_output=$(LC_ALL=C gpg --verify SHA256SUMS.asc SHA256SUMS 2>&1) || gpg_exit=$?

    if [[ $gpg_exit -eq 0 ]]; then
      echo "✓ SHA256SUMS.asc GPG signature verified"
    elif echo "$gpg_output" | grep -q "No public key\|public key not found\|can't check signature"; then
      if [[ "${SKIP_GPG_VERIFY:-0}" == "1" ]]; then
        echo "WARNING: SHA256SUMS.asc signing key not in GPG keyring (SKIP_GPG_VERIFY=1 — bypassed)" >&2
      else
        # Key absent → FATAL. SHA256SUMS is git-tracked; an attacker with commit access or a
        # git-clone MITM can replace both the tarball and SHA256SUMS atomically. The SHA256
        # check then passes, and without GPG there is no cryptographic backstop.
        echo "FATAL: SHA256SUMS.asc signing key not in GPG keyring" >&2
        echo "       GPG verification is mandatory — SHA256SUMS alone cannot detect a" >&2
        echo "       malicious tarball+hash replacement by a committer or MITM." >&2
        echo "       The bundled key/signature are placeholders. Configure a real owner key" >&2
        echo "       and checksum signature as described in SECURITY.md, then re-run." >&2
        echo "       CI/non-production bypass: export SKIP_GPG_VERIFY=1" >&2
        exit 1
      fi
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
fi  # Repository signing policy

# Step 1b: Verify source tarballs match recorded hashes
if command -v sha256sum >/dev/null 2>&1; then
  _checksum=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
  _checksum=(shasum -a 256)
else
  echo "FATAL: SHA256 verification requires sha256sum or shasum" >&2
  exit 1
fi
if ! "${_checksum[@]}" -c SHA256SUMS; then
  echo "ERROR: Collection tarball integrity check failed" >&2
  exit 1
fi
echo "✓ Collection tarballs integrity verified"

# Step 2: Verify extracted collection dirs are consistent with their tarballs
# by comparing extracted tree against fresh extraction from verified tarballs.
_tmpdir=$(mktemp -d)
trap 'rm -rf "$_tmpdir"' EXIT

# Extract all tarballs to namespace/collection subdirectories for comparison.
# Galaxy tarballs extract flat (plugins/, module_utils/ at top level).
# We extract each to tmpdir/namespace/collection/ so the Python check can
# locate fresh_path = tmpdir/namespace/collection for each installed collection.
# Tarball naming: namespace-collection-version.tar.gz (fields 0 and 1 are ns/col).
for tarball in "${REPO_DIR}/collections-dist/"*.tar.gz; do
  [[ -f "$tarball" ]] || continue
  # Exact filename comparison (not a regex), so '.' or '+' in a name cannot mis-match
  _want=$(TARBALL_NAME="$(basename "$tarball")" awk 'BEGIN { n = ENVIRON["TARBALL_NAME"] } { f = $2; sub(/^\*/, "", f); if (f == n && length($1) == 64) h = $1 } END { print h }' "${REPO_DIR}/collections-dist/SHA256SUMS")
  if [[ -z "$_want" ]]; then
    echo "ERROR: $(basename "$tarball") is not listed in SHA256SUMS — remove it or add a verified entry" >&2
    exit 1
  fi
  # Hash and extract the same bytes: a private copy, so the tarball cannot be swapped between the check and tar
  _copy="$_tmpdir/archive.tar.gz"
  cp "$tarball" "$_copy"
  _got=$("${_checksum[@]}" "$_copy" | awk '{ print $1 }')
  if [[ "$_got" != "$_want" ]]; then
    echo "ERROR: $(basename "$tarball") changed while it was being verified (SHA256 $_got, expected $_want)" >&2
    exit 1
  fi
  base="$(basename "$tarball" .tar.gz)"
  IFS='-' read -ra _parts <<< "$base"
  _ns="${_parts[0]}"
  _col="${_parts[1]}"
  mkdir -p "$_tmpdir/$_ns/$_col"
  if ! tar -xzf "$_copy" -C "$_tmpdir/$_ns/$_col"; then
    echo "ERROR: tar extraction failed for $tarball (possible path traversal or corrupt archive)" >&2
    exit 1
  fi
  rm -f "$_copy"
done

_extracted="${REPO_DIR}/collections"
if [[ -L "$_extracted" ]]; then
  echo "FATAL: ${_extracted} is a symlink — possible tampering" >&2
  exit 1
fi
if [[ ! -d "$_extracted/ansible_collections" ]] && { [[ -e "$_extracted/ansible_collections" ]] || [[ -L "$_extracted/ansible_collections" ]]; }; then
  echo "FATAL: ${_extracted}/ansible_collections exists but is not a directory (broken link or file) — possible tampering" >&2
  exit 1
fi
# Step 2a: one program for both trees. Every installed collection entry is compared with a fresh extraction of its
# verified tarball (and runtime Python with PYTHON_MANIFEST.json); bytecode caches beside the repo's own plugins are
# removed (Ansible would load a planted .pyc from there unchecked).
# read, not $(cat <<...): bash 3.2 (macOS) misparses quotes inside a heredoc in a command substitution
IFS= read -r -d '' _verifier_py << 'PYEOF' || true
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

# Every entry of every installed collection must be in its verified tarball (re-extracted fresh above), whatever its
# type or extension: Ansible loads extensionless modules, meta/runtime.yml redirects and collection roles too.
RUNTIME_DIRS = ('plugins', 'module_utils', 'roles')
HINT = "       Expected after pulling a collections-dist change; otherwise investigate. Then re-run: make collections"


def fail(message, paths=(), hint=HINT):
    print(f"ERROR: {message}", file=sys.stderr)
    paths = sorted(paths)
    for path in paths[:10]:
        print(f"  {path}", file=sys.stderr)
    if len(paths) > 10:
        print(f"  ... and {len(paths) - 10} more", file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    sys.exit(1)


def fail_entries(base, message, rels, hint=HINT):
    """fail(), unless some of the entries are gone by now: then the tree changed while it was being checked."""
    vanished = [rel for rel in rels if not os.path.lexists(os.path.join(base, rel))]
    if vanished:
        fail(f"{base}: entries changed during the check (concurrent Ansible run?):", vanished,
             hint="       Re-run once no other Ansible run is active; if this persists, investigate.")
    fail(message, rels, hint)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def scan(root):
    """Every entry below root, by relative path: ('file', sha256), ('link', target), ('dir',) or ('special', type)."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):  # does not descend into symlinked directories
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                mode = os.lstat(path).st_mode
                if stat.S_ISLNK(mode):
                    entries[rel] = ('link', os.readlink(path))
                elif stat.S_ISDIR(mode):
                    entries[rel] = ('dir',)
                elif stat.S_ISREG(mode):
                    entries[rel] = ('file', sha256(path))
                else:
                    entries[rel] = ('special', stat.S_IFMT(mode))
            except FileNotFoundError:  # removed while scanning (a concurrent run's bytecode cache): nothing to verify
                continue
    return entries


# A compiled file, or Python's temporary name for one before it renames it into place (name.pyc.<number>)
BYTECODE = re.compile(r'\.pyc(\.[0-9]+)?\Z')


def is_bytecode_cache(rel, entry, fresh_entries):
    """Ansible writes __pycache__/*.pyc next to controller plugins on every run: a __pycache__ directory the tarball
    does not ship, or a regular .pyc file directly in one."""
    parts = rel.split(os.sep)
    if parts[-1] == '__pycache__' and entry[0] == 'dir':
        return True
    return (len(parts) > 1 and parts[-2] == '__pycache__' and BYTECODE.search(rel) and entry[0] == 'file'
            and os.path.dirname(rel) not in fresh_entries)


def purge_bytecode(collection_dir, caches):
    """Bytecode cannot be checked against its source (a crafted .pyc whose header matches the source loads instead of
    it), so remove the caches; Ansible writes them again. False if a cache directory holds anything but .pyc files."""
    for rel in caches:
        path = os.path.join(collection_dir, rel)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        for _ in range(3):  # a concurrent run may write a new .pyc meanwhile; still there after three tries: fail
            try:
                children = [os.path.join(path, child) for child in os.listdir(path)]
            except FileNotFoundError:
                break
            if not all(BYTECODE.search(child) and os.path.isfile(child) and not os.path.islink(child)
                       for child in children):
                return False
            for child in children:
                try:
                    os.unlink(child)
                except FileNotFoundError:
                    pass
            try:
                os.rmdir(path)
                break
            except FileNotFoundError:
                break
            except OSError:
                continue
        else:
            return False
    return True


def runtime_python(entries):
    return {rel: entry[1] for rel, entry in entries.items()
            if entry[0] == 'file' and rel.split(os.sep, 1)[0] in RUNTIME_DIRS and rel.endswith(('.py', '.so'))}


def verify_collections(extracted_root, tmpdir, manifest_path):
    """extracted_root: $REPO_DIR/collections/ansible_collections; tmpdir: verified tarballs extracted to
    tmpdir/<namespace>/<collection>; manifest_path: PYTHON_MANIFEST.json, the committed runtime Python baseline."""
    manifest_data = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    if extracted_root.is_symlink():
        fail(f"symlink detected in vendored collections — possible tampering: {extracted_root}", hint=None)

    bytecode_removed = 0
    for top in [extracted_root] + [p for p in extracted_root.iterdir() if p.is_dir() and not p.is_symlink()]:
        if top.lstat().st_mode & 0o022:
            fail(f"{top} is group- or other-writable (chmod go-w it, or re-run make collections)")
    for namespace_dir in sorted(extracted_root.iterdir()):
        # Only namespace directories here, and only collection directories in them: a file at either level (a package
        # __init__.py, a planted plugin) is not part of any collection
        if namespace_dir.is_symlink() or not namespace_dir.is_dir():
            fail(f"unexpected entry in {extracted_root} (only namespace directories belong there): {namespace_dir.name}"
                 + (" (macOS Finder metadata: delete it)" if namespace_dir.name == '.DS_Store' else ""))
        for collection_dir in sorted(namespace_dir.iterdir()):
            collection_name = f"{namespace_dir.name}/{collection_dir.name}"
            if collection_dir.is_symlink() or not collection_dir.is_dir():
                fail(f"unexpected entry in {namespace_dir} (only collection directories belong there): {collection_dir.name}"
                     + (" (macOS Finder metadata: delete it)" if collection_dir.name == '.DS_Store' else ""))
            fresh_path = tmpdir / namespace_dir.name / collection_dir.name
            if not fresh_path.is_dir():
                fail(f"{collection_name}: directory exists on disk but has no corresponding verified tarball entry — "
                     "possible unauthorized collection injection")

            fresh = scan(fresh_path)
            installed = scan(collection_dir)
            # Content matches, but a group- or other-writable entry would let another account change it later
            writable = [rel for rel in [''] + list(installed)
                        if installed.get(rel, ('dir',))[0] != 'link'
                        and os.path.lexists(os.path.join(collection_dir, rel))
                        and os.lstat(os.path.join(collection_dir, rel)).st_mode & 0o022]
            if writable:
                fail(f"{collection_name}: group- or other-writable entries in the installed collection (chmod go-w them, or "
                     "re-run make collections):", [rel or '.' for rel in writable])
            caches = [rel for rel, entry in installed.items()
                      if rel not in fresh and is_bytecode_cache(rel, entry, fresh)]
            if caches:
                if not purge_bytecode(collection_dir, caches):
                    fail(f"{collection_name}: a __pycache__ directory holds more than .pyc files — possible tampering",
                         [rel for rel in installed if '__pycache__' in rel.split(os.sep) and rel not in caches])
                bytecode_removed += sum(1 for rel in caches if BYTECODE.search(rel))
                installed = {rel: entry for rel, entry in installed.items() if rel not in caches}

            links = [rel for rel, entry in {**fresh, **installed}.items()
                     if entry[0] == 'link' and rel.split(os.sep, 1)[0] in RUNTIME_DIRS]
            if links:
                fail(f"{collection_name}: symlink in plugins/, module_utils/ or roles/ — possible tampering", links)
            extra = set(installed) - set(fresh)
            if extra:
                finder = [rel for rel in extra if os.path.basename(rel) == '.DS_Store']
                fail(f"{collection_name}: installed collection has entries not in its verified tarball "
                     "(any file type, symlink or special file):", extra,
                     hint=("       macOS Finder metadata: delete the .DS_Store files listed (find collections -name .DS_Store "
                           "-delete), then re-run.\n" if finder else "") + HINT)
            missing = set(fresh) - set(installed)
            if missing:
                fail_entries(fresh_path, f"{collection_name}: verified tarball has entries missing from the installed "
                         "collection:", missing)
            changed = [rel for rel in installed if installed[rel] != fresh[rel]]
            if changed:
                fail(f"{collection_name}: installed entries differ from the verified tarball (content, link target or type):",
                     changed)

            # The committed baseline of runtime Python paths and hashes also catches a coordinated archive and install change
            with (fresh_path / 'MANIFEST.json').open() as metadata_file:
                metadata = json.load(metadata_file)['collection_info']
            manifest_key = f"{namespace_dir.name}-{collection_dir.name}-{metadata['version']}.tar"
            baseline = manifest_data.get(manifest_key, {}).get('runtime_files')
            if not isinstance(baseline, dict) or baseline != runtime_python(fresh):
                fail(f"{collection_name}: runtime paths or hashes differ from the committed Python manifest",
                     hint="       Regenerate the manifest only after verifying the archives against upstream sources.")

    for fresh_namespace in sorted(p for p in tmpdir.glob('*/') if p.is_dir()):
        for fresh_collection in sorted(p for p in fresh_namespace.glob('*/') if p.is_dir()):
            if not (extracted_root / fresh_namespace.name / fresh_collection.name).is_dir():
                fail(f"{fresh_namespace.name}/{fresh_collection.name} is vendored but not installed under collections/ "
                     "(interrupted install?); Ansible would load an unverified copy from another collections path",
                     hint="       Re-run: make collections")
    return bytecode_removed


def purge_plugin_bytecode(directory):
    """The repo's own plugin, library and module_utils directories (git-tracked sources): Ansible loads a .pyc from
    their __pycache__ when its header matches the source, so remove every such cache, as for the collections. A
    symlinked plugin or __pycache__ directory, or a cache holding anything but bytecode, is refused."""
    if not os.path.lexists(directory):  # library/ and module_utils/ are listed whether or not they exist
        return 0
    if os.path.islink(directory):
        fail(f"symlinked plugin directory — possible tampering: {directory}", hint=None)
    caches = []
    for dirpath, dirnames, _ in os.walk(directory):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if name == '__pycache__':
                if os.path.islink(path):
                    fail(f"symlinked __pycache__ directory — possible tampering: {path}", hint=None)
                caches.append(os.path.relpath(path, directory))
    removed = 0
    for rel in caches:
        try:
            removed += sum(1 for child in os.listdir(os.path.join(directory, rel)) if BYTECODE.search(child))
        except FileNotFoundError:
            continue
    if caches and not purge_bytecode(directory, caches):
        fail_entries(directory, "a __pycache__ directory beside the repo's plugins holds more than bytecode — possible "
                     "tampering", caches)
    return removed


extracted_arg, tmpdir_arg, manifest_arg, *plugin_dirs = sys.argv[1:]
bytecode_removed = 0
for plugin_dir in plugin_dirs:
    bytecode_removed += purge_plugin_bytecode(plugin_dir)
if extracted_arg:
    bytecode_removed += verify_collections(Path(extracted_arg), Path(tmpdir_arg), Path(manifest_arg))

if bytecode_removed:
    print(f"INFO: removed {bytecode_removed} cached .pyc files (bytecode cannot be verified)")
if extracted_arg:
    print("✓ All collection files match verified tarballs (every file type, symlinks included)")
PYEOF
shopt -s nullglob
_plugin_dirs=("$REPO_DIR"/roles/*/*_plugins "$REPO_DIR"/roles/*/library "$REPO_DIR"/roles/*/module_utils
              "$REPO_DIR"/*_plugins "$REPO_DIR"/library "$REPO_DIR"/module_utils)
shopt -u nullglob
if [[ -d "$_extracted/ansible_collections" ]]; then
  python3 -I -c "$_verifier_py" "$_extracted/ansible_collections" "$_tmpdir" "$REPO_DIR/collections-dist/PYTHON_MANIFEST.json" \
    ${_plugin_dirs[@]+"${_plugin_dirs[@]}"}

else
  python3 -I -c "$_verifier_py" "" "" "" ${_plugin_dirs[@]+"${_plugin_dirs[@]}"}
fi
if [[ -d "$_extracted/ansible_collections" ]]; then
  :
elif [[ -d "$_extracted" ]]; then
  # A fresh clone has no collections/ (gitignored); an empty one is an interrupted install, and Ansible would
  # silently load unverified copies from ~/.ansible or system paths.
  echo "FATAL: ${_extracted} has no ansible_collections/ (interrupted install?). Re-run: make collections" >&2
  exit 1
else
  echo "✓ Collections not yet installed (verify will run after make bootstrap or make collections)"
fi

exit 0
