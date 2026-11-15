#!/usr/bin/env python3
"""Check content of rendered configs and shipped units/hooks (real Ansible engine, no host changes)."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
VARS_FILES = ['default.config.yml', 'group_vars/all/vars.yml'] + [
    f'roles/{role}/defaults/main.yml' for role in ('system', 'dotfiles', 'desktop', 'containers')]

# name -> (template, extra vars)
CASES = {
    'ssh_seeded': ('roles/dotfiles/templates/ssh_config.j2', {
        'profile': 'work',
        'dotfiles_ssh_work_hosts': [{'host': 'git-a.example.internal'},
                                    {'host': 'git-b.example.internal', 'user': 'git'}],
        'ssh_work_keyscan_hosts': [
            {'host': 'git-a.example.internal', 'fingerprint': 'SHA256:' + 'A' * 43},
            'git-b.example.internal', 'git.example.test']}),
    'ssh_unseeded': ('roles/dotfiles/templates/ssh_config.j2', {
        'profile': 'work',
        'dotfiles_ssh_work_hosts': [{'host': 'git-a.example.internal'},
                                    {'host': 'git-b.example.internal', 'user': 'git'}],
        'ssh_work_keyscan_hosts': [
            {'host': 'git-a.example.internal', 'fingerprint': 'SHA256:placeholder'}]}),
    'policy': ('roles/containers/templates/policy.json.j2', {'is_dnf': True}),
    'usbguard': ('roles/system/templates/usbguard-rules.conf.j2', {}),
    'gitconfig': ('roles/dotfiles/templates/gitconfig.j2', {}),
    'sway': ('roles/desktop/templates/sway.config.j2', {}),
    'i3': ('roles/desktop/templates/i3.config.j2', {}),
    'i3status': ('roles/desktop/templates/i3status.conf.j2', {}),
    'bar_status': ('roles/desktop/templates/i3status-bar.sh.j2', {}),
    'audit': ('roles/system/templates/auditd-system.rules.j2', {}),
}
BASE = {'real_user_id': 'public-test-user', 'dotfiles_user_name': 'Test User',
        'dotfiles_user_email_work': 'w@example.test', 'dotfiles_user_email_personal': 'p@example.test',
        'dotfiles_github_user': 'testuser', 'ansible_managed': 'Ansible managed'}


def hosts(text):
    """Return {Host pattern line: [directive lines]} preserving order."""
    blocks, current = [], None
    for line in text.splitlines():
        if line.startswith('Host '):
            current = (line, [])
            blocks.append(current)
        elif current and line.strip() and not line.lstrip().startswith('#'):
            current[1].append(line.strip())
    return blocks


class RenderedConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name)
        tasks = []
        for name, (src, extra) in CASES.items():
            tasks.append({'name': f'render {name}', 'ansible.builtin.template': {
                'src': str(ROOT / src), 'dest': str(out / name), 'mode': '0600'},
                'vars': extra})
        play = [{'hosts': 'localhost', 'gather_facts': True, 'gather_subset': ['min'],
                 'vars_files': [str(ROOT / p) for p in VARS_FILES], 'vars': BASE, 'tasks': tasks}]
        (out / 'play.yml').write_text(yaml.safe_dump(play))
        result = subprocess.run(
            [str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(out / 'play.yml')], cwd=ROOT,
            env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
            capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.out = out

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def read(self, name):
        return (self.out / name).read_text()

    def test_ssh_config_block_order_and_strict_checking(self):
        for name in ('ssh_seeded', 'ssh_unseeded'):
            text = self.read(name)
            blocks = hosts(text)
            patterns = [b[0] for b in blocks]
            self.assertEqual(patterns[-1], 'Host *', name)
            self.assertEqual(patterns[0], 'Host gh gist github.com gist.github.com', name)
            self.assertEqual(len([p for p in patterns if p == 'Host *']), 1)
            star = dict(blocks)['Host *']
            self.assertIn('StrictHostKeyChecking accept-new', star)
            self.assertNotIn('StrictHostKeyChecking yes', star)
            self.assertGreater(text.index('Include ~/.ssh/config.local'), text.index('\nHost *\n'))
        seeded = dict(hosts(self.read('ssh_seeded')))
        self.assertIn('StrictHostKeyChecking yes', seeded['Host git-a.example.internal'])
        self.assertIn('StrictHostKeyChecking yes', seeded['Host git-b.example.internal'])
        unseeded = dict(hosts(self.read('ssh_unseeded')))
        host = 'Host git-a.example.internal'
        self.assertNotIn('StrictHostKeyChecking yes', unseeded[host], host)
        self.assertIn('StrictHostKeyChecking yes', unseeded['Host gh gist github.com gist.github.com'])

    def test_policy_json_registry_allowlist(self):
        import json
        policy = json.loads(self.read('policy'))
        self.assertEqual(policy['default'], [{'type': 'reject'}])
        docker = policy['transports']['docker']
        for registry in ('quay.io', 'docker.io', 'ghcr.io', 'registry.k8s.io', 'registry.fedoraproject.org',
                         'mcr.microsoft.com', 'public.ecr.aws'):
            self.assertEqual(docker[registry], [{'type': 'insecureAcceptAnything'}], registry)
        for registry in ('registry.redhat.io', 'registry.access.redhat.com'):
            self.assertEqual(docker[registry][0]['type'], 'signedBy', registry)
        self.assertNotIn('', docker, 'no catch-all docker entry may weaken the reject default')
        for transport in ('docker-archive', 'oci-archive', 'dir', 'oci', 'containers-storage'):
            self.assertIn(transport, policy['transports'])

    def test_usbguard_rules_have_no_name_globs(self):
        rules = [l for l in self.read('usbguard').splitlines() if l and not l.startswith('#')]
        self.assertTrue(any(l.startswith('allow id 1050:*') for l in rules))
        for rule in rules:
            self.assertNotRegex(rule, r'\bname\b[^\n]*\*', rule)

    def test_gitconfig_aliases_sign_off(self):
        text = self.read('gitconfig')
        alias = text.split('[alias]')[1].split('\n[')[0]
        self.assertRegex(alias, r'(?m)^\s*fixup = commit -s --fixup$')
        self.assertRegex(alias, r"(?m)^\s*wip = commit -s -m 'WIP: snapshot'$")

    def test_gitconfig_https_for_own_repos_ssh_for_upstream_and_pushsk(self):
        user = re.search(r'insteadOf = git@github.com:([^/]+)/', self.read('gitconfig')).group(1)
        with tempfile.TemporaryDirectory() as name:
            log = Path(name) / 'ssh-log'
            fake_ssh = Path(name) / 'fake-ssh'
            fake_ssh.write_text(f'#!/bin/sh\n[ "$1" = -G ] && exit 0\necho "$@" >> {log}\nexit 1\n')
            fake_ssh.chmod(0o755)
            env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.out / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1', HOME=name,
                       GIT_SSH_COMMAND=f'sh {fake_ssh}', GIT_TERMINAL_PROMPT='0')
            repo = Path(name) / 'r'
            (Path(name) / '.config/git').mkdir(parents=True)
            shutil.copy(ROOT / 'roles/dotfiles/files/git-pushsk', Path(name) / '.config/git/pushsk')

            def git(*args, check=True):
                return subprocess.run(['git', '-C', str(repo), *args], env=env, capture_output=True, text=True,
                                      check=check).stdout.strip()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
            git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'x')
            own = f'https://github.com/{user}/r.git'
            for remote, url, fetch, push in [
                    ('a', own, own, own), ('b', f'git@github.com:{user}/r.git', own, own),
                    ('c', f'ssh://git@github.com/{user}/r.git', own, own),
                    ('d', 'https://github.com/up/r.git', 'https://github.com/up/r.git', 'ssh://git@github.com/up/r.git')]:
                git('remote', 'add', remote, url)
                self.assertEqual(git('remote', 'get-url', remote), fetch, url)
                self.assertEqual(git('remote', 'get-url', '--push', remote), push, url)
                git('pushsk', remote, check=False)
                self.assertIn(f'{"up" if remote == "d" else user}/r.git', log.read_text().splitlines()[-1])
                self.assertIn('-p 22', log.read_text().splitlines()[-1])  # the explicit-port SSH URL
            # pushsk follows the push URL (fetch from upstream, push to the fork) and refuses -u.
            git('remote', 'add', 'e', 'https://github.com/up/r.git')
            git('remote', 'set-url', '--push', 'e', f'git@github.com:{user}/fork.git')
            git('pushsk', 'e', 'HEAD', check=False)
            self.assertIn(f'{user}/fork.git', log.read_text().splitlines()[-1])
            refused = subprocess.run(['git', '-C', str(repo), 'pushsk', 'e', '-u', 'HEAD'], env=env, capture_output=True,
                                     text=True, check=False)
            self.assertEqual(refused.returncode, 2, refused.stderr)

    def test_global_pre_push_refuses_https_pushes_outside_own_github_user(self):
        user = re.search(r'insteadOf = git@github.com:([^/]+)/', self.read('gitconfig')).group(1)
        hook = ROOT / 'roles/dotfiles/files/git-template-pre-push'
        with tempfile.TemporaryDirectory() as name:
            env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.out / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1', HOME=name)
            subprocess.run(['git', 'init', '-q', name], check=True, env=env)
            self.assertEqual(subprocess.run(['git', '-C', name, 'config', 'github.user'], env=env, capture_output=True,
                                            text=True, check=True).stdout.strip(), user)

            def push(url, global_config=str(self.out / 'gitconfig')):
                return subprocess.run(['sh', str(hook), 'origin', url], cwd=name, input='',
                                      env=dict(env, GIT_CONFIG_GLOBAL=global_config), capture_output=True,
                                      text=True, check=False).returncode
            for url in [f'https://github.com/{user}/r.git', f'https://github.com/{user.upper()}/r',
                        f'https://x-access-token:t@github.com/{user}/r.git', f'https://github.com/{user}/r.git/',
                        f'ssh://git@github.com/up/r.git',
                        'git@github.com:up/r.git', 'https://gitlab.com/up/r.git', '/srv/r.git']:
                self.assertEqual(push(url), 0, url)
            for url in ['https://github.com/up/r.git', 'HTTPS://GitHub.com/up/r', 'https://github.com:443/up/r.git',
                        'https://www.github.com/up/r', 'https://tok@github.com/up/r.git', 'http://github.com/up/r',
                        'https://github.com./up/r', 'https://github.com//up/r.git',
                        f'https://github.com/{user}x/r.git', f'https://github.com/x{user}/r.git',
                        f'https://github.com/{user}/../up/r', f'https://github.com/{user}/%2e%2e/up/r',
                        f'https://github.com/{user}/r/../../up/r', f'https://github.com/{user}//up/r',
                        f'https://github.com/{user}/..', f'https://github.com/{user}/r%2fx']:
                self.assertEqual(push(url), 1, url)
            self.assertEqual(push(f'https://github.com/{user}/r.git', os.devnull), 1)  # no github.user: refuse

    def test_fork_remotes_use_https_so_agents_can_push(self):
        repos = yaml.safe_load((ROOT / 'group_vars/all/repos.yml').read_text())
        remotes = [remote['url'] for repo in repos['git_repos_public'] for remote in repo.get('extra_remotes', [])]
        self.assertTrue(remotes)
        self.assertEqual([url for url in remotes if not url.startswith('https://github.com/')], [])

    def test_wm_configs_have_no_trailing_comments_after_commands(self):
        for name in ('sway', 'i3'):
            for number, line in enumerate(self.read(name).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(('exec', 'bindsym', 'bindcode', 'output', 'input')):
                    # sway/i3 pass a trailing '#...' to the command (exec) or reject it
                    self.assertNotRegex(line, r'\s#\s', f'{name}:{number}: {line}')

    def test_i3status_order_entries_on_separate_lines(self):
        lines = self.read('i3status').splitlines()
        order = [l for l in lines if l.startswith('order')]
        self.assertGreaterEqual(len(order), 8)
        for line in order:
            self.assertRegex(line, r'^order \+= "[^"]+"$')

    def test_bar_counts_rootless_containers_and_only_terminal_agent_sessions(self):
        with tempfile.TemporaryDirectory() as name:
            stubs = Path(name)
            def stub(command, body):
                (stubs / command).write_text('#!/bin/sh\n' + body + '\n')
                (stubs / command).chmod(0o755)
            stub('i3status', 'printf \'{"version":1}\\n[\\n[{"name":"wireless","full_text":"WIFI"}]\\n\'')
            stub('podman', 'printf "a1\\nb2\\n"')
            stub('ps', 'printf "pts/0    claude\\n?        codex\\n?        codex\\npts/3    codex\\n'
                       '?        agy\\npts/4    node\\npts/5    zsh\\npts/3    codex\\npts/0    agy\\n"')
            for quiet in ['nmcli', 'tailscale', 'rpm']:
                stub(quiet, 'exit 1')
            script = stubs / 'bar-status'
            script.write_text(self.read('bar_status'))
            result = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=30,
                                    env=dict(os.environ, PATH=f'{stubs}:/usr/bin:/bin', HOME=name))
            update = result.stdout.splitlines()[-1]
            self.assertIn('"full_text":"CTR 2"', update)
            # pts/0 and pts/3: a second agent process on a terminal is the same session
            self.assertIn('"full_text":"AI 2"', update)

    def test_audit_rules_set_backlog_before_rules(self):
        lines = [l for l in self.read('audit').splitlines() if l and not l.startswith('#')]
        self.assertEqual(lines.count('-b 8192'), 1)
        self.assertLess(lines.index('-b 8192'), min(i for i, l in enumerate(lines) if l.startswith(('-w', '-a'))))


class ShippedFileTests(unittest.TestCase):
    def test_units_hardening_matches_selinux_constraints(self):
        aide = (ROOT / 'roles/system/files/aide-check.service').read_text()
        active = [l for l in aide.splitlines() if not l.lstrip().startswith('#')]
        self.assertFalse([l for l in active if l.startswith('NoNewPrivileges')])
        setuid = (ROOT / 'roles/system/files/setuid-scan.service').read_text().splitlines()
        self.assertIn('ExecStart=/usr/bin/bash /usr/local/lib/setuid-scan.sh', setuid)
        self.assertIn('StateDirectory=setuid-scan', setuid)
        self.assertFalse([l for l in setuid if l.startswith('ReadWritePaths')])
        prune = [l for l in (ROOT / 'roles/containers/files/podman-prune.service').read_text().splitlines()
                 if not l.startswith('#')]
        for forbidden in ('NoNewPrivileges', 'PrivateTmp', 'ProtectSystem', 'ProtectHome', 'RestrictNamespaces',
                          'PrivateUsers', 'PrivateDevices', 'CapabilityBoundingSet',
                          'ProtectHostname', 'ProtectClock'):
            self.assertFalse([l for l in prune if l.startswith(forbidden)], forbidden)

    def test_aide_log_rules_use_growing_not_deprecated_S(self):
        text = (ROOT / 'roles/system/tasks/aide.yml').read_text()
        rules = re.findall(r"line: '(/var/log/\S+ [^']+)'", text)
        self.assertGreaterEqual(len(rules), 3)
        for rule in rules:
            self.assertIn('+growing+s+', rule)
            self.assertNotRegex(rule, r'\+S\b')

    def test_commit_msg_hook_chains_repo_hook_from_linked_worktree(self):
        hook = ROOT / 'roles/dotfiles/files/git-template-commit-msg'
        env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null',
                   GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.test',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.test', PATH='/usr/bin:/bin')
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            work = Path(directory) / 'linked'
            run = lambda *a, cwd=repo: subprocess.run(a, cwd=cwd, env=env, check=True, capture_output=True)
            run('git', 'init', '-q', str(repo), cwd=directory)
            run('git', 'commit', '-q', '--allow-empty', '-m', 'init')
            run('git', 'worktree', 'add', '-q', str(work))
            marker = Path(directory) / 'ran'
            repo_hook = repo / '.git/hooks/commit-msg'
            repo_hook.write_text(f'#!/bin/sh\necho "$1" > {marker}\nexit "${{HOOK_EXIT:-0}}"\n')
            repo_hook.chmod(0o755)
            msg = Path(directory) / 'msg'
            msg.write_text('Docs: note\n\nSigned-off-by: T <t@example.test>\n')
            result = subprocess.run(['bash', str(hook), str(msg)], cwd=work, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text().strip(), str(msg), 'repo hook not found from linked worktree')
            result = subprocess.run(['bash', str(hook), str(msg)], cwd=work, env=dict(env, HOOK_EXIT='7'),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, 'repo hook failure must abort the commit')


    def test_global_commit_msg_hook_allows_other_repo_conventions(self):
        """The global hook runs in every repo: it must require sign-off but not this repo's style."""
        hook = ROOT / 'roles/dotfiles/files/git-template-commit-msg'
        env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null', PATH='/usr/bin:/bin')
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['git', 'init', '-q', directory], env=env, check=True)
            msg = Path(directory) / 'msg'
            for subject in ('ovnkube: fix leak in node controller', 'Fix typo.', 'Added a test'):
                msg.write_text(subject + '\n\nSigned-off-by: T <t@example.test>\n')
                result = subprocess.run(['bash', str(hook), str(msg)], cwd=directory, env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, f'{subject!r}: {result.stderr}')
            msg.write_text('ovnkube: fix leak\n')
            result = subprocess.run(['bash', str(hook), str(msg)], cwd=directory, env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, 'missing Signed-off-by must be rejected')

if __name__ == '__main__':
    unittest.main()
