#!/usr/bin/env python3
"""Exercise the real commit hook and commitlint with isolated Git configuration."""
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class CommitMessageTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'node_modules/.bin/commitlint').exists(),
                         'commitlint is installed by npm ci --ignore-scripts')
    def test_scope_parser(self):
        for header, accepted in [('Desktop: repair terminal setup', True),
                                 ('Molecule/Desktop: repair terminal setup', True),
                                 ('Repos-dnf: repair repository setup', True),
                                 ('WIP', False), ('desktop: repair terminal setup', False),
                                 ('Desktop: ', False), ('Desktop: repair setup.', False)]:
            with self.subTest(header=header):
                result = subprocess.run([str(ROOT / 'node_modules/.bin/commitlint')], input=header + '\n',
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stdout + result.stderr)

    def test_effective_signing_configuration_and_empty_private_module_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/gpg-policy.sh', repo / 'scripts/gpg-policy.sh')
            (repo / 'scripts/verify-collections.sh').write_text('fixture')
            (repo / 'config.yml').write_text('dotfiles_gonosumdb: ""\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            message = repo / 'message'
            message.write_text('Security: repair verifier\n\nSigned-off-by: Test <test@ci.invalid>\n')
            global_config = repo / 'global'
            global_config.write_text('[commit]\n gpgsign = true\n')
            env = dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config), GIT_CONFIG_NOSYSTEM='1')
            for name in list(env):
                if name.startswith('GIT_CONFIG_') and name not in {'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM'}:
                    del env[name]
            subprocess.run(['git', '-C', str(repo), 'config', 'commit.gpgsign', 'false'], check=True)
            def check(signing, accepted):
                test_env = dict(env)
                if signing:
                    test_env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='commit.gpgsign', GIT_CONFIG_VALUE_0='true')
                result = subprocess.run(['/bin/bash', str(ROOT / '.githooks/commit-msg'), str(message)],
                                        cwd=repo, env=test_env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stdout + result.stderr)
                if not accepted:
                    self.assertIn('GPG signing not configured', result.stderr)
            check(False, False)  # Local false must override global true.
            check(True, True)  # Command configuration is the effective Git setting.
            policy = repo / '.gpg-enforcement'
            policy.write_text('disabled\n')
            check(False, False)  # An unstaged policy cannot weaken commit enforcement.
            subprocess.run(['git', '-C', str(repo), 'add', '.gpg-enforcement'], check=True)
            check(False, True)
            policy.write_text('enabled\n')
            check(False, True)  # Commit checks follow the index, not the working tree.
            message.write_text('Security: repair verifier\n')
            result = subprocess.run(['/bin/bash', str(ROOT / '.githooks/commit-msg'), str(message)],
                                    cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Missing Signed-off-by', result.stderr)


if __name__ == '__main__':
    unittest.main()
