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
        self.check((ROOT / 'group_vars/all/vault.yml').read_text(), True)
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
        self.check('vault_placeholder: secret\nvault_key: []\n', False)
        self.check('', False)


if __name__ == '__main__':
    unittest.main()
