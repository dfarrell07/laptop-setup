#!/usr/bin/env python3
"""Unit tests for roles/claude/files/claude-project-modes.py (temp dirs and temp git repos only)."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / 'roles/claude/files/claude-project-modes.py'


class ProjectModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.root = self.home / 'src'
        self.units = self.home / 'units'
        self.agents = self.home / 'agents'
        for d in (self.root, self.units, self.agents):
            d.mkdir()
        gitconfig = self.home / 'gitconfig'
        gitconfig.write_text('[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n')
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_GLOBAL=str(gitconfig),
                        GIT_CONFIG_NOSYSTEM='1')

    def git(self, repo, *args):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=self.env, timeout=30)

    def repo(self, rel, mode=None, ignore_local=True, local=None):
        repo = self.root / rel
        (repo / '.claude').mkdir(parents=True)
        self.git(repo, 'init', '-q')
        if ignore_local:
            (repo / '.gitignore').write_text('.claude/settings.local.json\n')
        settings = {'permissions': {'allow': ['Bash(make:*)']}}
        if mode:
            settings['permissions']['defaultMode'] = mode
        (repo / '.claude/settings.json').write_text(json.dumps(settings))
        if local is not None:
            (repo / '.claude/settings.local.json').write_text(json.dumps(local))
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-qm', 'init')
        return repo

    def run_helper(self, action, *extra):
        result = subprocess.run([sys.executable, '-I', str(HELPER), action, '--root', str(self.root),
                                 '--unit-dir', str(self.units), '--agent-dir', str(self.agents),
                                 '--exclude-file', str(self.home / 'exclude'), *extra],
                                capture_output=True, text=True, env=self.env, timeout=60)
        return result.returncode, json.loads(result.stdout)

    def local(self, repo):
        return json.loads((repo / '.claude/settings.local.json').read_text())

    def test_clean_machine_passes(self):
        self.repo('plain')
        self.repo('bypass', mode='bypassPermissions')
        self.assertEqual(self.run_helper('--check'), (0, {'changed': [], 'problems': []}))

    def test_check_reports_without_writing(self):
        repo = self.repo('owner/health', mode='acceptEdits')
        rc, out = self.run_helper('--check')
        self.assertEqual(rc, 1)
        self.assertIn('acceptEdits', out['problems'][0])
        self.assertFalse((repo / '.claude/settings.local.json').exists())

    def test_apply_writes_only_the_mode_and_keeps_other_local_keys(self):
        repo = self.repo('owner/health', mode='acceptEdits', local={'permissions': {'allow': ['Bash(ls:*)']}, 'x': 1})
        self.git(repo, 'status')  # local file is ignored, so the tree stays clean
        rc, out = self.run_helper('--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(out['changed']), 1)
        self.assertEqual(self.local(repo), {'permissions': {'allow': ['Bash(ls:*)'], 'defaultMode': 'bypassPermissions'},
                                            'x': 1})
        self.assertEqual(oct((repo / '.claude/settings.local.json').stat().st_mode & 0o777), '0o600')
        self.assertEqual(self.run_helper('--check'), (0, {'changed': [], 'problems': []}))
        self.assertEqual(self.run_helper('--apply'), (0, {'changed': [], 'problems': []}))  # idempotent

    def test_local_mode_alone_is_overridden(self):
        repo = self.repo('r', local={'permissions': {'defaultMode': 'plan'}})
        rc, _ = self.run_helper('--apply')
        self.assertEqual(rc, 0)
        self.assertEqual(self.local(repo)['permissions']['defaultMode'], 'bypassPermissions')

    def test_never_writes_where_local_file_is_not_ignored(self):
        repo = self.repo('r', mode='auto', ignore_local=False)
        rc, out = self.run_helper('--apply')
        self.assertEqual(rc, 1)
        self.assertIn('not git-ignored', out['problems'][0])
        self.assertFalse((repo / '.claude/settings.local.json').exists())

    def test_worktrees_are_covered(self):
        repo = self.repo('r', mode='acceptEdits')
        wt = repo / '.claude/worktrees/agent-1'
        self.git(repo, 'worktree', 'add', '-q', str(wt))
        rc, out = self.run_helper('--apply')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.local(wt)['permissions']['defaultMode'], 'bypassPermissions')

    def test_depth_and_hidden_directories_pruned(self):
        self.repo('a/b/c/d/deep', mode='acceptEdits')
        self.repo('.hidden/r', mode='acceptEdits')
        self.repo('node_modules/r', mode='acceptEdits')
        self.assertEqual(self.run_helper('--check', '--depth', '3')[0], 0)
        self.assertEqual(self.run_helper('--check', '--depth', '5')[0], 1)

    def test_excluded_checkouts_and_their_units_keep_their_mode(self):
        kept = self.repo('owner/kept', mode='acceptEdits')
        fixed = self.repo('owner/fixed', mode='acceptEdits')
        (self.units / 'kept.service').write_text(f'[Service]\nWorkingDirectory={kept}\nExecStart=/usr/bin/claude\n')
        (self.home / 'exclude').write_text(f'# comment\n\n{kept}\n')
        rc, out = self.run_helper('--apply')
        self.assertEqual((rc, out['problems']), (0, []))
        self.assertFalse((kept / '.claude/settings.local.json').exists())
        self.assertEqual(self.local(fixed)['permissions']['defaultMode'], 'bypassPermissions')
        (self.home / 'exclude').write_text(f'{self.root / "owner"}\n')  # a path covers everything below it
        self.assertEqual(self.run_helper('--check'), (0, {'changed': [], 'problems': []}))

    def test_mode_flag_respected(self):
        repo = self.repo('r', mode='default')
        rc, _ = self.run_helper('--apply', '--mode', 'acceptEdits')
        self.assertEqual(rc, 0)
        self.assertEqual(self.local(repo)['permissions']['defaultMode'], 'acceptEdits')

    def unit(self, name, cmd, workdir):
        (self.units / name).write_text(f'[Service]\nWorkingDirectory={workdir}\nExecStart={cmd}\n')

    def test_units_without_a_flag_into_another_mode_are_reported(self):
        repo = self.repo('health', mode='acceptEdits')
        tmux = '/usr/bin/tmux -L s new-session -d -s s'
        self.unit('bare.service', f'{tmux} /home/u/.local/bin/claude --name "X Y" --remote-control', repo)
        self.unit('skip.service', f'{tmux} /home/u/.local/bin/claude --dangerously-skip-permissions', repo)
        self.unit('explicit.service', '/usr/bin/claude --permission-mode acceptEdits', repo)
        self.unit('print.service', '/usr/bin/claude -p hi', repo)
        self.unit('other.service', '/usr/bin/make up', repo)
        rc, out = self.run_helper('--check')
        self.assertEqual(rc, 1)
        units = [p for p in out['problems'] if p.endswith('defaultMode') and p.startswith(('bare', 'skip',
                                                                                             'explicit', 'print'))]
        self.assertEqual(len(units), 1, out)
        self.assertTrue(units[0].startswith('bare.service'))
        rc, out = self.run_helper('--apply')  # the repo override settles the unit too
        self.assertEqual((rc, out['problems']), (0, []))

    def test_unit_resolves_to_user_settings_outside_a_repo(self):
        (self.home / '.claude').mkdir()
        (self.home / '.claude/settings.json').write_text(json.dumps({'permissions': {'defaultMode': 'default'}}))
        self.unit('u.service', '/usr/bin/claude', self.home)
        rc, out = self.run_helper('--check')
        self.assertEqual(rc, 1)
        self.assertIn('resolves to default', out['problems'][0])
        (self.home / '.claude/settings.json').write_text(json.dumps({'permissions': {'defaultMode': 'bypassPermissions'}}))
        self.assertEqual(self.run_helper('--check')[0], 0)

    def test_launch_agents(self):
        repo = self.repo('r', mode='acceptEdits')
        with (self.agents / 'a.plist').open('wb') as fh:
            plistlib.dump({'Label': 'a', 'ProgramArguments': ['/bin/zsh', '-c', 'exec claude --name a'],
                           'WorkingDirectory': str(repo)}, fh)
        rc, out = self.run_helper('--check')
        self.assertTrue(any(p.startswith('a.plist') for p in out['problems']), out)


if __name__ == '__main__':
    unittest.main()
