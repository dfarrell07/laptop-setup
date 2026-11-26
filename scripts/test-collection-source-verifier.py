#!/usr/bin/env python3
"""Verify collection source comparison, checksum tooling, GPG policy gating and committed-baseline checks."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parent.parent
os.umask(0o022)  # fixtures are created as make collections installs them; a group-writable tree is refused
SOURCE = (ROOT / 'scripts/verify-vendor-tarball.sh').read_text()
COMPARISON = SOURCE.split("<<'PYEOF'", 1)[1].split('\n', 1)[1].split('\nPYEOF', 1)[0]
BASELINE_SOURCE = (ROOT / 'scripts/verify-collections.sh').read_text()
BASELINE_COMPARISON = BASELINE_SOURCE.split("<< 'PYEOF'", 1)[1].split('\n', 1)[1].split('\nPYEOF', 1)[0]
NAME = 'plugins/modules/fixture.py'
SAFE = b'print("fixture")\n'


class CollectionChecksumAndSignatureTests(unittest.TestCase):
    def test_checksum_tools_and_native_macos_fallback(self):
        checksum_check = BASELINE_SOURCE.split('# Step 1b:', 1)[1].split('# Step 2:', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'fixture').write_bytes(SAFE)
            manifest = root / 'SHA256SUMS'
            # shasum (perl-Digest-SHA) is native on macOS but absent from a default Fedora install
            available = [name for name in ['sha256sum', 'shasum'] if shutil.which(name)]
            self.assertTrue(available, 'no checksum tool on PATH')
            for tool in available:
                tools = root / tool
                tools.mkdir()
                (tools / tool).symlink_to(shutil.which(tool))
                for correct in [True, False]:
                    with self.subTest(tool=tool, correct=correct):
                        manifest.write_text((hashlib.sha256(SAFE).hexdigest() if correct else '0' * 64) + '  fixture\n')
                        result = subprocess.run(['/bin/bash', '-c', '# checksum check\n' + checksum_check],
                                                cwd=root, env=dict(os.environ, PATH=str(tools)),
                                                capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0 if correct else 1, result.stdout + result.stderr)

    def test_missing_gpg_fails_with_dependency_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
            (root / 'collections-dist').mkdir()
            (root / 'collections-dist/SHA256SUMS.asc').write_text('fixture signature')
            verifier = root / 'scripts/verify-collections.sh'
            verifier.write_text(BASELINE_SOURCE)
            tools = root / 'tools'
            tools.mkdir()
            (tools / 'dirname').symlink_to(shutil.which('dirname'))
            for skip in ['0', '1']:
                with self.subTest(skip=skip):
                    result = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True,
                                            env=dict(os.environ, PATH=str(tools), SKIP_GPG_VERIFY=skip))
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn('gpg is required', result.stderr)
                    self.assertNotIn('signature verification FAILED', result.stderr)

    def test_empty_collections_dir_from_interrupted_install_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
            (root / '.gpg-enforcement').write_text('disabled\n')
            shutil.copytree(ROOT / 'collections-dist', root / 'collections-dist')
            verifier = root / 'scripts/verify-collections.sh'
            verifier.write_text(BASELINE_SOURCE)
            fresh = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True)
            self.assertEqual(fresh.returncode, 0, fresh.stderr)  # fresh clone: no collections/ yet
            (root / 'collections').mkdir()
            interrupted = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True)
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertIn('Re-run: make collections', interrupted.stderr)

    def test_disabled_signatures_still_reject_archive_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
            (root / '.gpg-enforcement').write_text('disabled\n')
            dist = root / 'collections-dist'
            dist.mkdir()
            (dist / 'fixture.tar.gz').write_bytes(SAFE)
            (dist / 'SHA256SUMS').write_text('0' * 64 + '  fixture.tar.gz\n')
            verifier = root / 'scripts/verify-collections.sh'
            verifier.write_text(BASELINE_SOURCE)
            result = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Collection tarball integrity check failed', result.stderr)
            self.assertNotIn('Required checksum signature', result.stderr)

    def test_gpg_only_runs_when_enabled_and_bad_signatures_still_fail(self):
        for policy in ['enabled', 'disabled']:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'scripts').mkdir()
                shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
                (root / '.gpg-enforcement').write_text(policy + '\n')
                dist = root / 'collections-dist'
                dist.mkdir()
                (dist / 'SHA256SUMS.asc').write_text('fixture signature')
                (dist / 'fixture.tar.gz').write_bytes(SAFE)
                (dist / 'SHA256SUMS').write_text('0' * 64 + '  fixture.tar.gz\n')
                verifier = root / 'scripts/verify-collections.sh'
                verifier.write_text(BASELINE_SOURCE)
                tools = root / 'tools'
                tools.mkdir()
                marker = root / 'gpg-called'
                gpg = tools / 'gpg'
                gpg.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\necho "gpg: BAD signature" >&2\nexit 1\n')
                gpg.chmod(0o755)
                result = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True,
                                        env=dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], SKIP_GPG_VERIFY='1'))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(marker.exists(), policy == 'enabled')
                self.assertIn('signature verification FAILED' if policy == 'enabled' else 'Collection tarball integrity', result.stderr)

    def test_invalid_signing_policy_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
            (root / 'collections-dist').mkdir()
            (root / '.gpg-enforcement').write_text('invalid\n')
            verifier = root / 'scripts/verify-collections.sh'
            verifier.write_text(BASELINE_SOURCE)
            result = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('enabled or disabled', result.stderr)

    def test_missing_signature_fails_even_with_missing_key_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
            (root / 'collections-dist').mkdir()
            verifier = root / 'scripts/verify-collections.sh'
            verifier.write_text(BASELINE_SOURCE)
            for skip in ['0', '1']:
                with self.subTest(skip=skip):
                    result = subprocess.run(['bash', str(verifier)], capture_output=True, text=True,
                                            env=dict(os.environ, SKIP_GPG_VERIFY=skip))
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn('Required checksum signature', result.stderr)


class CollectionSourceTests(unittest.TestCase):
    def check(self, contents, accepted, *, symlink=False, duplicate=False, extra=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / 'upstream'
            path = upstream / NAME
            path.parent.mkdir(parents=True)
            path.write_bytes(SAFE)
            archive = root / 'fixture.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo(NAME)
                if symlink:
                    member.type = tarfile.SYMTYPE
                    member.linkname = '/etc/shadow'
                    tar.addfile(member)
                elif contents is not None:
                    member.size = len(contents)
                    tar.addfile(member, io.BytesIO(contents))
                    if duplicate:
                        tar.addfile(member, io.BytesIO(contents))
                for extra_name, extra_type, *link in extra:
                    member = tarfile.TarInfo(extra_name)
                    member.type = extra_type
                    if extra_type == tarfile.SYMTYPE:
                        member.linkname = link[0] if link else '/tmp/elsewhere'
                        tar.addfile(member)
                    else:
                        member.size = len(SAFE)
                        tar.addfile(member, io.BytesIO(SAFE))
            result = subprocess.run([sys.executable, '-I', '-c', COMPARISON, str(archive), str(upstream)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0 if accepted else 1, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)  # rc 1 must be a rejection, not a crash

    def test_identical_source_passes(self):
        self.check(SAFE, True)

    def test_modified_content_with_identical_filename_fails(self):
        self.check(b'print("modified fixture")\n', False)

    def test_no_runtime_files_on_either_side_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'upstream').mkdir()
            (root / 'upstream' / 'README.md').write_text('x')
            archive = root / 'fixture.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo('README.md')
                member.size = 1
                tar.addfile(member, io.BytesIO(b'x'))
            result = subprocess.run([sys.executable, '-I', '-c', COMPARISON, str(archive), str(root / 'upstream')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('No runtime files', result.stderr)

    def test_missing_source_fails(self):
        self.check(None, False)

    def test_python_symlink_fails(self):
        self.check(None, False, symlink=True)

    def test_duplicate_python_members_fail(self):
        self.check(SAFE, False, duplicate=True)

    def test_dot_slash_extra_member_fails(self):
        self.check(SAFE, False, extra=[('./plugins/modules/evil.py', tarfile.REGTYPE)])

    def test_path_tricks_fail(self):
        for name in ['docs/../plugins/modules/evil.py', 'plugins/./modules/evil.py',
                     '/plugins/modules/evil.py', 'meta//runtime.yml', 'meta/./runtime.yml',
                     'Plugins/modules/evil.py', 'META/runtime.yml']:
            with self.subTest(name=name):
                self.check(SAFE, False, extra=[(name, tarfile.REGTYPE)])

    def test_symlink_into_runtime_tree_fails(self):
        self.check(SAFE, False, extra=[('docs', tarfile.SYMTYPE, 'plugins')])
        self.check(SAFE, False, extra=[('docs/link', tarfile.SYMTYPE, '../plugins/modules')])

    def test_members_below_a_link_fail(self):
        self.check(SAFE, False, extra=[('x', tarfile.SYMTYPE, '.'), ('x/plugins/modules/evil.py', tarfile.REGTYPE)])
        self.check(SAFE, False, extra=[('d', tarfile.SYMTYPE, 'docs/..'), ('d/roles/evil/tasks/main.yml', tarfile.REGTYPE)])
        self.check(SAFE, False, extra=[('x', tarfile.SYMTYPE, '.')])

    def test_link_chains_and_duplicate_names_fail(self):
        chain = [('tests/Q', tarfile.SYMTYPE, '../tests'), ('tests/P', tarfile.SYMTYPE, 'Q/..')]
        self.check(SAFE, False, extra=chain)
        self.check(SAFE, False, extra=[('tests/Q', tarfile.SYMTYPE, '../tests'),
                                       ('h', tarfile.SYMTYPE, 'tests/Q/plugins/modules/fixture.py')])
        self.check(SAFE, False, extra=[('h', tarfile.SYMTYPE, 'elsewhere'), ('h', tarfile.REGTYPE)])
        self.check(SAFE, False, extra=[('tests/Q', tarfile.SYMTYPE, 'sibling'), ('Tests/Q/x', tarfile.REGTYPE)])

    def test_symlink_inside_other_tree_passes(self):
        self.check(SAFE, True, extra=[('tests/link', tarfile.SYMTYPE, 'target')])
        self.check(SAFE, True, extra=[('tests/link', tarfile.SYMTYPE, '..hidden')])

    def test_runtime_directory_symlink_fails(self):
        self.check(SAFE, False, extra=[('plugins', tarfile.SYMTYPE, 'docs')])


class CommittedBaselineTests(unittest.TestCase):
    def check(self, *, coordinated_change=False, native_addition=False, missing_baseline=False, disk_only=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / 'installed'
            fresh = root / 'fresh'
            for base in [installed, fresh]:
                collection = base / 'fixture/collection'
                path = collection / NAME
                path.parent.mkdir(parents=True)
                path.write_bytes(b'changed bytes' if coordinated_change else SAFE)
                if native_addition:
                    (path.parent / 'added.so').write_bytes(b'new native code')
                (collection / 'MANIFEST.json').write_text(json.dumps({'collection_info': {'version': '1.0.0'}}))
            if disk_only:  # file injected into the installed tree after extraction
                (installed / 'fixture/collection' / NAME).with_name('evil.py').write_bytes(b'import os\n')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({} if missing_baseline else {
                'fixture-collection-1.0.0.tar': {'runtime_files': {NAME: hashlib.sha256(SAFE).hexdigest()}}
            }))
            result = subprocess.run([sys.executable, '-I', '-c', BASELINE_COMPARISON,
                                     str(installed), str(fresh), str(manifest)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1 if any([coordinated_change, native_addition, missing_baseline, disk_only]) else 0,
                             result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_unchanged_baseline_passes(self):
        self.check()

    def test_coordinated_archive_and_install_changes_fail(self):
        self.check(coordinated_change=True)

    def test_coordinated_native_code_addition_fails(self):
        self.check(native_addition=True)

    def test_missing_committed_baseline_fails(self):
        self.check(missing_baseline=True)

    def test_installed_only_python_file_fails(self):
        self.check(disk_only=True)


class FullTreeTests(unittest.TestCase):
    """Every installed entry must be in its verified tarball, whatever its type or extension."""

    def tree(self, base, runtime=True):
        collection = base / 'fixture/collection'
        (collection / 'plugins/modules').mkdir(parents=True)
        (collection / NAME).write_bytes(SAFE)
        (collection / 'meta').mkdir()
        if runtime:
            (collection / 'meta/runtime.yml').write_text('requires_ansible: ">=2.16"\n')
        (collection / 'tests').mkdir()
        (collection / 'tests/target.sh').write_text('echo test\n')
        (collection / 'tests/alias.sh').symlink_to('target.sh')
        (collection / 'MANIFEST.json').write_text(json.dumps({'collection_info': {'version': '1.0.0'}}))
        return collection

    def run_check(self, plant=None, runtime=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.tree(root / 'fresh', runtime)
            installed = self.tree(root / 'installed', runtime)
            if plant:
                plant(installed)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({
                'fixture-collection-1.0.0.tar': {'runtime_files': {NAME: hashlib.sha256(SAFE).hexdigest()}}}))
            result = subprocess.run([sys.executable, '-I', '-c', BASELINE_COMPARISON,
                                     str(root / 'installed'), str(root / 'fresh'), str(manifest)],
                                    capture_output=True, text=True)
            self.assertNotIn('Traceback', result.stderr)
            return result, installed

    def assert_rejected(self, plant, path, runtime=True):
        result, _ = self.run_check(plant, runtime)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(path, result.stderr)

    def test_identical_tree_with_tarball_symlinks_passes(self):
        result, _ = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_planted_extensionless_module_fails(self):
        def plant(collection):
            module = collection / 'plugins/modules/fixture'
            module.write_text('#!/bin/sh\necho pwned\n')
            module.chmod(0o755)
        self.assert_rejected(plant, 'plugins/modules/fixture')

    def test_planted_runtime_redirect_fails(self):
        """A collection that ships no meta/runtime.yml gets one redirecting its module elsewhere."""
        def plant(collection):
            (collection / 'meta/runtime.yml').write_text(
                'plugin_routing:\n  modules:\n    fixture:\n      redirect: evil.collection.fixture\n')
        self.assert_rejected(plant, 'meta/runtime.yml', runtime=False)

    def test_missing_tarball_file_fails(self):
        self.assert_rejected(lambda collection: (collection / 'meta/runtime.yml').unlink(), 'meta/runtime.yml')

    def test_modified_yml_fails(self):
        def plant(collection):
            (collection / 'meta/runtime.yml').write_text(
                'plugin_routing:\n  modules:\n    fixture:\n      redirect: evil.collection.fixture\n')
        self.assert_rejected(plant, 'meta/runtime.yml')

    def test_extra_symlink_fails(self):
        self.assert_rejected(lambda collection: (collection / 'tests/extra.sh').symlink_to('/bin/sh'), 'tests/extra.sh')

    def test_retargeted_tarball_symlink_fails(self):
        def plant(collection):
            (collection / 'tests/alias.sh').unlink()
            (collection / 'tests/alias.sh').symlink_to('/bin/sh')
        self.assert_rejected(plant, 'tests/alias.sh')

    def test_special_file_fails(self):
        self.assert_rejected(lambda collection: os.mkfifo(collection / 'tests/pipe'), 'tests/pipe')

    def test_file_beside_collections_fails(self):
        self.assert_rejected(lambda collection: (collection.parent / 'evil.py').write_text('import os\n'), 'evil.py')

    def test_bytecode_caches_are_removed_and_other_cache_content_fails(self):
        def plant(collection):
            cache = collection / 'plugins/modules/__pycache__'
            cache.mkdir()
            (cache / 'fixture.cpython-314.pyc').write_bytes(b'bytecode')
        result, installed = self.run_check(plant)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((installed / 'plugins/modules/__pycache__').exists())

        def plant_other(collection):
            cache = collection / 'plugins/modules/__pycache__'
            cache.mkdir()
            (cache / 'fixture.py').write_text('import os\n')
        self.assert_rejected(plant_other, '__pycache__')

    def test_bytecode_temp_files_count_as_cache(self):
        """Python writes name.pyc.<number> and renames it into place; a run caught mid-write leaves one behind."""
        def plant(collection):
            cache = collection / 'plugins/modules/__pycache__'
            cache.mkdir()
            (cache / 'fixture.cpython-314.pyc.140234').write_bytes(b'bytecode')
        result, installed = self.run_check(plant)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((installed / 'plugins/modules/__pycache__').exists())

    @unittest.skipIf(os.geteuid() == 0, 'root removes the cache regardless of directory permissions')
    def test_cache_that_cannot_be_removed_fails_closed(self):
        def plant(collection):
            cache = collection / 'plugins/modules/__pycache__'
            cache.mkdir()
            (cache / 'fixture.cpython-314.pyc').write_bytes(b'bytecode')
            (collection / 'plugins/modules').chmod(0o555)  # the cache directory cannot be removed (TemporaryDirectory
            # restores permissions when it cleans up)
        result, _ = self.run_check(plant)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('__pycache__', result.stderr)

    def test_finder_metadata_is_refused_with_a_delete_hint(self):
        def plant(collection):
            (collection / 'plugins/.DS_Store').write_bytes(b'\x00\x00\x00\x01Bud1')
        result, _ = self.run_check(plant)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('delete the .DS_Store files', result.stderr)

    def test_group_writable_entry_fails(self):
        self.assert_rejected(lambda collection: (collection / NAME).chmod(0o664), NAME)

    def test_tarball_swapped_after_the_hash_check_is_not_extracted(self):
        """tar sees the verified private copy; a swap of collections-dist/ during the check changes nothing."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
            (root / '.gpg-enforcement').write_text('disabled\n')
            shutil.copytree(ROOT / 'collections-dist', root / 'collections-dist')
            (root / 'scripts/verify-collections.sh').write_text(BASELINE_SOURCE)
            shims = root / 'shims'
            shims.mkdir()
            # the first tar call swaps the tarball being extracted (ansible.posix, first in order) for junk
            (shims / 'tar').write_text('#!/bin/sh\n[ -e "$SWAP_DIR/.swapped" ] || '
                                       '{ for t in "$SWAP_DIR"/ansible-posix-*.tar.gz; do printf junk > "$t"; done; '
                                       ': > "$SWAP_DIR/.swapped"; }\n'
                                       f'exec {shutil.which("tar")} "$@"\n')
            (shims / 'tar').chmod(0o755)
            result = subprocess.run(['/bin/bash', str(root / 'scripts/verify-collections.sh')], capture_output=True,
                                    text=True, env=dict(os.environ, PATH=f"{shims}:{os.environ['PATH']}",
                                                        SWAP_DIR=str(root / 'collections-dist')))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_tarballs_reject_a_planted_extensionless_module(self):
        """End to end on the vendored tarballs: install them as ansible-galaxy does, then plant ansible.posix's 'at'."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
            (root / '.gpg-enforcement').write_text('disabled\n')
            shutil.copytree(ROOT / 'collections-dist', root / 'collections-dist')
            verifier = root / 'scripts/verify-collections.sh'
            verifier.write_text(BASELINE_SOURCE)
            for tarball in sorted((root / 'collections-dist').glob('*.tar.gz')):
                namespace, name = tarball.name.split('-')[:2]
                target = root / 'collections/ansible_collections' / namespace / name
                target.mkdir(parents=True)
                with tarfile.open(tarball) as archive:
                    archive.extractall(target, filter='tar')
            clean = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True)
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            module = root / 'collections/ansible_collections/ansible/posix/plugins/modules/at'
            module.write_text('#!/bin/sh\necho pwned\n')
            module.chmod(0o755)
            planted = subprocess.run(['/bin/bash', str(verifier)], capture_output=True, text=True)
            self.assertEqual(planted.returncode, 1, planted.stdout + planted.stderr)
            self.assertIn('plugins/modules/at', planted.stderr)


class RepoPluginBytecodeTests(unittest.TestCase):
    """Bytecode beside the repo's own role plugins is removed before the run: Ansible would load a planted one."""
    FILTER = 'def fixture(value):\n    return value\n\n\nclass FilterModule:\n    def filters(self):\n        return {"fixture": fixture}\n'

    def layout(self, root):
        (root / 'scripts').mkdir()
        shutil.copy2(ROOT / 'scripts/gpg-policy.sh', root / 'scripts/gpg-policy.sh')
        (root / '.gpg-enforcement').write_text('disabled\n')
        shutil.copytree(ROOT / 'collections-dist', root / 'collections-dist')
        (root / 'scripts/verify-collections.sh').write_text(BASELINE_SOURCE)
        plugins = root / 'roles/fixture/filter_plugins'
        plugins.mkdir(parents=True)
        (plugins / 'fixture.py').write_text(self.FILTER)
        (root / 'roles/fixture/tasks').mkdir()
        (root / 'roles/fixture/tasks/main.yml').write_text('- ansible.builtin.debug:\n    msg: "{{ 1 | fixture }}"\n')
        return plugins

    def plant(self, source, marker):
        """Unchecked-hash bytecode (loaded whatever the source says) that also writes a marker."""
        import importlib.util
        import py_compile
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / source.name
            payload.write_text(source.read_text() + f"\nopen({str(marker)!r}, 'w').write('planted')\n")
            cache = Path(importlib.util.cache_from_source(str(source)))
            py_compile.compile(str(payload), cfile=str(cache), dfile=str(source), doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        return cache

    def test_planted_filter_bytecode_is_removed_before_ansible_loads_it(self):
        ansible = ROOT / '.venv/bin/ansible-playbook'
        if not ansible.exists():
            self.skipTest('needs the repo .venv')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugins = self.layout(root)
            marker = root / 'PLANTED'
            cache = self.plant(plugins / 'fixture.py', marker)
            verify = subprocess.run(['/bin/bash', str(root / 'scripts/verify-collections.sh')], capture_output=True, text=True)
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertFalse(cache.parent.exists(), 'the planted __pycache__ is still there')
            (root / 'play.yml').write_text('- hosts: localhost\n  gather_facts: false\n  roles: [fixture]\n')
            run = subprocess.run([str(ansible), '-i', 'localhost,', '-c', 'local', str(root / 'play.yml')], cwd=root,
                                 env=dict(os.environ, ANSIBLE_ROLES_PATH=str(root / 'roles'), PYTHONDONTWRITEBYTECODE='1',
                                          ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertFalse(marker.exists(), 'Ansible loaded the planted bytecode')

    def test_symlinked_or_mixed_cache_beside_plugins_is_refused(self):
        for kind in ('symlink', 'mixed'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plugins = self.layout(root)
                if kind == 'symlink':
                    (plugins / '__pycache__').symlink_to(root)
                else:
                    (plugins / '__pycache__').mkdir()
                    (plugins / '__pycache__/evil.py').write_text('import os\n')
                verify = subprocess.run(['/bin/bash', str(root / 'scripts/verify-collections.sh')], capture_output=True,
                                        text=True)
                self.assertEqual(verify.returncode, 1, verify.stdout + verify.stderr)
                self.assertIn('__pycache__', verify.stderr)

    def test_concurrent_change_gets_its_own_message(self):
        """An entry that is gone by the time it would be reported was changed during the check, not planted."""
        namespace = {}
        exec(BASELINE_COMPARISON.split('def sha256(path):', 1)[0].replace('import sys', 'import sys', 1), namespace)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit), unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
                namespace['fail_entries'](directory, 'tampering', ['vanished.cpython-314.pyc'])
            self.assertIn('changed during the check (concurrent Ansible run?)', stderr.getvalue())
            Path(directory, 'present').write_text('x')
            with self.assertRaises(SystemExit), unittest.mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
                namespace['fail_entries'](directory, 'tampering', ['present'])
            self.assertIn('tampering', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
