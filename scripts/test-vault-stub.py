#!/usr/bin/env python3
"""Verify the actual vault template and reject plaintext secrets or YAML tricks."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / 'scripts/verify-vault-stub.py'


class VaultStubTests(unittest.TestCase):
    def check(self, content, accepted):
        result = subprocess.run(['python3', str(VERIFY)], input=content, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_committed_template_and_empty_types(self):
        committed = (ROOT / 'group_vars/all/vault.yml').read_text()
        # After the documented ansible-vault encrypt the file is ciphertext, not the stub
        if not committed.startswith('$ANSIBLE_VAULT'):
            self.check(committed, True)
        for value in ('[]', "''", '""'):
            self.check('---\nvault_placeholder: "encrypt-me"\nvault_key: ' + value + ' # empty\n', True)

    def test_nonempty_and_disguised_values(self):
        marker = 'vault_placeholder: "encrypt-me"\n'
        for extra in ('vault_key: secret\n', 'vault_key: [] + secret\n',
                      'vault_key: []\n  - secret\n', 'vault_key: |\n  secret\n',
                      'other_key: secret\n', ' vault_key: secret\n',
                      'vault_key: !!str []\n', 'vault_key: &value secret\n', 'vault_key: ""#secret\n',
                      'vault_key: []\nvault_key: ""\n', '---\nvault_key: []\n'):
            with self.subTest(extra=extra):
                self.check(marker + extra, False)
        self.check('# vault_placeholder\nvault_key: []\n', False)
        for comment in ('# -----BEGIN OPENSSH PRIVATE KEY-----', '#   b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW', '# PRIVATE KEY'):
            with self.subTest(comment=comment):
                self.check(marker + comment + '\nvault_key: []\n', False)
        for tail in ('# -----BEGIN OPENSSH PRIVATE KEY-----', '# b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAA'):
            self.check(marker + 'vault_key: [] ' + tail + '\n', False)
            self.check('vault_placeholder: "encrypt-me" ' + tail + '\nvault_key: []\n', False)
        self.check(marker + '# Populate these, then run ansible-vault encrypt\nvault_key: []\n', True)
        self.check('vault_placeholder: secret\nvault_key: []\n', False)
        self.check('', False)
        self.check(marker + '# PRIVATE\0 KEY\nvault_key: []\n', False)
        self.check(marker + ''.join('vault_k%d: []\n' % i for i in range(40000)), False)
        self.check(marker + '  \u00a0# hunter2 correct horse battery staple\n', False)
        self.check(marker + 'vault_' + 'a' * 60 + ': []\n', False)
        self.check(marker + 'vault_' + 'a' * 56 + ': []\n', True)
        self.check(marker + '# private_key backup\nvault_key: []\n', False)
        self.check(marker + 'vault_x: x' + ' ' * 5000 + 'y\n', False)


if __name__ == '__main__':
    unittest.main()
