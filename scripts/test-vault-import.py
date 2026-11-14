#!/usr/bin/env python3
"""scripts/vault-import.sh refuses what is not an encrypted vault before changing anything.

These cases stop before vault-pass.sh is written, so they run against the real checkout; the key test itself
(a YubiKey opening the vault) needs hardware and is exercised by hand.
"""
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts/vault-import.sh'
WATCHED = [ROOT / 'scripts/vault-pass.sh', ROOT / 'scripts/vault-pass.sh.sha256', ROOT / 'group_vars/all/vault.yml']
VAULT_HEAD = '$ANSIBLE_VAULT;1.1;AES256\n6162\n'


def digest():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in WATCHED}


class VaultImportRefusals(unittest.TestCase):
    def run_import(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, NO_COLOR='1'))

    def setUp(self):
        local = (ROOT / 'group_vars/all/vault.yml').read_text()
        if local.startswith('$ANSIBLE_VAULT'):
            self.skipTest('this checkout already has an encrypted vault: vault-import refuses before any case')
        self.before = digest()

    def tearDown(self):
        self.assertEqual(digest(), self.before, 'vault-import changed the helper, its baseline or the vault')

    def test_needs_a_source(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 1)
        self.assertIn('usage: make vault-import', result.stderr)

    def test_rejects_an_option_like_source(self):
        self.assertIn("must not start with '-'", self.run_import('-oProxyCommand=x').stderr)

    def test_missing_path(self):
        result = self.run_import('/nonexistent/vault.yml')
        self.assertEqual(result.returncode, 1)
        self.assertIn('no such file or directory', result.stderr)

    def test_plaintext_is_not_imported(self):
        with tempfile.NamedTemporaryFile('w', suffix='.yml') as plain:
            plain.write('vault_placeholder: x\n')
            plain.flush()
            result = self.run_import(plain.name)
        self.assertEqual(result.returncode, 1)
        self.assertIn('is not an encrypted Ansible vault', result.stderr)

    def test_a_directory_must_hold_exactly_one_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn('found 0', self.run_import(tmp).stderr)
            for repo in ('a', 'b'):
                vault = Path(tmp, repo, 'group_vars/all/vault.yml')
                vault.parent.mkdir(parents=True)
                vault.write_text(VAULT_HEAD)
            self.assertIn('found 2', self.run_import(tmp).stderr)

    def test_no_temp_file_left_behind(self):
        self.run_import('/nonexistent/vault.yml')
        self.assertEqual(list((ROOT / 'group_vars/all').glob('vault.yml.import.*')), [])


if __name__ == '__main__':
    unittest.main()
