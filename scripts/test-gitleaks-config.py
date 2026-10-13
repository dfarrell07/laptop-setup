#!/usr/bin/env python3
"""Verify digest exceptions do not disable credential scanning for whole files."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class GitleaksConfigTests(unittest.TestCase):
    def scan(self, filename, content, accepted):
        binary = shutil.which('gitleaks')
        self.assertIsNotNone(binary, 'Install the pinned gitleaks binary before running this test')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'data' / filename
            target.parent.mkdir(parents=True)
            target.write_text(content)
            result = subprocess.run(
                [binary, 'dir', str(root / 'data'), '--config', str(ROOT / '.gitleaks.toml'), '--redact'],
                cwd=root, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)

    def test_digest_rows_are_allowed_only_in_the_generated_manifest(self):
        manifest = json.loads((ROOT / 'collections-dist/PYTHON_MANIFEST.json').read_text())
        files = next(value['runtime_files'] for value in manifest.values()
                     if 'plugins/modules/discord.py' in value['runtime_files'])
        for name in ['plugins/modules/discord.py', 'plugins/modules/newrelic_deployment.py',
                     'plugins/modules/bitbucket_access_key.py', 'plugins/callback/sumologic.py',
                     'plugins/doc_fragments/_auth_basic.py']:
            row = f'      "{name}": "{files[name]}",\n'
            with self.subTest(name=name):
                self.scan('collections-dist/PYTHON_MANIFEST.json', row, True)
                self.scan('other.json', row, False)

    def test_credentials_remain_detectable_in_manifest(self):
        credential = hashlib.sha256(b"non-secret credential scanner fixture").hexdigest()
        assignment = 'api_key = "' + credential + '"\n'
        self.scan('collections-dist/PYTHON_MANIFEST.json', assignment, False)
        row = '      "plugins/modules/example.py": "' + '1a' * 32 + '", '
        self.scan('collections-dist/PYTHON_MANIFEST.json', row + assignment, False)

    def test_dummy_vault_password_does_not_exempt_its_entire_script(self):
        stub = (ROOT / 'scripts/vault-pass-ci.sh').read_text()
        self.scan('scripts/vault-pass-ci.sh', stub, True)
        credential = hashlib.sha256(b"non-secret vault scanner fixture").hexdigest()
        self.scan('scripts/vault-pass-ci.sh', stub + '\napi_key = "' + credential + '"\n', False)


if __name__ == '__main__':
    unittest.main()
