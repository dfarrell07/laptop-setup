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

ROOT = Path(__file__).resolve().parent.parent
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


if __name__ == '__main__':
    unittest.main()
