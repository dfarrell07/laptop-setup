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
    'gitconfig_linux': ('roles/dotfiles/templates/gitconfig.j2', {'ansible_facts': {'system': 'Linux'}}),
    'gitconfig_darwin': ('roles/dotfiles/templates/gitconfig.j2', {'ansible_facts': {'system': 'Darwin'}}),
    'sway': ('roles/desktop/templates/sway.config.j2', {}),
    'i3': ('roles/desktop/templates/i3.config.j2', {}),
    'i3status': ('roles/desktop/templates/i3status.conf.j2', {}),
    'bar_status': ('roles/desktop/templates/i3status-bar.sh.j2', {}),
    'audit': ('roles/system/templates/auditd-system.rules.j2', {}),
    'sshd_macos': ('roles/desktop/templates/sshd-macos.conf.j2', {}),
    # what the play keeps on a Mac whose OpenSSH predates ML-KEM
    'sshd_macos_older': ('roles/desktop/templates/sshd-macos.conf.j2', {'_desktop_sshd_algorithms': {
        'KexAlgorithms': ['sntrup761x25519-sha512', 'curve25519-sha256'], 'Ciphers': ['chacha20-poly1305@openssh.com'],
        'MACs': ['hmac-sha2-512-etm@openssh.com'], 'HostKeyAlgorithms': ['ssh-ed25519'],
        'PubkeyAcceptedAlgorithms': ['ssh-ed25519', 'sk-ssh-ed25519@openssh.com']}}),
    'sshd_launchd': ('roles/desktop/templates/sshd-launchd.plist.j2', {}),
    'zshenv': ('roles/dotfiles/templates/zshenv.j2', {'dotfiles_homebrew_prefix': '/opt/homebrew'}),
    # podman from PATH (a stub); the hold file path is replaced per test
    'podman_ensure': ('roles/containers/templates/podman-machine-ensure.sh.j2', {
        'containers_podman_bin': 'podman', 'containers_macos_machine_hold_file': '/HOLD-FILE-PLACEHOLDER'}),
    'ssh_agent_plist': ('roles/dotfiles/templates/ssh-agent.plist.j2', {'dotfiles_homebrew_prefix': '/opt/homebrew'}),
    'ssh_auth_sock_plist': ('roles/dotfiles/templates/ssh-auth-sock.plist.j2', {}),
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
            for args in [['e', '-fu', 'HEAD'], ['e', '--set-up', 'HEAD'], ['e', '--force-with-lease', 'HEAD'],
                         ['e', '--force-with-lease=main', 'HEAD'], ['-f', 'e', 'HEAD']]:
                refused = subprocess.run(['git', '-C', str(repo), 'pushsk', *args], env=env, capture_output=True,
                                         text=True, check=False)
                self.assertEqual(refused.returncode, 2, (args, refused.stderr))
            lines = len(log.read_text().splitlines())
            git('pushsk', 'e', f'--force-with-lease=main:{"0" * 40}', 'HEAD', check=False)
            self.assertEqual(len(log.read_text().splitlines()), lines + 1)  # an explicit lease reaches ssh

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
                        f'https://github.com/{user}/..', f'https://github.com/{user}/r%2fx',
                        'https://github%2ecom/up/r', f'https://github%2Ecom/{user}/r', 'https://%67ithub.com/up/r',
                        'https::https://github.com/up/r']:
                self.assertEqual(push(url), 1, url)
            self.assertEqual(push(f'https://github.com/{user}/r.git', os.devnull), 1)  # no github.user: refuse

    def test_credential_helper_gives_the_token_only_to_own_and_allowlisted_repos(self):
        user = re.search(r'insteadOf = git@github.com:([^/]+)/', self.read('gitconfig')).group(1)
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / '.config/git').mkdir(parents=True)
            shutil.copy(ROOT / 'roles/dotfiles/files/git-gh-credential', home / '.config/git/gh-credential')
            (home / '.config/git/github-https-read').write_text('priv/readme\n')
            (home / 'bin').mkdir()
            (home / 'bin/gh').write_text('#!/bin/sh\ncat >/dev/null\nprintf "username=x\\npassword=TOKEN\\n"\n')
            (home / 'bin/gh').chmod(0o755)
            # macOS names gh by its Homebrew path, which would reach the host's real gh: the stub stands in for it
            helper = re.compile(r'^(\thelper = !sh ~/\.config/git/gh-credential) (\S+)$', re.M)
            self.assertRegex(helper.search(self.read('gitconfig')).group(2), r'^(gh|/.+/bin/gh)$')
            gitconfig = home / 'gitconfig'
            gitconfig.write_text(helper.sub(lambda m: f"{m.group(1)} {home / 'bin/gh'}", self.read('gitconfig')))
            env = dict(os.environ, GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM='1',
                       HOME=name, XDG_CONFIG_HOME=str(home / '.config'), GIT_TERMINAL_PROMPT='0',
                       GIT_ASKPASS='', SSH_ASKPASS='', PATH=f"{home / 'bin'}:{os.environ['PATH']}")
            env.pop('GIT_ASKPASS'); env.pop('SSH_ASKPASS')

            def token(url):
                result = subprocess.run(['git', 'credential', 'fill'], input=f'url={url}\n\n', cwd=name, env=env,
                                        capture_output=True, text=True, check=False)
                return 'password=TOKEN' in result.stdout
            for url in [f'https://github.com/{user}/r.git', f'https://github.com/{user.upper()}/r',
                        f'https://github.com/{user}/r/', 'https://github.com/priv/readme.git']:
                self.assertTrue(token(url), url)
            for url in ['https://github.com/up/r.git', f'https://github.com/{user}x/r', f'https://github.com/{user}/../up/r',
                        f'https://github.com/{user}/%2e%2e/up/r', f'https://github.com/{user}/r/../../up/r',
                        f'https://github.com/{user}//up/r', 'https://github.com/priv/other', f'https://github.com/{user}',
                        'https://github%2ecom/up/r', 'https://github.com/up/r:x', f'https://github.com/{user}/..',
                        'https://GitHub.com/up/r']:
                self.assertFalse(token(url), url)
            # A repo-local github.user cannot widen the owner.
            subprocess.run(['git', 'init', '-q', name + '/repo'], check=True, env=env)
            subprocess.run(['git', '-C', name + '/repo', 'config', 'github.user', 'up'], check=True, env=env)
            self.assertFalse(subprocess.run(['git', 'credential', 'fill'], input='url=https://github.com/up/r\n\n',
                                            cwd=name + '/repo', env=env, capture_output=True, text=True,
                                            check=False).stdout.count('TOKEN'))

    def test_global_hooks_chain_to_repo_hooks_only_when_opted_in(self):
        with tempfile.TemporaryDirectory() as name:
            home, repo = Path(name), Path(name) / 'repo'
            hooks = home / 'hooks'
            hooks.mkdir()
            for hook in ('prepare-commit-msg', 'commit-msg'):
                shutil.copy(ROOT / f'roles/dotfiles/files/git-template-{hook}', hooks / hook)
                (hooks / hook).chmod(0o755)
            (home / 'gitconfig').write_text(f'[core]\n\thooksPath = {hooks}\n[user]\n\tname = t\n\temail = t@t\n')
            env = dict(os.environ, GIT_CONFIG_GLOBAL=str(home / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1', HOME=name)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
            (repo / '.githooks').mkdir()
            marker = home / 'ran'
            (repo / '.githooks/prepare-commit-msg').write_text(f'#!/bin/sh\necho x >> {marker}\n')
            (repo / '.githooks/prepare-commit-msg').chmod(0o755)

            def commit(setting):
                if setting is None:
                    subprocess.run(['git', '-C', str(repo), 'config', '--unset', 'laptop.repoHooks'], env=env)
                else:
                    subprocess.run(['git', '-C', str(repo), 'config', 'laptop.repoHooks', setting], check=True, env=env)
                subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-s', '--allow-empty', '-m', 'Test: x'],
                               check=True, env=env)
                return marker.read_text().count('x') if marker.exists() else 0
            self.assertEqual(commit(None), 0)  # not opted in: the repo's hooks do not run
            self.assertEqual(commit('.githooks'), 1)
            self.assertEqual(commit('../repo/.githooks'), 1)  # a path out of the repo is ignored
            self.assertEqual(commit('/tmp'), 1)

    def test_commit_msg_skips_sign_off_only_in_temp_repos(self):
        hook = ROOT / 'roles/dotfiles/files/git-template-commit-msg'
        with tempfile.TemporaryDirectory(dir='/tmp') as name:
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', HOME=name)
            message = Path(name) / 'msg'
            message.write_text('Test: no sign-off\n')

            def check(repo):
                subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
                return subprocess.run(['bash', str(hook), str(message)], cwd=repo, env=env, capture_output=True,
                                      text=True, check=False).returncode
            self.assertEqual(check(Path(name) / 'scratch'), 0)  # under /tmp: a test suite's throwaway repo
            clone = Path(name) / 'worktree'
            subprocess.run(['git', 'init', '-q', str(clone)], check=True, env=env)
            subprocess.run(['git', '-C', str(clone), 'remote', 'add', 'origin', 'https://example.test/r.git'], check=True,
                           env=env)
            self.assertEqual(subprocess.run(['bash', str(hook), str(message)], cwd=clone, env=env, capture_output=True,
                                            check=False).returncode, 1)  # under /tmp but a real clone: rule applies
            with tempfile.TemporaryDirectory(dir=non_temp_dir()) as real:
                self.assertEqual(check(Path(real) / 'work'), 1)  # a real repository still needs Signed-off-by

    def test_fork_remotes_use_https_so_agents_can_push(self):
        repos = yaml.safe_load((ROOT / 'group_vars/all/repos.yml').read_text())
        remotes = [remote['url'] for repo in repos['git_repos_public'] for remote in repo.get('extra_remotes', [])]
        self.assertTrue(remotes)
        prefix = 'https://github.com/{{ dotfiles_github_user }}/'  # a fork is the owner's, over HTTPS
        self.assertEqual([url for url in remotes if not url.startswith(prefix)], [])

    def test_gitconfig_merge_and_diff_tool_per_os(self):
        for name, tool in [('gitconfig_linux', 'meld'), ('gitconfig_darwin', 'vimdiff')]:
            for key in ('merge.tool', 'diff.tool'):
                with self.subTest(name=name, key=key):
                    value = subprocess.run(['git', 'config', '--file', str(self.out / name), '--get', key],
                                           capture_output=True, text=True).stdout.strip()
                    self.assertEqual(value, tool)

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

    def test_macos_sshd_policy_is_key_only_and_tailnet_only(self):
        text = self.read('sshd_macos')
        for line in ('PasswordAuthentication no', 'KbdInteractiveAuthentication no', 'AuthenticationMethods publickey',
                     'PermitRootLogin no', 'PermitUserEnvironment no', 'MaxAuthTries 3', 'LoginGraceTime 30',
                     'X11Forwarding no', 'AllowAgentForwarding no', 'AllowTcpForwarding local', 'LogLevel VERBOSE',
                     'PermitEmptyPasswords no', 'HostbasedAuthentication no', 'GSSAPIAuthentication no',
                     'AuthorizedKeysFile .ssh/authorized_keys', 'MaxSessions 10', 'ClientAliveInterval 300',
                     'ClientAliveCountMax 2', 'UnusedConnectionTimeout 3600', 'TCPKeepAlive no', 'RekeyLimit 1G 1h',
                     'Compression no'):
            self.assertIn(line + '\n', text)
        self.assertNotIn('\nMaxStartups', text)  # no effect with one sshd -i per connection
        allow = next(line for line in text.splitlines() if line.startswith('AllowUsers '))
        users = allow.split()[1:]
        self.assertEqual([u.split('@', 1)[1] for u in users], ['100.64.0.0/10', 'fd7a:115c:a1e0::/48'])
        self.assertEqual(len({u.split('@', 1)[0] for u in users}), 1)
        # the algorithm lists are the Linux ones (group_vars), not a copy
        group_vars = yaml.safe_load((ROOT / 'group_vars/all/vars.yml').read_text())
        directives = dict(line.split(' ', 1) for line in text.splitlines() if line and not line.startswith('#'))
        self.assertEqual(directives['PubkeyAcceptedAlgorithms'], ','.join(group_vars['ssh_pubkey_accepted_algorithms']))
        for directive, var in [('Ciphers', 'ssh_ciphers'), ('MACs', 'ssh_macs'), ('KexAlgorithms', 'ssh_kex_algorithms'),
                               ('HostKeyAlgorithms', 'ssh_host_key_algorithms')]:
            self.assertEqual(directives[directive], group_vars[var], directive)
        # the Mac's defaults mirror the Linux ones: desktop_macos_ssh_X is "{{ system_ssh_X | default(<Linux value>) }}"
        linux = yaml.safe_load((ROOT / 'roles/system/defaults/main.yml').read_text())
        desktop = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        mirrors = {name: value for name, value in desktop.items() if name.startswith('desktop_macos_ssh_')
                   and str(value).startswith('{{ system_ssh_')}
        self.assertEqual(len(mirrors), 7, mirrors)
        for name, value in mirrors.items():
            match = re.fullmatch(r"\{\{ (system_ssh_\w+) \| default\((.*)\) \}\}", value)
            self.assertTrue(match, value)
            self.assertEqual(match.group(1), name.replace('desktop_macos_', 'system_'))
            self.assertEqual(yaml.safe_load(match.group(2)), linux[match.group(1)], name)
        sshd = Path('/usr/sbin/sshd')
        if sshd.exists():  # the drop-in parses with this host's sshd (Apple's on macOS), every algorithm included
            hostkey = self.out / 'hostkey'
            if not hostkey.exists():
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(hostkey)], check=True)
            config = self.out / 'sshd_config'
            config.write_text(f'HostKey {hostkey}\nInclude {self.out / "sshd_macos"}\n')
            result = subprocess.run([str(sshd), '-t', '-f', str(config)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # the task's validate command, sshd -t -f <drop-in alone>; it runs as root (become) with the default host
            # keys, here a readable one stands in for them
            result = subprocess.run([str(sshd), '-t', '-f', str(self.out / 'sshd_macos'), '-o', f'HostKey={hostkey}'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            bad = self.out / 'sshd_macos_bad'
            bad.write_text(text.replace('Ciphers ', 'Ciphers no-such-cipher,'))
            result = subprocess.run([str(sshd), '-t', '-f', str(bad), '-o', f'HostKey={hostkey}'],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, 'sshd -t must reject an unknown algorithm')

    def test_macos_sshd_dropin_uses_the_algorithms_the_play_kept(self):
        directives = dict(line.split(' ', 1) for line in self.read('sshd_macos_older').splitlines()
                          if line and not line.startswith('#'))
        self.assertEqual(directives['KexAlgorithms'], 'sntrup761x25519-sha512,curve25519-sha256')
        self.assertEqual(directives['Ciphers'], 'chacha20-poly1305@openssh.com')
        self.assertEqual(directives['MACs'], 'hmac-sha2-512-etm@openssh.com')
        self.assertEqual(directives['HostKeyAlgorithms'], 'ssh-ed25519')
        self.assertEqual(directives['PubkeyAcceptedAlgorithms'], 'ssh-ed25519,sk-ssh-ed25519@openssh.com')

    def test_macos_sshd_dropin_is_validated_before_it_lands(self):
        tasks = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        block = next(t for t in tasks if t.get('name') == 'Apply the sshd login policy and start sshd (macOS)')
        self.assertNotIn('rescue', block)  # nothing deletes the live drop-in
        names = [t['name'] for t in block['block']]
        deploy = block['block'][names.index('Deploy the sshd login policy drop-in (macOS)')]
        self.assertEqual(deploy['ansible.builtin.template']['validate'], '/usr/sbin/sshd -t -f %s')
        self.assertLess(names.index('Create missing SSH host keys (macOS)'), names.index(deploy['name']))

    def test_macos_sshd_job_listens_on_the_linux_ssh_port(self):
        text = self.read('sshd_launchd')
        self.assertIn('<string>722</string>', text)  # ssh_port, which the client config uses for the Mac
        self.assertIn('<string>/usr/libexec/sshd-keygen-wrapper</string>', text)
        if shutil.which('plutil'):
            result = subprocess.run(['plutil', '-lint', str(self.out / 'sshd_launchd')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('zsh'), 'zsh required')
    def test_zshenv_sets_the_managed_path_on_the_bare_ssh_path(self):
        with tempfile.TemporaryDirectory() as name:
            home, brew = Path(name) / 'home', Path(name) / 'brew'
            gnubins = ['make', 'gnu-tar', 'gawk', 'findutils', 'grep', 'gnu-sed', 'coreutils']
            for tool in gnubins:
                (brew / 'opt' / tool / 'libexec/gnubin').mkdir(parents=True)
            (brew / 'opt/python@3.14/libexec/bin').mkdir(parents=True)
            zshenv = home / '.zshenv'
            home.mkdir()
            zshenv.write_text(self.read('zshenv').replace('/opt/homebrew', str(brew)))
            bare = '/usr/bin:/bin:/usr/sbin:/sbin'
            # zsh -c with no rc file but ~/.zshenv, as ssh and mosh run remote commands
            result = subprocess.run(['zsh', '-c', 'print -r -- $PATH'], env={'PATH': bare, 'HOME': str(home)},
                                    capture_output=True, text=True)
            expected = ([f'{home}/.local/bin', f'{home}/go/bin'] + [f'{brew}/opt/{t}/libexec/gnubin' for t in gnubins]
                        + [f'{brew}/opt/python@3.14/libexec/bin', f'{brew}/bin'] + bare.split(':'))
            self.assertEqual(result.stdout.strip().split(':'), expected, result.stderr)
            # sourcing it again (nested shells, .zshrc after path_helper) moves the entries back to the front, once each
            result = subprocess.run(['zsh', '-f', '-c', 'source "$1"; PATH=/usr/bin:/bin:$PATH; _laptop_path; print -r -- $PATH',
                                     'zsh', str(zshenv)], env={'PATH': bare, 'HOME': str(home)}, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip().split(':'), expected, result.stderr)

    def test_launchd_ssh_auth_sock_names_the_ssh_agent_socket(self):
        agent = self.read('ssh_agent_plist')
        env = self.read('ssh_auth_sock_plist')
        socket = re.search(r'<string>-a</string>\s*<string>([^<]+)</string>', agent).group(1)
        self.assertTrue(socket.endswith('/.ssh/ssh-agent.socket'), socket)
        arguments = re.findall(r'<string>([^<]*)</string>', env.split('<array>')[1].split('</array>')[0])
        self.assertEqual(arguments, ['/bin/launchctl', 'setenv', 'SSH_AUTH_SOCK', socket])
        self.assertIn('<key>RunAtLoad</key>', env)
        self.assertNotIn('KeepAlive', env)  # a one-shot at login: kept alive, launchd would rerun it forever
        if shutil.which('plutil'):
            for name in ('ssh_agent_plist', 'ssh_auth_sock_plist'):
                result = subprocess.run(['plutil', '-lint', str(self.out / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_podman_machine_ensure_starts_only_a_stopped_provisioned_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / 'bin').mkdir()
            # machine inspect prints $STATE / $LASTUP (fails when $STATE is unset: no machine); start is logged
            (d / 'bin/podman').write_text(
                '#!/bin/sh\n'
                'case "$1 $2" in\n'
                '  "machine inspect") [ -n "${STATE:-}" ] || exit 125\n'
                '    case "$4" in *State*) echo "$STATE" ;; *LastUp*) echo "$LASTUP" ;; esac ;;\n'
                '  "machine start") echo "start $3" >> "$LOG" ;;\n'
                '  *) exit 2 ;;\n'
                'esac\n')
            (d / 'bin/podman').chmod(0o755)
            hold = d / 'hold'
            script = d / 'ensure'
            script.write_text(self.read('podman_ensure').replace('/HOLD-FILE-PLACEHOLDER', str(hold)))
            log = d / 'log'

            def run(**state):
                log.write_text('')
                result = subprocess.run(['sh', str(script)], capture_output=True, text=True, timeout=30,
                                        env={'PATH': f'{d}/bin:/usr/bin:/bin', 'LOG': str(log), **state})
                return result.returncode, result.stdout, log.read_text()
            booted = '2026-11-14 09:00:00.000000 +0000 UTC'
            self.assertEqual(run(STATE='running', LASTUP=booted), (0, '', ''))
            rc, out, started = run(STATE='stopped', LASTUP=booted)
            self.assertEqual((rc, started), (0, 'start podman-machine-default\n'))
            self.assertTrue(out.endswith(' starting podman-machine-default\n'), out)
            self.assertEqual(run(STATE='stopped', LASTUP='0001-01-01 00:00:00 +0000 UTC'), (0, '', ''))  # never booted
            self.assertEqual(run(STATE='stopped', LASTUP=''), (0, '', ''))
            self.assertEqual(run(), (0, '', ''))  # no machine yet
            hold.write_text('')
            self.assertEqual(run(STATE='stopped', LASTUP=booted), (0, '', ''))

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


    def test_acli_token_goes_only_over_stdin_with_no_log(self):
        tasks = yaml.safe_load((ROOT / 'roles/claude/tasks/acli.yml').read_text())

        def walk(items):
            for task in items:
                yield task
                yield from walk(task.get('block', []))
        using = [t for t in walk(tasks) if 'block' not in t and re.search(r'{{[^}]*\bacli_api_token', yaml.safe_dump(t))]
        self.assertTrue(using)
        for task in using:
            self.assertTrue(task.get('no_log'), task.get('name'))
            command = task.get('ansible.builtin.command', {})
            self.assertNotIn('acli_api_token', yaml.safe_dump(command.get('argv', [])), task.get('name'))
            self.assertIn('acli_api_token', command.get('stdin', ''), task.get('name'))
        self.assertIn("vault_acli_api_token: ''", (ROOT / 'group_vars/all/vault.yml').read_text())

    def test_global_commit_msg_hook_allows_other_repo_conventions(self):
        """The global hook runs in every repo: it must require sign-off but not this repo's style."""
        hook = ROOT / 'roles/dotfiles/files/git-template-commit-msg'
        env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null', PATH='/usr/bin:/bin')
        with tempfile.TemporaryDirectory(dir=non_temp_dir()) as directory:  # under /tmp the sign-off rule is skipped
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

def non_temp_dir():
    """A directory outside every temp location, for repositories the global hooks treat as real."""
    path = Path.home() / '.cache/laptop-setup-tests'
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def run_tasks(tasks, variables):
    """Run production tasks on localhost with fixture variables; return the json callback's task results."""
    import json
    with tempfile.TemporaryDirectory() as directory:
        playbook = Path(directory) / 'play.yml'
        playbook.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                                             'tasks': tasks}]))
        result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)], cwd=ROOT,
                                env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='json',
                                         ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                                capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    output = json.JSONDecoder().raw_decode(result.stdout)[0]  # warnings may follow the JSON
    return [t['hosts']['localhost'] for t in output['plays'][0]['tasks']]


def production_task(path, name):
    """A copy of the named task, found inside blocks too."""
    def walk(tasks):
        for task in tasks or []:
            yield task
            for key in ('block', 'rescue', 'always'):
                yield from walk(task.get(key))
    return dict(next(t for t in walk(yaml.safe_load((ROOT / path).read_text())) if t.get('name') == name))


class MacTaskTests(unittest.TestCase):
    def test_stale_mosh_server_entries_are_removed(self):
        """Older Cellar paths of mosh-server go; the current one and other programs stay."""
        task = production_task('roles/desktop/tasks/main.yml', 'Remove stale mosh-server entries from the macOS firewall')
        argv = task.pop('ansible.builtin.command')['argv']
        self.assertEqual(argv[:2], ['/usr/libexec/ApplicationFirewall/socketfilterfw', '--remove'])
        for key in ('become', 'tags', 'changed_when'):
            task.pop(key, None)
        task['ansible.builtin.debug'] = {'msg': argv[2]}
        current = '/opt/homebrew/Cellar/mosh/1.4.0_32/bin/mosh-server'
        old = '/opt/homebrew/Cellar/mosh/1.4.0_31/bin/mosh-server'
        listing = (f'ALF: total number of apps = 4\n\n1 : /Applications/Tailscale.app \n \t (Allow incoming connections)\n\n'
                   f'2 : {old} \n \t (Allow incoming connections)\n\n3 : {current} \n \t (Allow incoming connections)\n\n'
                   f'4 : /opt/homebrew/Cellar/mosh-extra/1/bin/mosh-server \n \t (Allow incoming connections)\n')
        results = run_tasks([task], {
            'is_macos': True, 'desktop_macos_firewall_allow': ['/opt/homebrew/bin/mosh-server'],
            '_desktop_fw_apps': {'stdout': listing}, '_desktop_fw_allow_paths': {'results': [{'rc': 0, 'stdout': current}]}})
        self.assertEqual([r['msg'] for r in results[0]['results']], [old])

    def test_docker_shim_needs_podman_and_no_runnable_docker(self):
        task = production_task('roles/containers/tasks/macos-docker.yml', 'Decide whether the docker shim is wanted (macOS)')
        podman = {'exists': True, 'executable': True}
        dangling = {'exists': False}  # follow: true on a link to nowhere
        for docker, podman_stat, wanted in [([dangling], podman, True), ([{'exists': True, 'executable': False}], podman, True),
                                            ([dangling, {'exists': True, 'executable': True}], podman, False),
                                            ([dangling], {'exists': False}, False),
                                            ([dangling], {'exists': True, 'executable': False}, False)]:
            with self.subTest(docker=docker, podman=podman_stat):
                results = run_tasks([task, {'ansible.builtin.debug': {'var': '_containers_docker_shim_wanted'}}], {
                    'containers_macos_docker_shim': True, 'containers_macos_machine_enabled': True,
                    '_containers_podman_bin_stat': {'stat': podman_stat},
                    '_containers_real_docker': {'results': [{'stat': stat} for stat in docker]}})
                self.assertEqual(results[1]['_containers_docker_shim_wanted'], wanted)

    def test_macos_sshd_reload_survives_its_session(self):
        """bootout ends the sessions the job started: bootout and bootstrap run as one detached async job."""
        names = ('Reload the changed sshd launchd job, detached (macOS)', 'Wait for the sshd launchd job reload (macOS)')
        tasks = [production_task('roles/desktop/tasks/main.yml', name) for name in names]
        reload_task, wait = tasks
        self.assertEqual((reload_task['async'] > 0, reload_task['poll']), (True, 0))
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            # bootstrap fails twice ("Bootstrap failed: 5" while launchd tears the old job down), then loads it
            (d / 'launchctl').write_text('#!/bin/sh\necho "$*" >> "$0.log"\n'
                                         '[ "$1" != bootstrap ] || [ "$(grep -c ^bootstrap "$0.log")" -gt 2 ] || exit 5\n')
            (d / 'launchctl').chmod(0o755)
            shell = reload_task['ansible.builtin.shell']
            shell['cmd'] = shell['cmd'].replace('/bin/launchctl', str(d / 'launchctl'))
            for task in tasks:
                task.pop('when')
            results = run_tasks(tasks, {'desktop_macos_sshd_label': 'com.example.sshd'})
            self.assertTrue(results[1]['finished'])
            self.assertEqual(results[1]['rc'], 0)
            self.assertEqual((d / 'launchctl.log').read_text().splitlines(),
                             ['bootout system/com.example.sshd'] + ['bootstrap system /Library/LaunchDaemons/com.example.sshd.plist'] * 3)

    def test_macos_sshd_keeps_only_the_algorithms_this_mac_supports(self):
        tasks = [production_task('roles/desktop/tasks/main.yml', name) for name in (
            'Keep the sshd algorithms this Mac supports (macOS)',
            'Check each sshd algorithm list keeps a supported algorithm (macOS)')]
        group_vars = yaml.safe_load((ROOT / 'group_vars/all/vars.yml').read_text())
        variables = {k: group_vars[k] for k in ('ssh_kex_algorithms', 'ssh_ciphers', 'ssh_macs', 'ssh_host_key_algorithms',
                                                'ssh_pubkey_accepted_algorithms')}
        # /usr/bin/ssh -Q of an OpenSSH before 9.9: no ML-KEM, and its own order
        supported = {'KexAlgorithms': ['ecdh-sha2-nistp521', 'curve25519-sha256', 'sntrup761x25519-sha512'],
                     'Ciphers': ['aes256-gcm@openssh.com', 'chacha20-poly1305@openssh.com'],
                     'MACs': ['hmac-sha2-256-etm@openssh.com', 'hmac-sha2-512-etm@openssh.com'],
                     'HostKeyAlgorithms': ['ssh-rsa', 'ssh-ed25519'],
                     'PubkeyAcceptedAlgorithms': ['ssh-ed25519', 'sk-ssh-ed25519@openssh.com', 'ssh-rsa']}

        def run(supported):
            return run_tasks(tasks + [{'ansible.builtin.debug': {'var': '_desktop_sshd_algorithms'}}], dict(
                variables, _desktop_ssh_supported={'results': [{'item': k, 'stdout_lines': v} for k, v in supported.items()]}))
        kept = run(supported)[-1]['_desktop_sshd_algorithms']
        self.assertEqual(kept['KexAlgorithms'], ['sntrup761x25519-sha512', 'curve25519-sha256', 'ecdh-sha2-nistp521'])
        self.assertEqual(kept['Ciphers'], variables['ssh_ciphers'].split(','))  # the group_vars order, not ssh's
        self.assertEqual(kept['HostKeyAlgorithms'], ['ssh-ed25519'])
        self.assertEqual(kept['PubkeyAcceptedAlgorithms'], ['ssh-ed25519', 'sk-ssh-ed25519@openssh.com'])
        with self.assertRaisesRegex(AssertionError, 'supports none of the configured MACs'):
            run(dict(supported, MACs=['hmac-sha1']))

    def test_macos_root_commands_are_system_paths(self):
        """sudo keeps /opt/homebrew/bin first on PATH: a bare name under become would run a user-writable binary
        (Homebrew's ssh-keygen also writes host keys to its own etc/, which Apple's sshd never reads)."""
        # tailscaled installs itself as the system daemon: running the Homebrew binary as root is the point
        allowed = {'{{ dotfiles_homebrew_prefix }}/bin/tailscaled'}

        def walk(tasks, become, macos):
            for task in tasks or []:
                task_become = task.get('become', become)
                when = str(task.get('when', ''))
                task_macos = macos or 'is_macos' in when or 'Darwin' in when
                for key in ('block', 'rescue', 'always'):
                    yield from walk(task.get(key), task_become, task_macos)
                if task_become and task_macos:
                    yield task
        files = ['roles/desktop/tasks/main.yml', 'roles/packages/tasks/main.yml', 'roles/packages/tasks/install_brew.yml',
                 'roles/containers/tasks/main.yml', 'roles/containers/tasks/macos.yml', 'roles/containers/tasks/macos-docker.yml']
        checked = []
        for path in files:
            macos_only = 'macos' in path or path.endswith('install_brew.yml')  # included only on macOS
            for task in walk(yaml.safe_load((ROOT / path).read_text()), False, macos_only):
                commands = []
                for module in ('ansible.builtin.command', 'ansible.builtin.shell'):
                    args = task.get(module)
                    if isinstance(args, dict) and 'argv' in args:
                        commands.append(args['argv'][0])
                    elif args is not None:
                        cmd = re.sub(r'{{.*?}}', 'ARG', args.get('cmd') if isinstance(args, dict) else args)
                        for part in re.split(r'&&|\|\||[;|]', cmd):
                            words = [w for w in part.split() if w not in ('until', 'while', 'if', 'then', 'do', 'done',
                                                                          'fi', '!')]
                            # shell builtins and assignments are not looked up on PATH
                            if words and not re.match(r'\w+=', words[0]) and words[0] not in ('[', 'exit', 'true', 'false'):
                                commands.append(words[0])
                for args in task.values():
                    if isinstance(args, dict) and isinstance(args.get('validate'), str):
                        commands.append(args['validate'].split()[0])
                for command in commands:
                    checked.append(command)
                    self.assertTrue(command.startswith('/') or command in allowed, f'{path}: {task.get("name")}: {command}')
        for command in ('/usr/bin/ssh-keygen', '/usr/sbin/sshd', '/bin/launchctl', '/usr/bin/pmset', '/bin/cp'):
            self.assertIn(command, checked)  # the walk reaches the remote-access, power and AeroSpace tasks


if __name__ == '__main__':
    unittest.main()
