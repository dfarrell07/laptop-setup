#!/usr/bin/env python3
"""Exercise the production plugin tasks with old pins and a transient clone failure."""
import copy
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
GIT = shutil.which('git')


class PinnedGitTests(unittest.TestCase):
    def test_shallow_old_pins_retry_and_repeat_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'upstream'
            repo.mkdir()
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'))

            def git(*args, cwd=repo):
                return subprocess.check_output([GIT, *args], cwd=cwd, env=env, text=True).strip()

            git('init', '-q')
            git('config', 'user.name', 'Fixture Author')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'plugin').write_text('fixture\n')
            git('add', 'plugin')
            tree = git('write-tree')
            parent = None
            for index in range(110):
                args = ['commit-tree', tree, '-m', f'Fixture {index}']
                if parent:
                    args += ['-p', parent]
                parent = git(*args)
                if index == 0:
                    pinned = parent
            git('update-ref', 'HEAD', parent)
            marker = root / 'failed-once'
            calls = root / 'clone-calls'
            executable = root / 'git-wrapper'
            executable.write_text('#!/bin/bash\nif [[ "$1" == clone ]]; then\n'
                '  printf "clone\\n" >> ' + shlex.quote(str(calls)) + '\n'
                '  if [[ ! -f ' + shlex.quote(str(marker)) + ' ]]; then\n'
                '    touch ' + shlex.quote(str(marker)) + '\n'
                '    echo "simulated transient transport failure" >&2; exit 128\n  fi\nfi\n'
                'exec ' + shlex.quote(GIT) + ' "$@"\n')
            executable.chmod(0o755)
            production = yaml.safe_load((ROOT / 'roles/dotfiles/tasks/main.yml').read_text())
            ohmyzsh = next(task for task in production if task.get('name') == 'Install oh-my-zsh')
            block = next(task['block'] for task in production if task.get('name') == 'Forgit and legacy oh-my-zsh cleanup')
            forgit = next(task for task in block if task['name'].startswith('Install forgit'))
            tasks = copy.deepcopy([ohmyzsh, forgit])
            for index, task in enumerate(tasks):
                task['ansible.builtin.git'].update(repo=repo.as_uri(), dest=str(root / f'checkout-{index}'),
                                                 executable=str(executable))
            play = root / 'clone.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                'vars': {'dotfiles_ohmyzsh_version': pinned, 'dotfiles_forgit_version': pinned}, 'tasks': tasks}]))
            for repeat in [False, True]:
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                                        env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('Ignoring depth argument', result.stderr)
                if repeat:
                    self.assertEqual(re.findall(r'changed=(\d+)', result.stdout)[-1], '0', result.stdout)
                for index in range(2):
                    checkout = root / f'checkout-{index}'
                    self.assertEqual(git('rev-parse', 'HEAD', cwd=checkout), pinned)
                    self.assertEqual(git('rev-parse', '--is-shallow-repository', cwd=checkout), 'true')
            self.assertEqual(calls.read_text().splitlines(), ['clone', 'clone', 'clone'])


if __name__ == '__main__':
    unittest.main()
