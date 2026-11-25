#!/usr/bin/env python3
"""Pre-trusting managed repos in the Claude Code, Codex and Antigravity state files (roles/common)."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / 'roles/common/files/agent-trust.py'
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists() and shutil.which('ansible-playbook'):
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TIMEOUT = int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '120'))


def helper(*args):
    return subprocess.run([sys.executable, '-I', str(HELPER), *map(str, args)], capture_output=True, text=True)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()  # macOS temp dirs sit under /var, a link to /private/var
        self.addCleanup(shutil.rmtree, self.dir)

    def test_claude_adds_trust_and_keeps_everything_else(self):
        state = self.dir / '.claude.json'
        original = {'projects': {'/a': {'hasTrustDialogAccepted': False, 'allowedTools': ['x']}}, 'other': 'é'}
        state.write_text(json.dumps(original))
        state.chmod(0o644)
        self.assertEqual(helper('claude', state, '--check', '/a', '/b').stdout.strip(), 'changed')
        self.assertEqual(json.loads(state.read_text()), original)
        self.assertEqual(helper('claude', state, '/a', '/b').stdout.strip(), 'changed')
        data = json.loads(state.read_text())
        self.assertEqual(data['other'], 'é')
        self.assertEqual(data['projects']['/a'], {'hasTrustDialogAccepted': True, 'allowedTools': ['x']})
        self.assertIs(data['projects']['/b']['hasTrustDialogAccepted'], True)
        self.assertEqual(data['projects']['/b']['mcpServers'], {})
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        before = state.read_text()
        self.assertEqual(helper('claude', state, '/a', '/b').stdout.strip(), 'unchanged')
        self.assertEqual(state.read_text(), before)

    def test_codex_appends_project_tables_and_respects_existing_ones(self):
        state = self.dir / 'config.toml'
        state.write_text('model = "m"\n[projects."/a"]\ntrust_level = "untrusted"\n')
        self.assertEqual(helper('codex', state, '/a', '/b c').stdout.strip(), 'changed')
        self.assertEqual(tomllib.loads(state.read_text()), {
            'model': 'm', 'projects': {'/a': {'trust_level': 'untrusted'}, '/b c': {'trust_level': 'trusted'}}})
        self.assertEqual(helper('codex', state, '/a', '/b c').stdout.strip(), 'unchanged')

    def test_agy_appends_trusted_workspaces_once(self):
        state = self.dir / 'settings.json'
        state.write_text('{"model": "m", "trustedWorkspaces": ["/a"]}')
        self.assertEqual(helper('agy', state, '/a', '/b').stdout.strip(), 'changed')
        self.assertEqual(json.loads(state.read_text()), {'model': 'm', 'trustedWorkspaces': ['/a', '/b']})
        self.assertEqual(helper('agy', state, '/b').stdout.strip(), 'unchanged')

    def test_missing_files_bad_files_and_bad_paths_are_left_alone(self):
        self.assertEqual(helper('agy', self.dir / 'none.json', '/a').stdout.strip(), 'missing')
        self.assertFalse((self.dir / 'none.json').exists())
        broken = self.dir / 'broken.json'
        broken.write_text('{not json')
        self.assertNotEqual(helper('claude', broken, '/a').returncode, 0)
        self.assertEqual(broken.read_text(), '{not json')
        for bad in ['relative', '/a\nb']:
            self.assertNotEqual(helper('agy', broken, bad).returncode, 0, bad)

    def test_paths_are_normalized_and_symlinks_trusted_under_both_spellings(self):
        real = self.dir / 'real'
        (real / 'repo').mkdir(parents=True)
        (self.dir / 'link').symlink_to(real)
        state = self.dir / 'settings.json'
        state.write_text('{"trustedWorkspaces": []}')
        self.assertEqual(helper('agy', state, f'{self.dir}/link//repo/', '/x/./y').stdout.strip(), 'changed')
        self.assertEqual(json.loads(state.read_text())['trustedWorkspaces'],
                         [f'{self.dir}/link/repo', '/x/y', f'{real}/repo'])


@unittest.skipUnless(ANSIBLE.exists(), 'needs ansible-playbook')
class RoleTaskTests(unittest.TestCase):
    def run_play(self, home, variables, check=False):
        play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                 'tasks': [{'ansible.builtin.include_tasks': str(ROOT / 'roles/common/tasks/agent_trust.yml')}]}]
        playbook = home / 'play.yml'
        playbook.write_text(yaml.safe_dump(play))
        result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)]
                                + (['--check'] if check else []),
                                capture_output=True, text=True, timeout=TIMEOUT, cwd=home,
                                env={**os.environ, 'HOME': str(home), 'ANSIBLE_LOCAL_TEMP': str(home / 'tmp'),
                                     'ANSIBLE_VAULT_PASSWORD_FILE': str(ROOT / 'scripts/vault-pass-ci.sh')})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_own_repos_are_trusted_and_upstream_ones_only_with_all(self):
        shared = yaml.safe_load((ROOT / 'group_vars/all/vars.yml').read_text())
        with tempfile.TemporaryDirectory() as name:
            name = str(Path(name).resolve())  # macOS: /var is a link to /private/var, which trust adds as a second spelling
            home = Path(name)
            state = home / 'settings.json'
            state.write_text('{"trustedWorkspaces": []}')
            busy = home / 'broken.json'
            busy.write_text('{not json')
            variables = {key: shared[key] for key in ['agents_trust_repos', 'agents_trusted_dirs_extra',
                                                     'agents_trusted_dirs']}
            variables.update({
                'ansible_facts': {'user_dir': name}, 'role_path': str(ROOT / 'roles/antigravity'),
                'dotfiles_github_user': 'me', 'agents_trusted_dirs_extra': [name + '/checkout'],
                'git_repos_list': [{'url': 'https://github.com/me/mine', 'dest': 'me/mine'},
                                   {'url': 'git@github.com:me/other.git', 'dest': 'me/other'},
                                   {'url': 'https://github.com/upstream/thing', 'dest': 'upstream/thing'},
                                   {'url': 'https://github.com/meme/x', 'dest': 'meme/x'}],
                '_agent_trust_kind': 'agy', '_agent_trust_files': [str(state), str(busy), name + '/none.json']})
            output = self.run_play(home, variables, check=True)
            self.assertEqual(json.loads(state.read_text()), {'trustedWorkspaces': []})
            self.assertIn('WARNING', output)
            self.run_play(home, variables)
            self.assertEqual(json.loads(state.read_text())['trustedWorkspaces'],
                             [name + '/src/me/mine', name + '/src/me/other', name + '/checkout'])
            self.assertIn('changed=0', self.run_play(home, variables))
            self.run_play(home, dict(variables, agents_trust_repos='all'))
            self.assertIn(name + '/src/upstream/thing', json.loads(state.read_text())['trustedWorkspaces'])


if __name__ == '__main__':
    unittest.main()
