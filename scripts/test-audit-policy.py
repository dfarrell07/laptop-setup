#!/usr/bin/env python3
"""Test real filesystem preparation and immutable audit policy preservation."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('audit_policy', ROOT / 'roles/system/files/audit-policy.py')
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class AuditPolicyTests(unittest.TestCase):
    def test_missing_noise_exclusions_and_immutable_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'optional-tool'
            source = f'-a never,exit -F exe={executable} -k optional\n-w {directory}/future -p wa -k sensitive\n-e 2\n'
            compiled, receipt = POLICY.select_policy(source, '', None)
            self.assertNotIn('-a never', compiled)
            self.assertIn('-w ', compiled)
            self.assertTrue(compiled.endswith('-e 2\n'))
            executable.write_bytes(b'fixture')
            repeated, same_receipt = POLICY.select_policy(source, compiled, receipt)
            self.assertEqual(repeated, compiled)
            self.assertEqual(same_receipt, receipt)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                POLICY.select_policy(source, compiled.replace('-e 2', '-e 0'), receipt)
            changed, _ = POLICY.select_policy(source.replace('-p wa', '-p rwa'), compiled, receipt)
            self.assertIn('-a never', changed)

    def test_mutable_policy_recompiled_when_omitted_executable_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'optional-tool'
            source = f'-a never,exit -F exe={executable} -k optional\n-e 1\n'
            compiled, receipt = POLICY.select_policy(source, '', None)
            self.assertNotIn('-a never', compiled)
            again, same = POLICY.select_policy(source, compiled, receipt)
            self.assertEqual((again, same), (compiled, receipt))
            executable.write_bytes(b'fixture')
            recompiled, new_receipt = POLICY.select_policy(source, compiled, receipt)
            self.assertIn('-a never', recompiled)
            self.assertEqual(new_receipt['omitted_executables'], [])

    def test_private_directories_without_empty_credential_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = f'-w {home}/.aws/ -p r -k sensitive\n-w {home}/.kube/config -p wa -k sensitive\n'
            self.assertEqual(POLICY.prepare_watch_directories(source, home, os.getuid(), os.getgid(), False), [])
            self.assertFalse((home / '.aws').exists())
            self.assertTrue(POLICY.prepare_watch_directories(source, home, os.getuid(), os.getgid()))
            self.assertEqual((home / '.aws').stat().st_mode & 0o777, 0o700)
            self.assertEqual((home / '.kube').stat().st_uid, os.getuid())
            self.assertFalse((home / '.kube/config').exists())
            self.assertEqual(POLICY.prepare_watch_directories(source, home, os.getuid(), os.getgid()), [])

    def test_external_symlink_cannot_redirect_privileged_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            outside = root / 'outside'
            home.mkdir()
            outside.mkdir()
            (home / '.config').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'escapes home'):
                POLICY.prepare_watch_directories(f'-w {home}/.config/tool/ -p wa -k test\n', home, os.getuid(), os.getgid())
            self.assertFalse((outside / 'tool').exists())


if __name__ == '__main__':
    unittest.main()
