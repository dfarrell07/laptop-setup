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


# Top-level paths that cannot change what the molecule scenarios converge, so a PR touching only them may skip molecule
MOLECULE_SKIPPABLE = {'.ansible-lint', '.claude', '.githooks', '.github', '.gitignore', '.gitleaks.toml', '.gpg-enforcement',
                      '.markdownlint.yml', '.npmrc', '.yamllint', 'AGENTS.md', 'CLAUDE.md', 'LICENSE', 'Makefile.sha256',
                      'README.md', 'SECURITY.md', 'commitlint.config.js', 'docs', 'package-lock.json', 'package.json',
                      'chrome-bookmarks', 'local_bookmarks.yml.example', 'references', 'test-var-override.sh'}


class WorkflowGuardTests(unittest.TestCase):
    def test_suites_that_run_ansible_pin_the_ci_vault_password(self):
        """ansible.cfg points at scripts/vault-pass.sh, which make setup-yubikeys turns into the YubiKey script: a suite
        that runs ansible-playbook without ANSIBLE_VAULT_PASSWORD_FILE would call it from make gate (a touch, a hang). This is a
        file-level check; run-tests.py also pins it for every suite, which covers a missed call in a pinned file."""
        unpinned = [path.name for path in sorted((ROOT / 'scripts').glob('test-*.py'))
                    if 'ansible-playbook' in (text := path.read_text(encoding='utf-8'))
                    and 'vault-pass-ci.sh' not in text]
        self.assertEqual(unpinned, [], 'set ANSIBLE_VAULT_PASSWORD_FILE to scripts/vault-pass-ci.sh for every ansible run')
        # The backstop for make test-scripts: a missed call in a file that pins others still gets the CI script there
        runner = (ROOT / 'scripts/run-tests.py').read_text(encoding='utf-8')
        self.assertRegex(runner, r"os\.environ\['ANSIBLE_VAULT_PASSWORD_FILE'\] = .*\n.*'vault-pass-ci\.sh'")

    def test_every_top_level_path_runs_molecule_or_is_listed_as_skippable(self):
        steps = yaml.safe_load((ROOT / '.github/workflows/molecule.yml').read_text())['jobs']['changes']['steps']
        script = next(s['run'] for s in steps if s.get('id') == 'filter')
        assignments = '\n'.join(l for l in script.splitlines() if l.strip().startswith('ANSIBLE_PATHS'))
        tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        tops = {}
        for path in tracked.splitlines():
            top, _, rest = path.partition('/')
            tops.setdefault(top, path if rest else top)  # a directory is probed through one of its files
        for top, probe in sorted(tops.items()):
            with self.subTest(path=top):
                runs = subprocess.run(['bash', '-c', assignments + '\ngrep -Eq "$ANSIBLE_PATHS" <<< "$1"', '_', probe]).returncode == 0
                self.assertTrue(runs != (top in MOLECULE_SKIPPABLE),
                                f'{top}: ' + ('listed as skippable but runs molecule' if runs else
                                             'skips molecule: add it to ANSIBLE_PATHS or MOLECULE_SKIPPABLE'))

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
