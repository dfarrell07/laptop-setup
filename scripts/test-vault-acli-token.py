#!/usr/bin/env python3
"""scripts/vault-set-acli-token.py (make acli-token): a throwaway vault, the token on stdin, a fake vault-pass.sh."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VAULT_BIN = ROOT / '.venv/bin/ansible-vault'
TOKEN = 'ATATT3xFfGF0test-token_value=ABC123'


@unittest.skipUnless(VAULT_BIN.exists(), 'no dev venv (make bootstrap)')
class VaultAcliToken(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / 'scripts').mkdir()
        for name in ('vault-set-acli-token.py', 'vault-set-signing-key.py'):
            shutil.copy(ROOT / 'scripts' / name, self.dir / 'scripts' / name)
        (self.dir / 'scripts/vault-pass.sh').write_text('#!/bin/sh\necho test-password\n')
        (self.dir / 'scripts/vault-pass.sh').chmod(0o700)
        self.vault = self.dir / 'vault.yml'
        self.vault.write_text("vault_ssh_auth_key: 'keep-me'\nvault_acli_api_token: ''\nvault_codex_api_key: ''\n")
        self.env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(self.dir / 'scripts/vault-pass.sh'))
        self.encrypt()

    def encrypt(self):
        subprocess.run([str(VAULT_BIN), 'encrypt', str(self.vault)], env=self.env, check=True, capture_output=True)

    def run_script(self, token):
        return subprocess.run([sys.executable, '-I', str(self.dir / 'scripts/vault-set-acli-token.py'), str(self.vault)],
                              input=token + '\n', cwd=self.dir, env=self.env, capture_output=True, text=True,
                              timeout=60)

    def decrypted(self):
        out = subprocess.run([str(VAULT_BIN), 'view', str(self.vault)], env=self.env, check=True, capture_output=True,
                             text=True)
        return yaml.safe_load(out.stdout)

    def test_sets_the_token_and_keeps_the_rest(self):
        result = self.run_script(TOKEN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)  # never printed
        values = self.decrypted()
        self.assertEqual(values['vault_acli_api_token'], TOKEN)
        self.assertEqual(values['vault_ssh_auth_key'], 'keep-me')
        self.assertEqual(values['vault_codex_api_key'], '')
        self.assertTrue(self.vault.read_text().startswith('$ANSIBLE_VAULT'))
        self.assertEqual(self.vault.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_script(TOKEN + 'X').returncode, 0)  # a new token replaces the old one
        self.assertEqual(self.decrypted()['vault_acli_api_token'], TOKEN + 'X')

    def test_a_missing_entry_is_added(self):
        self.vault.write_text("vault_ssh_auth_key: 'keep-me'\n")
        self.encrypt()
        self.assertEqual(self.run_script(TOKEN).returncode, 0)
        self.assertEqual(self.decrypted(), {'vault_ssh_auth_key': 'keep-me', 'vault_acli_api_token': TOKEN})

    def test_the_same_token_again_changes_nothing(self):
        self.assertEqual(self.run_script(TOKEN).returncode, 0)
        before = self.vault.read_bytes()
        again = self.run_script(TOKEN)
        self.assertEqual(again.returncode, 3)
        self.assertIn('nothing changed', again.stderr)
        self.assertEqual(self.vault.read_bytes(), before)

    def test_refuses_what_is_not_a_token(self):
        before = self.vault.read_bytes()
        for bad in ['', 'short', "{{ lookup('pipe', 'id') }}aaaaaaaa", 'has spaces in the token value', "quo'te-aaaaaaaaaaaa"]:
            result = self.run_script(bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn('nothing changed', result.stderr, bad)
            self.assertNotIn('Traceback', result.stderr, bad)
            if bad:
                self.assertNotIn(bad, result.stdout + result.stderr)
        self.assertEqual(self.vault.read_bytes(), before)


    def test_a_wrong_password_or_a_plaintext_vault_is_a_clear_error(self):
        before = self.vault.read_bytes()
        (self.dir / 'scripts/vault-pass.sh').write_text('#!/bin/sh\necho another-password\n')
        result = self.run_script(TOKEN)
        self.assertIn('could not be decrypted', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.vault.read_bytes(), before)
        self.vault.write_text("vault_acli_api_token: ''\n")
        result = self.run_script(TOKEN)  # refused before any prompt
        self.assertIn('is not an encrypted vault', result.stderr)
        self.assertEqual(self.vault.read_text(), "vault_acli_api_token: ''\n")


if __name__ == '__main__':
    unittest.main()
