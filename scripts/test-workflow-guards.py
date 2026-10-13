#!/usr/bin/env python3
"""Exercise CI hook-write detection and Go checksum defaults with real tools."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
JOBS = yaml.safe_load((ROOT / '.github/workflows/linting.yml').read_text())['jobs']


class WorkflowGuardTests(unittest.TestCase):
    def test_hook_reads_and_documentation_are_allowed_but_writes_fail(self):
        script = JOBS['git-config-guards']['steps'][2]['run']
        cases = [('# git config --local core.hooksPath /dev/null\n', True),
                 ('value=$(git config --local --get core.hooksPath || true)\n', True),
                 ('git config --local core.hooksPath /dev/null\n', False),
                 ('git config core.hooksPath /dev/null\n', False),
                 ('git config --local --unset core.hooksPath\n', False),
                 ('echo okay; git config core.hooksPath /dev/null\n', False)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.githooks').mkdir()
            for content, accepted in cases:
                with self.subTest(content=content):
                    (root / '.githooks/test').write_text(content)
                    result = subprocess.run(['bash', '-eu', '-c', script], cwd=root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)

    @unittest.skipUnless(shutil.which('go'), 'Go is needed to verify its effective settings')
    def test_go_default_passes_and_disabled_verification_fails(self):
        script = JOBS['gpg-signatures']['steps'][2]['run']
        with tempfile.TemporaryDirectory() as directory:
            for value, accepted in [(None, True), ('off', False)]:
                with self.subTest(value=value):
                    env = dict(os.environ, GOENV='off')
                    env.pop('GOSUMDB', None)
                    if value is not None:
                        env['GOSUMDB'] = value
                    result = subprocess.run(['bash', '-eu', '-c', script], cwd=directory, env=env,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)


if __name__ == '__main__':
    unittest.main()
