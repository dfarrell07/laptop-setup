#!/usr/bin/env python3
"""roles/git_repos skips repos on hosts it cannot use this run (unreachable, or internal with no Red Hat git key)
instead of failing them, and leaves the other repos alone (real Ansible engine, temporary HOME)."""
from pathlib import Path
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists() and shutil.which('ansible-playbook'):
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TIMEOUT = int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '120'))
UNREACHABLE = '192.0.2.1'  # TEST-NET-1: never answers


def git(*args, cwd=None):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                        'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'})


class GitReposSkipTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()  # macOS temp dirs sit under /var, a link to /private/var
        self.addCleanup(shutil.rmtree, self.home)
        (self.home / '.ssh').mkdir(mode=0o700)
        key = self.home / 'hostkey'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        hostkey = ' '.join((self.home / 'hostkey.pub').read_text().split()[:2])
        # seeded hosts, so only the probe (or the key check) can keep their repos out
        (self.home / '.ssh/known_hosts').write_text(f'{UNREACHABLE} {hostkey}\n127.0.0.1 {hostkey}\n')
        # a reachable repo the skips must not touch: a local bare repo with one commit
        self.bare = self.home / 'upstream.git'
        work = self.home / 'work'
        git('init', '-q', '--bare', str(self.bare))
        git('init', '-q', str(work))
        (work / 'f').write_text('x')
        git('add', 'f', cwd=work)
        git('commit', '-q', '-m', 'c', cwd=work)
        git('push', '-q', str(self.bare), 'HEAD:refs/heads/main', cwd=work)
        git('--git-dir', str(self.bare), 'symbolic-ref', 'HEAD', 'refs/heads/main')

    def run_role(self, repos, **extra):
        variables = {
            'ansible_facts': {'user_dir': str(self.home)}, 'common_project_root': str(ROOT), 'profile': 'work',
            'git_repos_base_dir': str(self.home / 'src'), 'git_repos_list': repos, 'git_repos_pull': True,
            'git_repos_category_filter': '', 'git_repos_work_categories': ['t'], 'git_repos_personal_categories': ['t'],
            'dotfiles_github_user': 'me', 'dotfiles_ssh_redhat_key_basename': 'id_rh',
            'is_dnf': True, 'is_apt': False, 'is_macos': False, 'is_linux': True, **extra}
        play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                 'tasks': [{'ansible.builtin.include_tasks': str(ROOT / 'roles/git_repos/tasks/main.yml')}]}]
        playbook = self.home / 'play.yml'
        playbook.write_text(yaml.safe_dump(play))
        return subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                              capture_output=True, text=True, timeout=TIMEOUT, cwd=self.home,
                              env={**os.environ, 'HOME': str(self.home), 'ANSIBLE_LOCAL_TEMP': str(self.home / 'tmp')})

    def local_repo(self):
        return {'url': f'file://{self.bare}', 'dest': 'local/repo', 'category': 't'}

    def assert_skipped_cleanly(self, result, warning):
        out = ' '.join(result.stdout.split())  # either stdout callback, wrapped or not
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(out.count(warning), 1, out)
        self.assertNotIn('FAILED', out)
        self.assertNotIn('fatal:', out)
        self.assertTrue((self.home / 'src/local/repo/f').exists(), out)  # the reachable repo cloned as usual

    def test_unreachable_host_is_skipped_with_one_warning_even_when_pulling(self):
        # an existing clone of the internal repo: make update's pull must not fail the run off the VPN
        existing = self.home / 'src/internal/repo'
        git('init', '-q', str(existing))
        git('remote', 'add', 'origin', f'git@{UNREACHABLE}:group/repo.git', cwd=existing)
        repos = [{'url': f'git@{UNREACHABLE}:group/repo.git', 'dest': 'internal/repo', 'category': 't'},
                 {'url': f'ssh://me@{UNREACHABLE}:2222/other.git', 'dest': 'internal/other', 'category': 't'},
                 self.local_repo()]
        result = self.run_role(repos)
        self.assert_skipped_cleanly(result, f'Skipping repos on {UNREACHABLE} this run: unreachable (VPN down?)')
        self.assertFalse((self.home / 'src/internal/other').exists())
        self.assertNotIn('no known_hosts entry', result.stdout)

    def test_internal_host_without_the_red_hat_key_is_skipped(self):
        listener = socket.create_server(('127.0.0.1', 0))  # answers the probe; nothing may get as far as ssh
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        repos = [{'url': f'ssh://127.0.0.1:{port}/repo.git', 'dest': 'internal/repo', 'category': 't'},
                 self.local_repo()]
        result = self.run_role(repos, dotfiles_ssh_work_hosts=[{'host': '127.0.0.1', 'user': 'git'}])
        self.assert_skipped_cleanly(
            result, 'no Red Hat git key (~/.ssh/id_rh): add vault_rh_git_key (make vault-edit), then make apply')
        self.assertNotIn('VPN down?', result.stdout)

    def kept_by_probe(self, repos, env=None):
        """Run the role's tasks up to the unreachable-host skip; return the dests it keeps."""
        tasks = yaml.safe_load((ROOT / 'roles/git_repos/tasks/main.yml').read_text())
        end = next(i for i, t in enumerate(tasks) if t.get('name') == 'Skip repos on unreachable git hosts')
        tasks = tasks[:end + 1] + [{'ansible.builtin.debug': {'msg': 'KEPT={{ git_repos_filtered | map(attribute="dest") | join(",") }}'}}]
        variables = {'ansible_facts': {'user_dir': str(self.home)}, 'profile': 'work', 'git_repos_list': repos,
                     'git_repos_base_dir': str(self.home / 'src'), 'git_repos_category_filter': '',
                     'git_repos_work_categories': ['t'], 'git_repos_personal_categories': ['t']}
        playbook = self.home / 'probe.yml'
        playbook.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                                             'tasks': tasks}]))
        result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)], capture_output=True,
                                text=True, timeout=TIMEOUT, cwd=self.home,
                                env={**os.environ, 'HOME': str(self.home), **(env or {})})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return re.search(r'KEPT=([a-z0-9,/]*)', result.stdout).group(1).split(',')

    def test_only_repos_on_the_port_that_failed_are_dropped(self):
        listener = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(listener.close)
        open_port = listener.getsockname()[1]
        closed = socket.create_server(('127.0.0.1', 0))
        closed_port = closed.getsockname()[1]
        closed.close()  # nothing listens there now
        repos = [{'url': f'ssh://git@127.0.0.1:{closed_port}/a.git', 'dest': 'a', 'category': 't'},
                 {'url': f'https://127.0.0.1:{open_port}/b.git', 'dest': 'b', 'category': 't'},
                 {'url': f'git@{UNREACHABLE}:c.git', 'dest': 'c', 'category': 't'},
                 {'url': f'https://{UNREACHABLE}/d.git', 'dest': 'd', 'category': 't'},
                 {'url': 'https://github.com/me/e', 'dest': 'e', 'category': 't'}]
        self.assertEqual(self.kept_by_probe(repos, {'https_proxy': '', 'HTTPS_PROXY': ''}), ['b', 'e'])
        # behind a proxy git reaches HTTPS hosts through it, so a direct probe says nothing about them
        self.assertEqual(self.kept_by_probe(repos, {'https_proxy': 'http://proxy.example.internal:3128'}),
                         ['b', 'd', 'e'])

if __name__ == '__main__':
    unittest.main()
