#!/usr/bin/env python3
"""scripts/vault-set-signing-key.py (make vault-signing-key): a throwaway vault, fake key files, a fake vault-pass.sh."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts/vault-set-signing-key.py'
VAULT_BIN = ROOT / '.venv/bin/ansible-vault'
PRIVATE = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nZmFrZQ==\n-----END OPENSSH PRIVATE KEY-----\n'
PUBLIC = 'sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t signing\n'


@unittest.skipUnless(VAULT_BIN.exists(), 'no dev venv (make bootstrap)')
class VaultSigningKey(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / 'scripts').mkdir()
        shutil.copy(SCRIPT, self.dir / 'scripts/vault-set-signing-key.py')
        (self.dir / 'scripts/vault-pass.sh').write_text('#!/bin/sh\necho test-password\n')
        (self.dir / 'scripts/vault-pass.sh').chmod(0o700)
        self.vault = self.dir / 'vault.yml'
        # The mistake this target replaces: paths pasted where the key contents belong.
        self.vault.write_text("vault_ssh_auth_key: 'keep-me'\n"
                              "vault_ssh_signing_key: /home/me/.ssh/id_ed25519_sk_signing\n"
                              "vault_ssh_signing_key_pub: /home/me/.ssh/id_ed25519_sk_signing.pub\n"
                              "vault_codex_api_key: ''\n")
        self.env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(self.dir / 'scripts/vault-pass.sh'))
        subprocess.run([str(VAULT_BIN), 'encrypt', str(self.vault)], env=self.env, check=True, capture_output=True)
        self.key = self.dir / 'id_ed25519_sk_signing'
        self.key.write_text(PRIVATE)
        Path(f'{self.key}.pub').write_text(PUBLIC)

    def run_script(self, key=None):
        return subprocess.run([sys.executable, str(self.dir / 'scripts/vault-set-signing-key.py'), str(key or self.key),
                               str(self.vault)], cwd=self.dir, env=self.env, capture_output=True, text=True, timeout=60)

    def decrypted(self):
        out = subprocess.run([str(VAULT_BIN), 'view', str(self.vault)], env=self.env, check=True, capture_output=True,
                             text=True)
        return yaml.safe_load(out.stdout)

    def test_sets_both_entries_and_keeps_the_rest(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)  # nothing secret printed
        self.assertNotIn('AAAAGnNr', result.stdout + result.stderr)
        values = self.decrypted()
        self.assertEqual(values['vault_ssh_signing_key'], PRIVATE)
        self.assertEqual(values['vault_ssh_signing_key_pub'], PUBLIC.strip())
        self.assertEqual(values['vault_ssh_auth_key'], 'keep-me')
        self.assertEqual(values['vault_codex_api_key'], '')
        self.assertTrue(self.vault.read_text().startswith('$ANSIBLE_VAULT'))
        self.assertEqual(self.vault.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_script().returncode, 0)  # a second run replaces the block it wrote
        self.assertEqual(self.decrypted()['vault_ssh_signing_key'], PRIVATE)

    def test_refuses_files_that_are_not_keys(self):
        before = self.vault.read_bytes()
        Path(f'{self.key}.pub').write_text('/home/me/.ssh/id_ed25519_sk_signing.pub\n')
        self.assertIn('not a single public key line', self.run_script().stderr)
        Path(f'{self.key}.pub').write_text(PUBLIC)
        self.key.write_text('/home/me/.ssh/id_ed25519_sk_signing\n')
        self.assertIn('not a private key file', self.run_script().stderr)
        self.assertIn('is not a regular file', self.run_script(self.dir / 'missing').stderr)
        self.assertEqual(self.vault.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
