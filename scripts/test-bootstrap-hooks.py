#!/usr/bin/env python3
"""Reject bad archives before installation and verify atomic executable repair."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('bootstrap_hooks', ROOT / 'scripts/bootstrap-hooks.py')
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)


class BootstrapTests(unittest.TestCase):
    def test_integrity_type_and_safe_installation(self):
        def archive(kind=tarfile.REGTYPE):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w:gz') as output:
                entry = tarfile.TarInfo('gitleaks')
                entry.type = kind
                entry.linkname = '/public-outside-fixture'
                content = b'public binary fixture'
                entry.size = len(content) if kind == tarfile.REGTYPE else 0
                output.addfile(entry, io.BytesIO(content) if entry.size else None)
            return data.getvalue()
        data = archive()
        expected = hashlib.sha256(data).hexdigest()
        binary = BOOTSTRAP.verified_binary(data, expected)
        with self.assertRaises(ValueError):
            BOOTSTRAP.verified_binary(data + b'corruption', expected)
        data = archive(tarfile.SYMTYPE)
        with self.assertRaises(ValueError):
            BOOTSTRAP.verified_binary(data, hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            target = BOOTSTRAP.install(binary, location)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            before = target.stat().st_ctime_ns
            BOOTSTRAP.install(binary, location)
            self.assertEqual(target.stat().st_ctime_ns, before)
            outside = location / 'outside'
            outside.write_bytes(b'preserve')
            target.unlink()
            target.symlink_to(outside)
            BOOTSTRAP.install(binary, location)
            self.assertFalse(target.is_symlink())
            self.assertEqual(outside.read_bytes(), b'preserve')
            self.assertEqual(target.read_bytes(), binary)

    def test_linux_pins_match_production(self):
        import yaml
        defaults = yaml.safe_load((ROOT / 'roles/packages/defaults/main.yml').read_text())
        self.assertEqual(BOOTSTRAP.VERSION, defaults['packages_gitleaks_version'])
        self.assertEqual(BOOTSTRAP.RELEASES[('Linux', 'x86_64')][1], defaults['packages_gitleaks_sha256'])
        self.assertEqual(BOOTSTRAP.RELEASES[('Linux', 'aarch64')][1], defaults['packages_gitleaks_sha256_arm64'])


if __name__ == '__main__':
    unittest.main()
