#!/usr/bin/env python3
"""Check content of rendered configs and shipped units/hooks (real Ansible engine, no host changes)."""
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
SKETCHYBAR_BASH = '/bin/bash'  # the SketchyBar scripts' shebang: bash 3.2 on macOS, so these tests run what the bar runs
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
VARS_FILES = ['default.config.yml', 'group_vars/all/vars.yml'] + [
    f'roles/{role}/defaults/main.yml' for role in ('system', 'dotfiles', 'desktop', 'containers')]

# The macOS bar's disk list, as roles/desktop/tasks/macos_bar.yml computes it
MACOS_BAR_DISKS = next(t for t in yaml.safe_load((ROOT / 'roles/desktop/tasks/macos_bar.yml').read_text())
                       if t['name'] == 'Deploy the SketchyBar bar (macOS)')['vars']['_desktop_macos_bar_disks']

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
    'swaylock': ('roles/desktop/templates/swaylock.config.j2', {}),
    'i3': ('roles/desktop/templates/i3.config.j2', {}),
    'i3status': ('roles/desktop/templates/i3status.conf.j2', {}),
    'bar_status': ('roles/desktop/templates/i3status-bar.sh.j2', {}),
    # macOS SketchyBar (desktop_macos_bar): a second VPN that is not installed, and / as the one disk
    'sketchybarrc': ('roles/desktop/templates/sketchybarrc.j2', {
        '_desktop_macos_bar_disks': MACOS_BAR_DISKS, 'desktop_i3status_disks': ['/', '/tmp', '/Volumes/My Disk/'],
        'desktop_i3status_vpns_extra': [{'name': 'Mullvad', 'command': ['mullvad-missing', 'status'], 'connected': '^Connected'}]}),
    'sketchybar_status': ('roles/desktop/templates/sketchybar-status.sh.j2', {
        '_desktop_macos_bar_disks': MACOS_BAR_DISKS, 'desktop_i3status_disks': ['/', '/tmp', '/Volumes/My Disk/'],
        'desktop_i3status_vpns_extra': [{'name': 'Mullvad', 'command': ['mullvad-missing', 'status'], 'connected': '^Connected'}]}),
    # an accented volume typed in NFC (macOS stores and df prints it in NFD), and a name with a control character
    'sketchybar_status_unicode': ('roles/desktop/templates/sketchybar-status.sh.j2', {
        '_desktop_macos_bar_disks': MACOS_BAR_DISKS,
        'desktop_i3status_disks': ['/', '/Volumes/Données/', '/Volumes/Bell\x07Disk', '/Volumes/Plain',
                                   '/Volumes/Caf\u00e9 \U0001f389']}),
    'sketchybar_workspaces': ('roles/desktop/templates/sketchybar-workspaces.sh.j2', {}),
    # AeroSpace with the bar on, before and after the packages chunk built SketchyBar
    'aerospace_bar': ('roles/desktop/templates/aerospace.toml.j2', {
        'desktop_environment': 'aerospace', '_desktop_sketchybar_bin': {'stat': {'executable': True}}}),
    'aerospace_bar_unbuilt': ('roles/desktop/templates/aerospace.toml.j2', {
        'desktop_environment': 'aerospace', '_desktop_sketchybar_bin': {'stat': {'exists': False}}}),
    'audit': ('roles/system/templates/auditd-system.rules.j2', {}),
    'sshd_macos': ('roles/desktop/templates/sshd-macos.conf.j2', {}),
    # what the play keeps on a Mac whose OpenSSH predates ML-KEM
    'sshd_macos_older': ('roles/desktop/templates/sshd-macos.conf.j2', {'_desktop_sshd_algorithms': {
        'KexAlgorithms': ['sntrup761x25519-sha512', 'curve25519-sha256'], 'Ciphers': ['chacha20-poly1305@openssh.com'],
        'MACs': ['hmac-sha2-512-etm@openssh.com'], 'HostKeyAlgorithms': ['ssh-ed25519'],
        'PubkeyAcceptedAlgorithms': ['ssh-ed25519', 'sk-ssh-ed25519@openssh.com']}}),
    'sshd_launchd': ('roles/desktop/templates/sshd-launchd.plist.j2', {}),
    'zshenv': ('roles/dotfiles/templates/zshenv.j2', {'dotfiles_homebrew_prefix': '/opt/homebrew'}),
    'zshenv_machine': ('roles/dotfiles/templates/zshenv.j2', {'dotfiles_homebrew_prefix': '/opt/homebrew',
                                                           'containers_macos_machine_name': "dev vm's"}),
    # podman from PATH (a stub); the hold file path is replaced per test
    'podman_ensure': ('roles/containers/templates/podman-machine-ensure.sh.j2', {
        'containers_podman_bin': 'podman', 'containers_macos_machine_hold_file': '/HOLD-FILE-PLACEHOLDER',
        'containers_macos_machine_trim_stamp': '/TRIM-STAMP-PLACEHOLDER'}),
    'podman_ensure_prune': ('roles/containers/templates/podman-machine-ensure.sh.j2', {
        'containers_podman_bin': 'podman', 'containers_macos_machine_hold_file': '/HOLD-FILE-PLACEHOLDER',
        'containers_macos_machine_trim_stamp': '/TRIM-STAMP-PLACEHOLDER', 'containers_prune_enabled': True,
        'containers_macos_machine_prune_stamp': '/PRUNE-STAMP-PLACEHOLDER'}),
    'ssh_agent_plist': ('roles/dotfiles/templates/ssh-agent.plist.j2', {'dotfiles_homebrew_prefix': '/opt/homebrew'}),
    'ssh_auth_sock_plist': ('roles/dotfiles/templates/ssh-auth-sock.plist.j2', {}),
    'ssh_agent_unit': ('roles/dotfiles/templates/ssh-agent.service.j2', {}),
}
SSHD_ALGORITHM_VARS = {'KexAlgorithms': 'ssh_kex_algorithms', 'Ciphers': 'ssh_ciphers', 'MACs': 'ssh_macs',
                       'HostKeyAlgorithms': 'ssh_host_key_algorithms', 'PubkeyAcceptedAlgorithms': 'ssh_pubkey_accepted_algorithms'}


def host_sshd_algorithms():
    """The group_vars algorithm lists kept to what this host's ssh -Q lists, as the play does on a Mac; None without an ssh
    that answers or when a list would be empty (sshd -t of the full lists fails on an older OpenSSH, e.g. Ubuntu CI's)."""
    ssh = '/usr/bin/ssh' if Path('/usr/bin/ssh').exists() else shutil.which('ssh')
    if not ssh:
        return None
    group_vars = yaml.safe_load((ROOT / 'group_vars/all/vars.yml').read_text())
    kept = {}
    for kind, var in SSHD_ALGORITHM_VARS.items():
        result = subprocess.run([ssh, '-Q', kind], capture_output=True, text=True)
        wanted = group_vars[var] if isinstance(group_vars[var], list) else group_vars[var].split(',')
        kept[kind] = [a.strip() for a in wanted if a.strip() in result.stdout.split()]
        if result.returncode or not kept[kind]:
            return None
    return kept


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
        cls.host_algorithms = host_sshd_algorithms()
        cases = dict(CASES)
        if cls.host_algorithms:  # the drop-in this host's sshd -t can parse
            cases['sshd_macos_host'] = ('roles/desktop/templates/sshd-macos.conf.j2',
                                        {'_desktop_sshd_algorithms': cls.host_algorithms})
        for name, (src, extra) in cases.items():
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

    def test_wm_configs_end_by_including_the_users_config_local(self):
        """Hand edits live in config.local, which the managed config includes last so they can add to it."""
        for wm in ('i3', 'sway'):
            with self.subTest(wm=wm):
                lines = [line for line in self.read(wm).splitlines() if line.strip()]
                self.assertEqual(lines[-1], f'include ~/.config/{wm}/config.local')
                self.assertEqual(sum(line.startswith('include ') for line in lines), 1)

    @unittest.skipUnless(shutil.which('i3'), 'i3 not installed')
    def test_rendered_i3_config_validates_with_and_without_config_local(self):
        """Runs where i3 is installed (developer hosts; the CI runner has none and skips it)."""
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            config = Path(home) / 'config'
            config.write_text(self.read('i3'))
            for local in (None, 'bindsym Mod4+F12 exec true\n', 'not a command\n'):
                with self.subTest(local=local):
                    path = Path(home) / '.config/i3/config.local'
                    path.unlink(missing_ok=True)
                    if local is not None:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(local)
                    result = subprocess.run(['i3', '-C', '-c', str(config)], env=env, capture_output=True, text=True,
                                            timeout=30)
                    # A bad config.local must fail: proof the include is read, not silently skipped
                    self.assertEqual(result.returncode, 1 if local == 'not a command\n' else 0,
                                     result.stdout + result.stderr)

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

    def test_global_hooks_do_not_loop_with_a_repo_that_calls_them(self):
        """A repo with its own core.hooksPath whose hooks call the global hooks (Health) runs each hook once: relative and
        absolute hooksPath forms, a commit and a real push, and a bare repository's push."""
        hooks_names = ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'pre-push')
        with tempfile.TemporaryDirectory(dir=non_temp_dir()) as name:
            home = Path(name)
            hooks = home / 'global'
            hooks.mkdir()
            for hook in hooks_names:
                shutil.copy(ROOT / f'roles/dotfiles/files/git-template-{hook}', hooks / hook)
                (hooks / hook).chmod(0o755)
            (home / 'gitconfig').write_text(f'[core]\n\thooksPath = {hooks}\n[user]\n\tname = t\n\temail = t@t\n')
            env = dict(os.environ, GIT_CONFIG_GLOBAL=str(home / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1', HOME=name,
                       PATH=f"/nonexistent:{os.environ['PATH']}")
            env.pop('TMPDIR', None)

            def git(*args, cwd=None):
                return subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
                                      check=False)

            def repo_hooks(directory, log):
                """Hooks that log, then call the global hook; capped at 5 runs, so a regression ends instead of looping."""
                directory.mkdir(parents=True, exist_ok=True)
                for hook in hooks_names:
                    own = directory / hook
                    own.write_text(f'#!/bin/sh\necho {hook} >> {log}\n'
                                   f'[ "$(grep -c . {log})" -lt 5 ] || exit 0\nexec "{hooks}/{hook}" "$@"\n')
                    own.chmod(0o755)

            git('init', '-q', '--bare', str(home / 'remote.git'))
            for form in ('relative', 'absolute'):
                repo, log = home / form, home / f'{form}.log'
                git('init', '-q', str(repo))
                path = '.git/hooks' if form == 'relative' else str(repo / '.git/hooks')
                git('-C', str(repo), 'config', 'core.hooksPath', path)
                repo_hooks(repo / '.git/hooks', log)
                committed = git('-C', str(repo), 'commit', '-q', '-s', '--allow-empty', '-m', 'Test: x')
                self.assertEqual(committed.returncode, 0, committed.stderr)
                pushed = git('-C', str(repo), 'push', '-q', str(home / 'remote.git'), f'HEAD:{form}')
                self.assertEqual(pushed.returncode, 0, pushed.stderr)
                self.assertEqual(sorted(log.read_text().split()), sorted(hooks_names), form)
            # A bare repository with a relative hooksPath: git resolves it against the git directory.
            bare, log = home / 'bare.git', home / 'bare.log'
            git('clone', '-q', '--bare', str(home / 'relative'), str(bare))
            git('-C', str(bare), 'config', 'core.hooksPath', 'hooks')
            repo_hooks(bare / 'hooks', log)
            pushed = git('-C', str(bare), 'push', '-q', str(home / 'remote.git'), 'HEAD:bare')
            self.assertEqual(pushed.returncode, 0, pushed.stderr)
            self.assertEqual(log.read_text().split(), ['pre-push'])

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

    @unittest.skipUnless(os.uname().sysname == 'Linux', 'the sway bar script is Linux-only: /proc, bash 4.3')
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

    def run_sketchybar_script(self, name, stubs, env=None, stderr=''):
        """Run a rendered SketchyBar script with stub commands first on PATH; returns {item: {key: value}} from the
        stub sketchybar's --set arguments, or None when it never called sketchybar. stderr: a regex it must match."""
        stub_dir = Path(stubs['__dir'])
        for command, body in stubs.items():
            if command != '__dir':
                (stub_dir / command).write_text('#!/bin/sh\n' + body + '\n')
                (stub_dir / command).chmod(0o755)
        out = stub_dir / 'sketchybar.args'
        (stub_dir / 'sketchybar').write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > {out}\n')
        (stub_dir / 'sketchybar').chmod(0o755)
        script = stub_dir / name
        script.write_text(self.read(name))
        result = subprocess.run([SKETCHYBAR_BASH, str(script)], capture_output=True, text=True, timeout=30,
                                env={**os.environ, 'PATH': f'{stub_dir}:/usr/bin:/bin', 'HOME': str(stub_dir),
                                     'SKETCHYBAR': str(stub_dir / 'sketchybar'), **(env or {})})
        if stderr:
            self.assertRegex(result.stderr, stderr)
        else:
            self.assertEqual(result.stderr, '')
        if not out.exists():
            return None
        items, current = {}, None
        args = out.read_text().splitlines()
        out.unlink()
        for i, arg in enumerate(args):
            if arg == '--set':
                current = items.setdefault(args[i + 1], {})
            elif current is not None and '=' in arg:
                key, value = arg.split('=', 1)
                current[key] = value
        return items

    def test_sketchybar_status_shows_the_i3status_fields_from_macos_tools(self):
        with tempfile.TemporaryDirectory() as name:
            stubs = {'__dir': name,
                     'iostat': 'printf "      cpu    load average\\n us sy id   1m   5m   15m\\n'
                               '  5  5 90  1.0 1.0 1.0\\n 50 40 10  1.0 1.0 1.0\\n"',
                     'sysctl': 'case "$2" in vm.loadavg) echo "{ 9.50 2.00 3.00 }";; hw.memsize) echo 17179869184;;'
                               ' kern.memorystatus_vm_pressure_level) echo 4;; esac',
                     'vm_stat': 'printf "Mach Virtual Memory Statistics: (page size of 16384 bytes)\\n'
                                'Pages wired down:  100000.\\nPages purgeable: 0.\\nAnonymous pages: 300000.\\n'
                                'Pages occupied by compressor: 100000.\\n"',
                     'df': 'printf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n"; case "$2" in'
                           ' "/Volumes/My Disk") printf "//me@nas/My Share 1000000 950000 50000 95%% /Volumes/My Disk\\n";;'
                           ' *) printf "/dev/d 1000000 500000 500000 50%% /\\n";; esac',
                     'pmset': 'printf "Now drawing from \'AC Power\'\\n -InternalBattery-0 (id=1)\\t85%%; charging; 1:30 remaining present: true\\n"',
                     'osascript': 'echo "40 true"',
                     'ps': 'printf "ttys001 /opt/bin/claude\\n?? /opt/bin/codex\\nttys001 claude\\nttys002 codex\\nttys003 zsh\\n"',
                     'podman': '[ "$1" = ps ] && printf "a1\\nb2\\n"',
                     'tailscale': 'echo \'"BackendState": "Running"\'',
                     'scutil': 'echo \'* (Connected)   ABC PPP --> L2TP "Work VPN" [PPP:L2TP]\'',
                     'route': 'echo "  interface: en0"',
                     'ipconfig': 'echo 10.0.0.5',
                     'networksetup': 'printf "Hardware Port: Wi-Fi\\nDevice: en0\\n"',
                     'netstat': 'printf "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\\n'
                                'en0 1500 <Link#4> aa:bb 1 0 $RX 1 0 2048 0\\n"'}
            first = self.run_sketchybar_script('sketchybar_status', stubs, {'RX': '1000'})
            red = '0xff' + yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())[
                'desktop_alacritty_color_normal_red'].lstrip('#').lower()
            labels = {item: v.get('label') for item, v in first.items()}
            self.assertEqual(labels['cpu'], 'CPU 90%')
            self.assertEqual(first['cpu']['label.color'], red)  # above 80%
            self.assertEqual(labels['load'], 'LOAD 9.50 2.00 3.00')
            self.assertEqual(first['load']['label.color'], red)  # 1 min load above the CPU count
            self.assertEqual(labels['ram'], 'RAM 7.6/16.0')  # (300000 + 100000 + 100000) pages of 16 KiB
            self.assertEqual((labels['pressure'], first['pressure']['label.color']), ('PRESSURE critical', red))
            self.assertEqual((labels['disk.0'], first['disk.0']['label.color']), ('/ 488.3M', first['ram']['label.color']))
            self.assertEqual(first['disk.1'], {'drawing': 'off'})  # /tmp is not a mount point on macOS
            self.assertEqual((labels['disk.2'], first['disk.2']['label.color']), ('/Volumes/My Disk 48.8M', red))  # 5% free
            self.assertEqual(labels['bat'], 'BAT ⚡85% 1:30')
            self.assertEqual(labels['vol'], 'VOL muted')
            self.assertEqual(labels['ctr'], 'CTR 2')
            self.assertEqual(labels['ai'], 'AI 2')  # ttys001 counted once, ?? (no terminal) and zsh not at all
            self.assertEqual(labels['vpn.0'], 'Tailscale up')
            self.assertEqual(labels['vpn.1'], 'Mullvad missing')
            self.assertEqual(labels['vpn.nc'], 'VPN Work VPN')
            self.assertEqual(labels['net'], 'WIFI 10.0.0.5')  # no rate before a previous sample
            self.assertRegex(labels['time'], r'^\w{3} \d\d \w{3} \d\d:\d\d$')
            self.assertRegex(labels['tz.0'], r'^IN \w{3} \d\d:\d\d$')
            time.sleep(1.1)
            second = self.run_sketchybar_script('sketchybar_status', stubs, {'RX': str(1000 + 3 * 2**20)})
            self.assertRegex(second['net']['label'], r'^WIFI 10\.0\.0\.5 ↓[0-9.]+M ↑0K$')
            # No battery, no podman machine, no network: those fields hide
            stubs.update({'pmset': 'echo "Now drawing from \'AC Power\'"', 'podman': 'exit 125', 'route': 'exit 1',
                          'ipconfig': 'exit 1'})
            for cache in Path(name, '.cache/sketchybar').iterdir():
                cache.unlink()
            third = self.run_sketchybar_script('sketchybar_status', stubs, {'RX': '0'})
            for item in ('bat', 'ctr', 'net'):
                self.assertEqual(third[item], {'drawing': 'off'}, item)

    def test_sketchybar_status_shows_accented_volumes(self):
        """/Volumes/Données shows though config.yml has it in NFC and df prints the volume's NFD name; a control
        character is dropped from a name; an ASCII name that is not a mount point still hides."""
        self.assertNotIn('\x07', self.read('sketchybar_status_unicode'))
        quiet = {c: 'exit 1' for c in ('iostat', 'sysctl', 'vm_stat', 'pmset', 'osascript', 'ps', 'podman', 'scutil',
                                       'route', 'ipconfig', 'networksetup', 'netstat')}
        # The NFD mount points df prints, each quoted for the stub's shell
        nfd = '"$(printf \'/Volumes/Donne\\314\\201es\')"'
        party = '"$(printf \'/Volumes/Cafe\\314\\201 \\360\\237\\216\\211\')"'  # Apple's iconv cannot convert the emoji
        with tempfile.TemporaryDirectory() as name:
            items = self.run_sketchybar_script('sketchybar_status_unicode', {
                '__dir': name, **quiet,
                'df': 'printf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n"; case "$2" in'
                      f' "$(printf \'/Volumes/Donn\\303\\251es\')"|{nfd}) printf "/dev/d2 1000000 500000 500000 50%% %s\\n" {nfd};;'
                      ' /Volumes/BellDisk) printf "/dev/d3 1000000 500000 500000 50%% /Volumes/BellDisk\\n";;'
                      f' "$(printf \'/Volumes/Caf\\303\\251 \\360\\237\\216\\211\')"|{party})'
                      f' printf "/dev/d4 1000000 500000 500000 50%% %s\\n" {party};;'
                      ' *) printf "/dev/d 1000000 500000 500000 50%% /\\n";; esac'})
        self.assertEqual(items['disk.0']['label'], '/ 488.3M')
        self.assertEqual(items['disk.1']['label'], '/Volumes/Données 488.3M')  # as typed, the trailing slash dropped
        self.assertEqual(items['disk.2']['label'], '/Volumes/BellDisk 488.3M')
        self.assertEqual(items['disk.3'], {'drawing': 'off'})  # /Volumes/Plain: df reports /, not a mount point
        self.assertEqual(items['disk.4']['label'], '/Volumes/Café \U0001f389 488.3M')

    def test_sketchybar_status_edge_cases(self):
        quiet = {c: 'exit 1' for c in ('iostat', 'sysctl', 'vm_stat', 'df', 'pmset', 'osascript', 'ps', 'podman', 'scutil',
                                       'route', 'ipconfig', 'networksetup', 'netstat')}
        with tempfile.TemporaryDirectory() as name:
            # A volume key: only VOL, without the slow full run
            items = self.run_sketchybar_script('sketchybar_status', {'__dir': name, **quiet, 'osascript': 'echo "55 false"',
                                                                     'iostat': 'sleep 5'}, {'SENDER': 'volume_change'})
            self.assertEqual(items, {'vol': {'label': 'VOL 55%', 'label.color': items['vol']['label.color'], 'drawing': 'on'}})
        with tempfile.TemporaryDirectory() as name:
            # A hung VPN CLI that ignores SIGALRM (as Go programs do), started by a wrapper that leaves a child holding
            # the output pipe, is killed with its children after 2 s and counts as down. A podman killed by a signal
            # is a failure: no CTR field, not "CTR 0". A df hung on a network mount is cut off too: that disk hides.
            start = time.monotonic()
            hang = 'exec perl -e \'$SIG{ALRM} = "IGNORE"; sleep 20\''
            # survives the group kill, as a df stuck in the kernel on a hung mount does, and keeps stdout open
            survivor = 'perl -e \'setpgrp; sleep 20\' &\n'
            items = self.run_sketchybar_script('sketchybar_status', {
                '__dir': name, **quiet, 'podman': 'kill -SEGV $$', 'df': survivor + hang,
                'tailscale': 'sleep 20 &\n' + hang})
            self.assertLess(time.monotonic() - start, 15)  # three disks and the VPN, 2 s each at most
            self.assertEqual(items['vpn.0']['label'], 'Tailscale down')
            self.assertEqual(items['ctr'], {'drawing': 'off'})
            self.assertEqual(items['disk.0'], {'drawing': 'off'})
        with tempfile.TemporaryDirectory() as name:
            # A VPN tunnel holds the default route: the Wi-Fi link shows, its counters read without an Address column
            stubs = {'__dir': name, **quiet, 'route': 'echo "  interface: utun4"',
                     'ipconfig': '[ "$2" = en0 ] && echo 10.0.0.5',
                     'networksetup': 'printf "Hardware Port: Wi-Fi\\nDevice: en0\\n"',
                     'netstat': 'printf "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\\n'
                                'en0 1500 <Link#4> 1 0 $RX 1 0 2048 0\\n"',
                     'pmset': 'printf " -InternalBattery-0 (id=1)\\t100%%; charged; 0:00 remaining present: true\\n"'}
            items = self.run_sketchybar_script('sketchybar_status', stubs, {'RX': '1000'})
            self.assertEqual(items['net']['label'], 'WIFI 10.0.0.5')
            self.assertEqual(items['bat']['label'], 'BAT 100%')  # full: no time left, as on Linux
            time.sleep(1.1)
            stubs['pmset'] = 'printf " -InternalBattery-0 (id=1)\\t80%%; AC attached; not charging present: true\\n"'
            items = self.run_sketchybar_script('sketchybar_status', stubs, {'RX': str(1000 + 2**20)})
            self.assertRegex(items['net']['label'], r'^WIFI 10\.0\.0\.5 ↓(1\.0M|512K) ↑0K$')  # 1 MiB over 1 or 2 whole s
            self.assertEqual(items['bat']['label'], 'BAT 80%')  # held below full: not charging, no ⚡
        with tempfile.TemporaryDirectory() as name:
            # Caches from an older config (a longer VPN list) or a torn write: skipped, never an abort of the update
            cache = Path(name, '.cache/sketchybar')
            cache.mkdir(parents=True)
            (cache / 'vpn').write_text('0 up\n7 up\n-1 up\nx\n\n')
            (cache / 'net').write_text('abc en0 x y\n')
            items = self.run_sketchybar_script('sketchybar_status', {
                '__dir': name, **quiet, 'route': 'echo "  interface: en0"', 'ipconfig': 'echo 10.0.0.5',
                'networksetup': 'printf "Hardware Port: Wi-Fi\\nDevice: en0\\n"',
                'netstat': 'printf "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\\n'
                           'en0 1500 <Link#4> aa:bb 1 0 1000 1 0 2048 0\\n"'})
            self.assertEqual(items['vpn.0']['label'], 'Tailscale up')
            self.assertNotIn('vpn.7', items)
            self.assertEqual(items['net']['label'], 'WIFI 10.0.0.5')
        with tempfile.TemporaryDirectory() as name:
            # Volume and network-extension probes that hang are cut off like the others; VOL stays as it was
            start = time.monotonic()
            hang = 'exec perl -e \'$SIG{ALRM} = "IGNORE"; sleep 20\''
            items = self.run_sketchybar_script('sketchybar_status', {'__dir': name, **quiet, 'osascript': hang,
                                                                     'scutil': hang})
            self.assertLess(time.monotonic() - start, 12)
            self.assertNotIn('vol', items)
            # The same on a volume key: no update at all, and no error
            self.assertIsNone(self.run_sketchybar_script('sketchybar_status', {'__dir': name, **quiet, 'osascript': hang},
                                                         {'SENDER': 'volume_change'}))
        with tempfile.TemporaryDirectory() as name:
            # No writable cache (a read-only or full home): the probes still show, only uncached
            if os.geteuid() == 0:
                self.skipTest('root writes to a read-only home')
            home = Path(name, 'home')
            home.mkdir(mode=0o500)
            try:
                items = self.run_sketchybar_script('sketchybar_status', {
                    '__dir': name, **quiet, 'podman': '[ "$1" = ps ] && printf "a1\\n"',
                    'tailscale': 'echo \'"BackendState": "Running"\''}, {'HOME': str(home)},
                    stderr='Permission denied')
            finally:
                home.chmod(0o700)
            self.assertEqual(items['ctr']['label'], 'CTR 1')
            self.assertEqual(items['vpn.0']['label'], 'Tailscale up')

    def test_sketchybar_status_killed_mid_probe_leaves_no_process(self):
        # The bar stopping a run (TERM to its process group) must not leave a limited command running past its bound
        quiet = {c: 'exit 1' for c in ('iostat', 'sysctl', 'vm_stat', 'df', 'pmset', 'osascript', 'ps', 'scutil',
                                       'route', 'ipconfig', 'networksetup', 'netstat')}
        with tempfile.TemporaryDirectory() as name:
            stub_dir = Path(name)
            started = stub_dir / 'podman.started'
            marker = f'podman-stub-{os.getpid()}-{stub_dir.name}'  # names only this test's process
            for command, body in {**quiet, 'podman': f'touch {started}\nexec perl -e \'sleep 20\' {marker}'}.items():
                (stub_dir / command).write_text('#!/bin/sh\n' + body + '\n')
                (stub_dir / command).chmod(0o755)
            (stub_dir / 'sketchybar').write_text('#!/bin/sh\n')
            (stub_dir / 'sketchybar').chmod(0o755)
            script = stub_dir / 'status.sh'
            script.write_text(self.read('sketchybar_status'))
            process = subprocess.Popen([SKETCHYBAR_BASH, str(script)], start_new_session=True, stderr=subprocess.DEVNULL,
                                       env=dict(os.environ, PATH=f'{stub_dir}:/usr/bin:/bin', HOME=name,
                                                SKETCHYBAR=str(stub_dir / 'sketchybar')))
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
            time.sleep(0.5)
            left = subprocess.run(['pgrep', '-f', marker], capture_output=True, text=True).stdout.split()
            for pid in left:
                os.kill(int(pid), signal.SIGKILL)
            self.assertEqual(left, [], 'the limited podman outlived the killed run')

    def test_sketchybar_workspaces_follow_aerospace_like_i3bar_buttons(self):
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        color = {k: '0xff' + defaults[f'desktop_bar_{k}'].lstrip('#').lower()
                 for k in ('focused_ws_bg', 'active_ws_bg', 'inactive_ws_bg')}
        with tempfile.TemporaryDirectory() as name:
            stubs = {'__dir': name, 'aerospace': 'case "$*" in "list-workspaces --focused") echo 2;;'
                     ' "list-workspaces --all") printf "1\\n2\\n3\\nS\\n";;'
                     ' "list-workspaces --monitor all --visible") printf "2\\n3\\n";;'
                     ' "list-modes --current") echo "$MODE";; esac'}
            items = self.run_sketchybar_script('sketchybar_workspaces', stubs,
                                               {'AEROSPACE': str(Path(name) / 'aerospace'), 'MODE': 'resize'})
            self.assertEqual(items['ws.2']['background.color'], color['focused_ws_bg'])
            self.assertEqual(items['ws.3']['background.color'], color['active_ws_bg'])  # shown on another monitor
            for ws in ('ws.1', 'ws.S'):
                self.assertEqual(items[ws]['background.color'], color['inactive_ws_bg'])
            self.assertEqual(items['ws.4'], {'drawing': 'off'})  # no windows, not shown: gone, as in i3
            self.assertEqual(items['mode'], {'drawing': 'on', 'label': 'resize'})
            # A stale FOCUSED_WORKSPACE from an older event never wins over the live query; main mode hides
            items = self.run_sketchybar_script('sketchybar_workspaces', stubs, {
                'AEROSPACE': str(Path(name) / 'aerospace'), 'MODE': 'main', 'FOCUSED_WORKSPACE': 'S'})
            self.assertEqual(items['ws.2']['background.color'], color['focused_ws_bg'])
            self.assertEqual(items['ws.S']['background.color'], color['inactive_ws_bg'])
            self.assertEqual(items['mode'], {'drawing': 'off'})
            # A hung AeroSpace query, the first or a later one, is cut off after 2 s and leaves the buttons as they are
            hang = 'exec perl -e \'$SIG{ALRM} = "IGNORE"; sleep 20\''
            for hung in ('list-workspaces --focused', 'list-workspaces --all', 'list-modes --current'):
                start = time.monotonic()
                stubs['aerospace'] = f'[ "$*" = "{hung}" ] && {hang}\necho 1'
                self.assertIsNone(self.run_sketchybar_script('sketchybar_workspaces', stubs,
                                                             {'AEROSPACE': str(Path(name) / 'aerospace')}), hung)
                self.assertLess(time.monotonic() - start, 6)
            # No AeroSpace at all (not running, or not installed): every button hides, as before
            stubs['aerospace'] = 'exit 1'
            items = self.run_sketchybar_script('sketchybar_workspaces', stubs, {'AEROSPACE': str(Path(name) / 'aerospace')})
            self.assertEqual({items[f'ws.{ws}']['drawing'] for ws in (*range(1, 11), 'S')}, {'off'})

    def test_aerospace_keeps_room_for_the_bar_only_once_it_is_built(self):
        import tomllib
        built, unbuilt = (tomllib.loads(self.read(n)) for n in ('aerospace_bar', 'aerospace_bar_unbuilt'))
        self.assertEqual(built['gaps']['outer']['top'], 30)
        self.assertRegex(built['on-mode-changed'][0], r'^exec-and-forget /\S+/\.local/bin/sketchybar --trigger aerospace_mode_change$')
        self.assertEqual(built['exec-on-workspace-change'][1:], ['--trigger', 'aerospace_workspace_change'])
        self.assertEqual(unbuilt['gaps']['outer']['top'], 0)  # no empty strip while the menu bar still shows
        self.assertNotIn('exec-on-workspace-change', unbuilt)

    def test_sketchybarrc_adds_status_fields_in_the_i3status_order(self):
        with tempfile.TemporaryDirectory() as name:
            log = Path(name) / 'calls'
            rc = re.sub(r'^sketchybar=.*$', f'sketchybar={name}/sketchybar', self.read('sketchybarrc'), flags=re.M)
            (Path(name) / 'sketchybar').write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
            (Path(name) / 'sketchybar').chmod(0o755)
            (Path(name) / 'rc').write_text(rc)
            result = subprocess.run(['bash', str(Path(name) / 'rc')], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            right = re.findall(r'--add item (\S+) right', log.read_text())
            # added right to left, so reversed they read left to right as on Linux
            self.assertEqual(right[::-1], ['status', 'cpu', 'load', 'ram', 'pressure', 'disk.0', 'disk.1', 'disk.2',
                                           'bat', 'vol', 'time',
                                           'tz.0', 'tz.1', 'ctr', 'ai', 'vpn.0', 'vpn.1', 'vpn.nc', 'net'])
            self.assertEqual(re.findall(r'--add item (ws\.\S+) left', log.read_text()),
                             [f'ws.{w}' for w in [*range(1, 11), 'S']])

    def test_sway_lock_and_idle_policy(self):
        sway = self.read('sway')
        for line in ('for_window [app_id=".*"] inhibit_idle fullscreen', 'for_window [class=".*"] inhibit_idle fullscreen'):
            self.assertIn(line + '\n', sway)
        self.assertIn('ignore-empty-password\n', self.read('swaylock'))
        if shutil.which('sway'):
            # sway 1.9 (Ubuntu 24.04) opens a seat even for -C; the headless backend needs none.
            with tempfile.TemporaryDirectory() as runtime:
                result = subprocess.run(['sway', '-C', '-c', str(self.out / 'sway')], capture_output=True, text=True,
                                        timeout=30, env=dict(os.environ, XDG_RUNTIME_DIR=runtime,
                                                             WLR_BACKENDS='headless', WLR_RENDERER='pixman'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.uname().sysname == 'Linux', 'the sway bar script is Linux-only: /proc, bash 4.3')
    def test_bar_shows_the_microphone_state_after_volume(self):
        for wpctl, expect in [('Volume: 0.40 [MUTED]', '"name":"mic","full_text":"MIC muted"'),
                              ('Volume: 0.40', '"name":"mic","full_text":"MIC on","color":"#FFFF00"'),
                              (None, None)]:
            with tempfile.TemporaryDirectory() as name:
                stubs = Path(name)

                def stub(command, body):
                    (stubs / command).write_text('#!/bin/sh\n' + body + '\n')
                    (stubs / command).chmod(0o755)
                stub('i3status', 'printf \'{"version":1}\\n[\\n[{"name":"volume","full_text":"VOL 40%%"},'
                                 '{"name":"wireless","full_text":"WIFI"}]\\n\'')
                stub('wpctl', f'echo "{wpctl}"' if wpctl else 'exit 1')  # exit 1: no input device
                stub('timeout', 'shift; exec "$@"')  # GNU timeout is not in macOS /usr/bin
                for quiet in ['nmcli', 'tailscale', 'rpm', 'podman', 'ps']:
                    stub(quiet, 'exit 1')
                script = stubs / 'bar-status'
                script.write_text(self.read('bar_status'))
                result = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=30,
                                        env=dict(os.environ, PATH=f'{stubs}:/usr/bin:/bin', HOME=name))
                update = result.stdout.splitlines()[-1]
                if expect:
                    self.assertIn('"full_text":"VOL 40%"},{' + expect, update)
                else:
                    self.assertNotIn('"name":"mic"', update)
                json.loads(update)  # still a valid i3bar array

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

    @unittest.skipUnless(Path('/usr/sbin/sshd').exists(), 'no /usr/sbin/sshd')
    def test_macos_sshd_dropin_parses_with_this_hosts_sshd(self):
        """sshd -t on the drop-in with the algorithms this host's OpenSSH supports, as the play keeps them on a Mac."""
        if not self.host_algorithms:
            self.skipTest("this host's ssh -Q supports none of a configured algorithm list")
        sshd = Path('/usr/sbin/sshd')
        text = self.read('sshd_macos_host')
        hostkey = self.out / 'hostkey'
        if not hostkey.exists():
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(hostkey)], check=True)
        config = self.out / 'sshd_config'
        config.write_text(f'HostKey {hostkey}\nInclude {self.out / "sshd_macos_host"}\n')
        result = subprocess.run([str(sshd), '-t', '-f', str(config)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # the task's validate command, sshd -t -f <drop-in alone>; it runs as root (become) with the default host
        # keys, here a readable one stands in for them
        result = subprocess.run([str(sshd), '-t', '-f', str(self.out / 'sshd_macos_host'), '-o', f'HostKey={hostkey}'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        bad = self.out / 'sshd_macos_bad'
        bad.write_text(text.replace('Ciphers ', 'Ciphers no-such-cipher,'))
        result = subprocess.run([str(sshd), '-t', '-f', str(bad), '-o', f'HostKey={hostkey}'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, 'sshd -t must reject an unknown algorithm')
        # the play's sshd -T check on this sshd's output: our drop-in first passes; a drop-in sorting before it, a
        # sshd_config without the Include, or a Match block for addresses outside the tailnet does not
        dropins = self.out / 'sshd_config.d'
        dropins.mkdir(exist_ok=True)
        shutil.copy(self.out / 'sshd_macos_host', dropins / '000-laptop-setup.conf')
        effective = self.out / 'sshd_effective'
        user = getpass.getuser()  # the drop-in was rendered with this host's ansible_facts['user_id']
        match = 'Match Address 203.0.113.0/24\n    PasswordAuthentication yes\n    AllowUsers *\n'
        for name, earlier, include, tail, error in [
                ('ours first', None, True, '', None),
                ('earlier drop-in', 'PasswordAuthentication yes\n', True, '', 'shows .*passwordauthentication yes'),
                ('earlier AllowUsers', 'AllowUsers nobody\n', True, '', r'allowusers .*nobody@?'),
                ('no Include', None, False, '', 'shows .*passwordauthentication yes'),
                ('Match outside the tailnet', None, True, match, r'from 203\.0\.113\.5 .*shows passwordauthentication yes,'
                                                                  r'.* and allowusers \*, not')]:
            with self.subTest(name):
                (dropins / '000-aaa.conf').unlink(missing_ok=True)
                if earlier:
                    (dropins / '000-aaa.conf').write_text(earlier)
                effective.write_text(f'HostKey {hostkey}\n' + (f'Include {dropins}/*\n' if include else '')
                                     + 'PasswordAuthentication yes\nKbdInteractiveAuthentication yes\n' + tail)
                outputs = []
                for addr in SSHD_PROBE_ADDRESSES:
                    result = subprocess.run([str(sshd), '-T', '-f', str(effective), '-C', f'user={user},addr={addr},host=x'],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    outputs.append(result.stdout)
                if error is None:
                    sshd_effective_check(*outputs, user=user)
                else:
                    with self.assertRaisesRegex(AssertionError, error):
                        sshd_effective_check(*outputs, user=user)

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
        import plistlib
        # one root sshd per connection: cap them like Apple's ssh.plist, or a connection flood spawns without limit
        job = plistlib.loads(text.encode())
        self.assertEqual(job['inetdCompatibility'], {'Wait': False, 'Instances': 42})
        # Apple's process keys, so sessions behave as under Remote Login (evicted iCloud files open, no pressured exit)
        process_keys = {'POSIXSpawnType': 'Interactive', 'MaterializeDatalessFiles': True,
                        'EnablePressuredExit': False, 'EnableTransactions': False}
        self.assertEqual({key: job.get(key) for key in process_keys}, process_keys)
        apple = Path('/System/Library/LaunchDaemons/ssh.plist')
        if apple.exists():  # on a Mac, the same values as Apple's own, and no key Apple's job does not have
            apple_job = plistlib.loads(apple.read_bytes())
            self.assertEqual({key: apple_job.get(key) for key in process_keys}, process_keys)
            self.assertEqual(set(job) - set(apple_job), set())
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
            expected = ([f'{home}/.local/bin', f'{home}/.npm-global/bin', f'{home}/go/bin', f'{home}/bin', f'{home}/.cargo/bin',
                         f'{home}/.krew/bin'] + [f'{brew}/opt/{t}/libexec/gnubin' for t in gnubins]
                        + [f'{brew}/opt/python@3.14/libexec/bin', f'{brew}/bin'] + bare.split(':'))
            self.assertEqual(result.stdout.strip().split(':'), expected, result.stderr)
            # sourcing it again (nested shells, .zshrc after path_helper) moves the entries back to the front, once each
            result = subprocess.run(['zsh', '-f', '-c', 'source "$1"; PATH=/usr/bin:/bin:$PATH; _laptop_path; print -r -- $PATH',
                                     'zsh', str(zshenv)], env={'PATH': bare, 'HOME': str(home)}, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip().split(':'), expected, result.stderr)

    @unittest.skipUnless(shutil.which('zsh'), 'zsh required')
    def test_zshenv_points_docker_clients_at_the_podman_machine_socket(self):
        import socket
        tmp = Path(tempfile.mkdtemp(dir='/tmp'))  # short: a unix socket path is limited to ~104 bytes
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / 'podman').mkdir()

        def env_after(case, **env):
            result = subprocess.run(['zsh', '-f', '-c', 'source "$1"; print -r -- "${DOCKER_HOST-unset}|$KIND_EXPERIMENTAL_PROVIDER"',
                                     'zsh', str(self.out / case)], env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp),
                                     'TMPDIR': f'{tmp}/', **env}, capture_output=True, text=True)
            self.assertEqual(result.stderr, '')
            return result.stdout.strip()
        self.assertEqual(env_after('zshenv'), 'unset|podman')  # no machine running: no DOCKER_HOST
        for case, name in [('zshenv', 'podman-machine-default'), ('zshenv_machine', "dev vm's")]:
            path = tmp / 'podman' / f'{name}-api.sock'
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path))
            self.addCleanup(server.close)
            self.assertEqual(env_after(case), f'unix://{path}|podman')
        # a DOCKER_HOST already set, or Docker Desktop's socket, wins; the user's own socket only (-O: tests run unprivileged,
        # so a socket owned by someone else cannot be made here)
        self.assertEqual(env_after('zshenv', DOCKER_HOST='tcp://127.0.0.1:2375'), 'tcp://127.0.0.1:2375|podman')
        (tmp / '.docker/run').mkdir(parents=True)
        desktop = socket.socket(socket.AF_UNIX)
        desktop.bind(str(tmp / '.docker/run/docker.sock'))
        self.addCleanup(desktop.close)
        self.assertEqual(env_after('zshenv'), 'unset|podman')
        desktop.close()
        (tmp / '.docker/run/docker.sock').unlink()
        self.assertIn('-S "$_laptop_docker_sock" \\\n  && -O "$_laptop_docker_sock" ]]', self.read('zshenv'))
        (tmp / 'podman/podman-machine-default-api.sock').unlink()
        (tmp / 'podman/podman-machine-default-api.sock').write_text('')  # a plain file, not the machine's socket
        self.assertEqual(env_after('zshenv'), 'unset|podman')

    def test_launchd_ssh_agent_starts_over_a_stale_socket(self):
        # A SIGKILL or power loss leaves the socket file behind; ssh-agent -a will not bind over it.
        import plistlib
        import socket
        args = plistlib.loads(self.read('ssh_agent_plist').encode())['ProgramArguments']
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / 's.sock'
            dead = socket.socket(socket.AF_UNIX)
            dead.bind(str(stale))
            dead.close()
            agent = Path(directory) / 'ssh-agent'  # binds like ssh-agent: fails if the path exists
            agent.write_text('#!/usr/bin/env python3\nimport socket, sys\n'
                             'socket.socket(socket.AF_UNIX).bind(sys.argv[sys.argv.index("-a") + 1])\n')
            agent.chmod(0o755)
            args = [str(agent) if a.endswith('/bin/ssh-agent') else str(stale) if a.endswith('/ssh-agent.socket')
                    else a for a in args]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_systemd_ssh_agent_starts_over_a_stale_socket(self):
        unit = self.read('ssh_agent_unit')
        socket = re.search(r'^ExecStart=\S+ .*-a (\S+)', unit, re.M).group(1)
        self.assertIn(f'ExecStartPre=-/usr/bin/rm -f {socket}\n', unit)

    def test_launchd_ssh_auth_sock_names_the_ssh_agent_socket(self):
        agent = self.read('ssh_agent_plist')
        env = self.read('ssh_auth_sock_plist')
        socket = re.search(r'<string>-a</string>\s*<string>([^<]+)</string>', agent).group(1)
        self.assertTrue(socket.endswith('/.ssh/ssh-agent.socket'), socket)
        arguments = [a.replace('&amp;', '&') for a in
                     re.findall(r'<string>([^<]*)</string>', env.split('<array>')[1].split('</array>')[0])]
        self.assertEqual(arguments[0:2] + arguments[3:], ['/bin/zsh', '-c', 'zsh', socket])
        # run the job's command with a stub launchctl: SSH_AUTH_SOCK, then the PATH zsh -c built (from ~/.zshenv)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'log'
            stub = Path(directory) / 'launchctl'
            stub.write_text(f'#!/bin/sh\necho "$*" >> {log}\n')
            stub.chmod(0o755)
            command = arguments[2].replace('/bin/launchctl', str(stub))
            subprocess.run(['/bin/sh', '-c', command, 'zsh', socket], env={'PATH': '/opt/x/bin:/usr/bin:/bin'}, check=True)
            self.assertEqual(log.read_text().splitlines(), [f'setenv SSH_AUTH_SOCK {socket}', 'setenv PATH /opt/x/bin:/usr/bin:/bin'])
        self.assertIn('<key>RunAtLoad</key>', env)
        self.assertNotIn('KeepAlive', env)  # a one-shot at login: kept alive, launchd would rerun it forever
        if shutil.which('plutil'):
            for name in ('ssh_agent_plist', 'ssh_auth_sock_plist'):
                result = subprocess.run(['plutil', '-lint', str(self.out / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_podman_machine_ensure_starts_a_stopped_machine_and_trims_a_running_one(self):
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
                '  "machine ssh") echo "ssh $3 $4 sock=${SSH_AUTH_SOCK:-}" >> "$LOG"; exit "${SSH_RC:-0}" ;;\n'
                '  *) exit 2 ;;\n'
                'esac\n')
            (d / 'bin/podman').chmod(0o755)
            hold = d / 'hold'
            script = d / 'ensure'
            stamp = d / 'trimmed'
            script.write_text(self.read('podman_ensure').replace('/HOLD-FILE-PLACEHOLDER', str(hold))
                              .replace('/TRIM-STAMP-PLACEHOLDER', str(stamp)))
            log = d / 'log'

            def run(**state):
                log.write_text('')
                result = subprocess.run(['sh', str(script)], capture_output=True, text=True, timeout=30,
                                        env={'PATH': f'{d}/bin:/usr/bin:/bin', 'LOG': str(log),
                                             'SSH_AUTH_SOCK': '/agent.sock', **state})
                return result.returncode, result.stdout, log.read_text()
            booted = '2026-11-14 09:00:00.000000 +0000 UTC'
            trimmed = 'ssh podman-machine-default sudo fstrim -a sock=\n'  # without the agent's socket
            self.assertEqual(run(STATE='running', LASTUP=booted), (0, '', trimmed))  # no stamp yet: trims
            self.assertTrue(stamp.exists())
            self.assertEqual(run(STATE='running', LASTUP=booted), (0, '', ''))  # trimmed within the hour
            os.utime(stamp, (time.time() - 2 * 3600,) * 2)
            self.assertEqual(run(STATE='running', LASTUP=booted), (0, '', trimmed))  # stamp older than an hour
            stamp.unlink()
            rc, out, log_text = run(STATE='running', LASTUP=booted, SSH_RC='1')
            self.assertEqual((rc, log_text), (0, trimmed))
            self.assertTrue(out.endswith(' trimming podman-machine-default failed\n'), out)
            self.assertTrue(stamp.exists())  # a failed trim waits for the next interval, not every check
            rc, out, started = run(STATE='stopped', LASTUP=booted)
            self.assertEqual((rc, started), (0, 'start podman-machine-default\n'))
            self.assertTrue(out.endswith(' starting podman-machine-default\n'), out)
            self.assertEqual(run(STATE='stopped', LASTUP='0001-01-01 00:00:00 +0000 UTC'), (0, '', ''))  # never booted
            self.assertEqual(run(STATE='stopped', LASTUP=''), (0, '', ''))
            self.assertEqual(run(), (0, '', ''))  # no machine yet
            hold.write_text('')
            self.assertEqual(run(STATE='stopped', LASTUP=booted), (0, '', ''))
            stamp.unlink()
            self.assertEqual(run(STATE='running', LASTUP=booted), (0, '', ''))  # on hold: left alone, not trimmed

    def test_podman_machine_ensure_prunes_weekly_before_trimming(self):
        self.assertNotIn('system prune', self.read('podman_ensure'))  # containers_prune_enabled defaults off
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / 'bin').mkdir()
            (d / 'bin/podman').write_text(
                '#!/bin/sh\n'
                'case "$1 $2" in\n'
                '  "machine inspect") echo running ;;\n'
                '  "system prune") echo "prune $3 sock=${SSH_AUTH_SOCK:-}" >> "$LOG"; exit "${PRUNE_RC:-0}" ;;\n'
                '  "machine ssh") echo "trim" >> "$LOG" ;;\n'
                'esac\n')
            (d / 'bin/podman').chmod(0o755)
            script, log, pruned, trimmed = d / 'ensure', d / 'log', d / 'pruned', d / 'trimmed'
            script.write_text(self.read('podman_ensure_prune').replace('/HOLD-FILE-PLACEHOLDER', str(d / 'hold'))
                              .replace('/TRIM-STAMP-PLACEHOLDER', str(trimmed))
                              .replace('/PRUNE-STAMP-PLACEHOLDER', str(pruned)))

            def run(**env):
                log.write_text('')
                trimmed.unlink(missing_ok=True)
                result = subprocess.run(['sh', str(script)], capture_output=True, text=True, timeout=30,
                                        env={'PATH': f'{d}/bin:/usr/bin:/bin', 'LOG': str(log),
                                             'SSH_AUTH_SOCK': '/agent.sock', **env})
                return result.stdout, log.read_text()
            self.assertEqual(run(), ('', 'prune --force sock=\ntrim\n'))  # first run: prune, then trim
            self.assertEqual(run(), ('', 'trim\n'))  # pruned within the week
            os.utime(pruned, (time.time() - 8 * 86400,) * 2)
            self.assertEqual(run(), ('', 'prune --force sock=\ntrim\n'))
            os.utime(pruned, (time.time() - 8 * 86400,) * 2)
            out, logged = run(PRUNE_RC='1')
            self.assertTrue(out.endswith(' pruning podman-machine-default failed\n'), out)
            self.assertEqual(logged, 'prune --force sock=\ntrim\n')  # a failed prune still trims
            (d / 'hold').write_text('')
            os.utime(pruned, (time.time() - 8 * 86400,) * 2)
            self.assertEqual(run(), ('', ''))  # on hold: left alone

    def test_audit_rules_set_backlog_before_rules(self):
        lines = [l for l in self.read('audit').splitlines() if l and not l.startswith('#')]
        self.assertEqual(lines.count('-b 8192'), 1)
        self.assertLess(lines.index('-b 8192'), min(i for i, l in enumerate(lines) if l.startswith(('-w', '-a'))))


class ShippedFileTests(unittest.TestCase):
    def test_global_gitignore_ignores_build_files_not_directories(self):
        with tempfile.TemporaryDirectory() as name:
            repo = Path(name)
            for path in ('conf.d/a.conf', 'tags/post.md', 'dep.d', 'tags', 'TAGS', 'main.o'):
                (repo / path).parent.mkdir(parents=True, exist_ok=True)
                if path != 'tags':
                    (repo / path).touch()
            (repo / 'sub').mkdir()
            (repo / 'sub/tags').touch()
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', HOME=name)
            subprocess.run(['git', 'init', '-q', name], env=env, check=True)
            result = subprocess.run(['git', '-c', f'core.excludesFile={ROOT / "roles/dotfiles/files/gitignore_global"}',
                                     'status', '--porcelain', '-uall'], cwd=name, env=env, capture_output=True,
                                    text=True, check=True)
            self.assertEqual(sorted(line[3:] for line in result.stdout.splitlines()),
                             ['conf.d/a.conf', 'tags/post.md'])

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
        with tempfile.TemporaryDirectory() as directory:
            # As deployed: the global core.hooksPath is where the global hook lives (else git would run .git/hooks
            # itself, and the global hook rightly would not chain into the directory git runs from).
            (Path(directory) / 'gitconfig').write_text(f'[core]\n\thooksPath = {hook.parent}\n')
            env = dict(os.environ, GIT_CONFIG_GLOBAL=str(Path(directory) / 'gitconfig'), GIT_CONFIG_SYSTEM='/dev/null',
                       GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.test',
                       GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.test', PATH='/usr/bin:/bin')
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


    def test_a_file_path_in_the_vault_never_overwrites_a_key_file(self):
        """Each private key deploy is preceded by an assert that the vault value is a key block, not a pasted path."""
        tasks = yaml.safe_load((ROOT / 'roles/ssh/tasks/main.yml').read_text())
        names = [t.get('name', '') for t in tasks]
        block = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n-----END OPENSSH PRIVATE KEY-----\n'
        for key, deploy in [('ssh_auth_key', 'Deploy SSH auth key'), ('ssh_signing_key', 'Deploy SSH signing key')]:
            at = names.index(deploy)
            guard = tasks[at - 1]
            self.assertIn('ansible.builtin.assert', guard, deploy)
            that = guard['ansible.builtin.assert']['that']
            pattern = re.search(r"search\('(.*)'\)", that).group(1)
            self.assertTrue(re.search(pattern, block), deploy)
            for wrong in ('/home/me/.ssh/id_ed25519_sk_signing', '~/.ssh/id_ed25519_sk', 'sk-ssh-ed25519@openssh.com AAAA'):
                self.assertFalse(re.search(pattern, wrong), (deploy, wrong))
        dot = yaml.safe_load((ROOT / 'roles/dotfiles/tasks/main.yml').read_text())
        signers = next(t for t in dot if t.get('name') == 'Validate and deploy git allowed signers')
        first = signers['block'][0]['ansible.builtin.assert']['that']
        path_re = re.search(r"match\('(.*)'\)", first).group(1).replace('\\\\', '\\')
        self.assertTrue(re.match(path_re, '/home/me/.ssh/id_ed25519_sk_signing.pub'))
        self.assertFalse(re.match(path_re, 'sk-ssh-ed25519@openssh.com AAAAC3 signing'))

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

    def test_acli_login_runs_alone_under_its_tag_with_a_default_site(self):
        """make acli-login runs --tags common,acli: the include and every task it pulls in carry the acli tag."""
        main = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        include = next(t for t in main if t.get('name') == 'Log acli in to Jira from the vault')
        self.assertIn('acli', include['tags'])
        self.assertIn('acli', include['ansible.builtin.include_tasks']['apply']['tags'])
        self.assertIn('--tags common,acli --skip-tags become', (ROOT / 'Makefile').read_text())
        defaults = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
        site = jinja2.Environment().from_string(defaults['acli_site'])
        self.assertEqual(site.render(profile='work'), 'redhat.atlassian.net')
        self.assertEqual(site.render(), 'redhat.atlassian.net')  # profile unset means work
        self.assertEqual(site.render(profile='personal'), '')  # opt-in: personal machines share the vault
        smoke = (ROOT / 'scripts/smoke-test.sh').read_text()
        self.assertIn('if [[ "$profile" == "work" ]]; then _acli_site=redhat.atlassian.net; else _acli_site=""; fi',
                      smoke)

    def test_acli_calls_use_the_resolved_timeout_and_check_the_macos_keychain(self):
        """macOS has no bare timeout without gnubin, and a locked Keychain (SSH) must stop the login like Linux's."""
        tasks = yaml.safe_load((ROOT / 'roles/claude/tasks/acli.yml').read_text())

        def walk(items):
            for task in items:
                yield task
                yield from walk(task.get('block', []))
        for task in walk(tasks):
            argv = task.get('ansible.builtin.command', {}).get('argv', [])
            if '{{ _acli_bin }}' in argv:
                self.assertEqual(argv[0], '{{ _acli_timeout_bin }}', task['name'])
                self.assertEqual(argv[2], '{{ _acli_bin }}', task['name'])
            self.assertNotIn('acli', argv, task['name'])  # bare acli: not on make's PATH on macOS
        keychain = next(t for t in walk(tasks) if 'security' in t.get('ansible.builtin.command', {}).get('argv', []))
        self.assertIn('is_macos', keychain['when'])
        marker = next(t for t in tasks if t.get('name') == 'Mark that the vault holds an acli token')
        self.assertEqual(marker['ansible.builtin.copy']['mode'], '0600')
        self.assertNotIn('{{', marker['ansible.builtin.copy']['content'])  # fixed text: no secret, not even a digest
        main = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        cleanup = next(t for t in main if t.get('name') == 'Remove the acli token marker when no acli login is configured')
        self.assertEqual(cleanup['ansible.builtin.file']['state'], 'absent')
        for condition in ('acli_api_token', 'acli_site'):
            self.assertIn(condition, cleanup['when'])
        self.assertNotIn('profile', cleanup['when'])  # the login runs on every profile, so the marker stays on each
        decide = next(t for t in tasks if t.get('name') == 'Decide whether the keyring allows an acli login')
        self.assertIn('_acli_keychain', decide['ansible.builtin.set_fact']['_acli_keyring_ok'])

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


def run_tasks(tasks, variables, check=False):
    """Run production tasks on localhost with fixture variables (in check mode with check=True); return the json
    callback's task results."""
    import json
    with tempfile.TemporaryDirectory() as directory:
        playbook = Path(directory) / 'play.yml'
        playbook.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                                             'tasks': tasks}]))
        result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)] + (['--check'] if check else []), cwd=ROOT,
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



SSHD_PROBE_ADDRESSES = ['100.64.0.1', '203.0.113.5']  # the play's probes: a tailnet address and TEST-NET-3 outside it


def sshd_effective_check(*outputs, user='u', allow_from=None):
    """Run the play's check of sshd -T output, one output per probe address (the last one repeats); raises
    AssertionError with its message when it fails."""
    task = production_task('roles/desktop/tasks/main.yml', 'Check the effective sshd settings are key-only and this user only (macOS)')
    if allow_from is None:
        allow_from = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())['desktop_macos_ssh_allow_from']
    results = [{'item': addr, 'stdout_lines': outputs[min(i, len(outputs) - 1)].splitlines()}
               for i, addr in enumerate(SSHD_PROBE_ADDRESSES)]
    run_tasks([task], {'_desktop_sshd_effective': {'results': results}, 'ansible_facts': {'user_id': user},
                       'desktop_macos_ssh_allow_from': allow_from,
                       'desktop_macos_sshd_dropin': '/etc/ssh/sshd_config.d/000-laptop-setup.conf'})

# Root commands: every task that runs with become (on the task, a block or an include site) is checked unless a `when`
# limits it to Linux. A term limits it when it is one of these; a `when` list is ANDed, as are the ` and ` parts of a
# term without ` or `. _packages_linux_x86_work ANDs is_linux (checked in the test).
LINUX_TERM = re.compile(
    r"""(is_linux|is_dnf5?|is_apt|is_rhel|is_fedora|csb_rhel|_packages_linux_x86_work|not\s+is_macos
    |ansible_facts\[['"]system['"]\](\s*\|\s*default\(['"]\w+['"]\))?\s*==\s*['"]Linux['"]
    )(\s*\|\s*bool)?""", re.X)
ROOT_COMMAND_MODULES = ('ansible.builtin.command', 'ansible.builtin.shell')
# run a command in a form not read here, or by a short name that a collection could shadow
ROOT_REJECTED_MODULES = {'raw', 'script', 'command', 'shell', 'expect', 'ansible.builtin.raw', 'ansible.builtin.script',
                         'ansible.builtin.expect', 'ansible.legacy.raw', 'ansible.legacy.script', 'ansible.legacy.command',
                         'ansible.legacy.shell', 'ansible.legacy.expect'}
INCLUDE_MODULES = ('ansible.builtin.include_tasks', 'ansible.builtin.import_tasks', 'include_tasks', 'import_tasks')
INCLUDE_ROLE_MODULES = ('ansible.builtin.include_role', 'ansible.builtin.import_role', 'include_role', 'import_role')
FALSE_WORDS = {'false', 'no', 'off', '0', 'n', 'f'}
SHELL_BUILTINS = {'[', '[[', 'test', 'echo', 'printf', 'true', 'false', ':', 'exit', 'return', 'set', 'cd', 'export', 'local',
                  'read', 'unset', 'shift', 'break', 'continue', 'wait', 'trap', 'umask'}  # no PATH lookup
SHELL_PREFIX_WORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', '(', 'time', 'exec'}
SHELL_SKIP_WORDS = {'fi', 'done', '}', ')', 'esac', 'for', 'case'}


def become_on(value):
    """Fail closed: a become value that is not clearly false (a template, "True", an empty value, ...) counts as root."""
    if value is False or (isinstance(value, int) and not isinstance(value, bool) and value == 0):
        return False
    return not (isinstance(value, str) and value.strip().lower() in FALSE_WORDS)


def linux_only(when):
    for term in when if isinstance(when, list) else [] if when is None else [when]:
        term = str(term).strip()
        parts = [term] if ' or ' in term else re.split(r'\s+and\s+', term)
        if any(LINUX_TERM.fullmatch(part.strip()) for part in parts):
            return True
    return False


def shell_command_words(cmd):
    """The command word of each simple command in a shell string (Jinja expressions kept verbatim)."""
    jinja = []

    def hide(match):
        jinja.append(match.group(0))
        return f'\0{len(jinja) - 1}\0'
    cmd = re.sub(r'{{.*?}}|{%.*?%}', hide, cmd, flags=re.S)
    cmd = re.sub(r'\$\(\(.*?\)\)', 'ARITH', cmd, flags=re.S)  # arithmetic, not a command substitution
    cmd = re.sub(r'\d*[<>]&\d*-?|&>>?', ' ', cmd)  # fd redirections, not a background &
    words = []
    for segment in re.split(r'&&|\|\||\$\(|[;|&\n`]', cmd):
        tokens = segment.split()
        while tokens and (tokens[0] in SHELL_PREFIX_WORDS or re.match(r'[A-Za-z_]\w*=', tokens[0])):
            tokens.pop(0)  # keywords and VAR=value assignments before the command
        if not tokens or tokens[0] in SHELL_SKIP_WORDS or tokens[0].startswith('#'):
            continue
        word = tokens[0].lstrip('("\'{').rstrip(')"\'')
        if word and word not in SHELL_BUILTINS:
            words.append(re.sub(r'\0(\d+)\0', lambda m: jinja[int(m.group(1))], word))
    return words


class RootCommandScan:
    """Commands that run as root outside Linux-only `when`s, following blocks, include/import_tasks and
    include/import_role (tasks_from; that role's handlers are scanned as entry points).

    Known limits, not scanned: site.yml's own play tasks (pre_tasks, tasks, post_tasks); a bare name behind a full-path
    wrapper, since only the first word of each simple command is read (/bin/sh -c 'launchctl ...', ... | /usr/bin/xargs
    launchctl, /usr/bin/env launchctl); become set through the ansible_become variable or the command line.
    """

    def __init__(self, load=None):
        self.load = load or (lambda path: yaml.safe_load((ROOT / path).read_text()) or [])
        self.seen = set()
        self.commands = []  # (path, task name, command word)
        self.rejected = []  # (path, task name, module)

    @staticmethod
    def resolve(path, include):
        role = path.split('/')[1] if path.startswith('roles/') else None
        include = str(include).replace('{{ role_path }}', f'roles/{role}').replace('{{ common_project_root }}/', '')
        if '{{' in include:
            raise AssertionError(f'{path}: cannot resolve include {include}')
        if not include.startswith(('roles/', 'common/')):
            include = str(Path(path).parent / include)  # relative to the including file
        return os.path.normpath(include)

    def scan(self, path, become=False, linux=False):
        if (path, become, linux) not in self.seen:
            self.seen.add((path, become, linux))
            self.walk(path, self.load(path), become, linux)

    def walk(self, path, tasks, become, linux):
        for task in tasks or []:
            task_become = become_on(task['become']) if 'become' in task else become
            task_linux = linux or linux_only(task.get('when'))
            for key in ('block', 'rescue', 'always'):
                self.walk(path, task.get(key), task_become, task_linux)
            for module in INCLUDE_MODULES:
                if module in task:
                    args = task[module]
                    apply = (args.get('apply') or {}) if isinstance(args, dict) else {}
                    self.scan(self.resolve(path, args['file'] if isinstance(args, dict) else args),
                              become_on(apply['become']) if 'become' in apply else task_become, task_linux)
            for module in INCLUDE_ROLE_MODULES:
                if module in task:
                    args = task[module] if isinstance(task[module], dict) else {'name': task[module]}
                    apply = args.get('apply') or {}
                    role, tasks_from = str(args.get('name')), str(args.get('tasks_from') or 'main')
                    if '{{' in role + tasks_from:
                        raise AssertionError(f'{path}: cannot resolve role {role} tasks_from {tasks_from}')
                    tasks_from += '' if tasks_from.endswith(('.yml', '.yaml')) else '.yml'
                    self.scan(f'roles/{role}/tasks/{tasks_from}',
                              become_on(apply['become']) if 'become' in apply else task_become, task_linux)
            if task_become and not task_linux:
                self.check(path, task)

    def check(self, path, task):
        name = task.get('name')
        self.rejected += [(path, name, module) for module in sorted(ROOT_REJECTED_MODULES & set(task))]
        words = []
        for module in ROOT_COMMAND_MODULES:
            if module in task:
                args = task[module]
                if isinstance(args, dict) and 'argv' in args:
                    words.append(str(args['argv'][0]))
                    continue
                cmd = args.get('cmd') if isinstance(args, dict) else args
                if cmd is None:
                    cmd = (task.get('args') or {}).get('cmd', '')
                words += shell_command_words(str(cmd))
        for args in task.values():
            if isinstance(args, dict) and isinstance(args.get('validate'), str):
                words += shell_command_words(args['validate'])
        self.commands += [(path, name, word) for word in words]


def site_roles(site):
    """{role: (become, linux)} from site.yml's plays: become from the role entry, else the play (Play 1's play-level
    become); Linux-only when every entry of the role has a Linux-only `when`."""
    roles = {}
    for play in site:
        for entry in play.get('roles', []):
            entry = entry if isinstance(entry, dict) else {'role': entry}
            become = become_on(entry['become']) if 'become' in entry else become_on(play.get('become', False))
            old_become, old_linux = roles.get(entry['role'], (False, True))
            roles[entry['role']] = (old_become or become, old_linux and linux_only(entry.get('when')))
    return roles


def scan_root_commands(site=None):
    """Scan every role's tasks and handlers and common/tasks; roles that site.yml runs only on Linux are out of scope,
    and a role site.yml runs with become starts with it."""
    roles = site_roles(site if site is not None else yaml.safe_load((ROOT / 'site.yml').read_text()))
    linux_roles = {role for role, (_, linux) in roles.items() if linux}
    scan = RootCommandScan()
    entries = (sorted(ROOT.glob('roles/*/tasks/main.yml')) + sorted(ROOT.glob('roles/*/handlers/*.yml'))
               + sorted(ROOT.glob('common/tasks/*.yml')) + sorted(ROOT.glob('roles/*/tasks/*.yml')))
    for file in entries:  # entry points first: an included file is walked with its include sites' become and when
        path = str(file.relative_to(ROOT))
        become, linux = roles.get(path.split('/')[1], (False, False)) if path.startswith('roles/') else (False, False)
        if not any(seen == path for seen, _, _ in scan.seen):
            scan.scan(path, become, linux)
    return scan, linux_roles


class ClaudeTaskTests(unittest.TestCase):
    def test_personal_profile_gets_the_project_mode_exclusion_dir(self):
        """A personal profile without the queue still writes the exclusion list: its directory must be created first."""
        names = ('Ensure ~/.config/claude directory exists', 'Deploy project permission mode exclusions')
        tasks = [production_task('roles/claude/tasks/main.yml', name) for name in names]
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'home/.config/claude'
            variables = {'profile': 'personal', 'claude_queue_enabled': False, 'claude_project_mode_override': True,
                         'claude_project_mode_exclude': ['/x'], 'claude_runtime_config_dir': str(runtime)}
            results = run_tasks(tasks, variables, check=True)  # a fresh machine's plan: the directory is only simulated
            self.assertFalse(runtime.exists())
            self.assertTrue(results[1].get('failed') is not True or results[1].get('ignored'), results[1])
            run_tasks(tasks, variables)
            self.assertEqual((runtime / 'project-mode-exclude').read_text().splitlines()[1:], ['/x'])
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)


class MacTaskTests(unittest.TestCase):
    def test_app_bundles_are_copied_with_their_symlinks(self):
        """macOS cp -r follows symlinks: a framework's Versions/Current link would become a directory, breaking the
        bundle's code signature. Every task copying with cp uses -R; on a Mac, the task's own options keep the link."""
        def walk(tasks):
            for task in tasks or []:
                yield task
                for key in ('block', 'rescue', 'always'):
                    yield from walk(task.get(key))

        copies = []
        for path in sorted(ROOT.glob('roles/*/tasks/*.yml')):
            for task in walk(yaml.safe_load(path.read_text())):
                command = task.get('ansible.builtin.command') or task.get('ansible.builtin.shell') or {}
                argv = command.get('argv') if isinstance(command, dict) else None
                if argv and Path(str(argv[0])).name == 'cp':
                    copies.append((f"{path.relative_to(ROOT)}: {task.get('name')}", argv))
                text = command if isinstance(command, str) else command.get('cmd', '') if isinstance(command, dict) else ''
                self.assertNotRegex(text, r'\bcp\s+-[A-Za-z]*r', f'{path}: {task.get("name")}')
        self.assertEqual(len([c for c in copies if 'AeroSpace.app' in c[0]]), 2)  # the user and the sudo copy
        for where, argv in copies:
            options = [a for a in argv[1:] if str(a).startswith('-')]
            self.assertFalse(any('r' in o.lstrip('-') for o in options), f'{where}: {argv}')
            if os.uname().sysname == 'Darwin' and argv[0] == '/bin/cp':
                with tempfile.TemporaryDirectory() as directory:
                    versions = Path(directory) / 'src.app/Contents/Frameworks/F.framework/Versions'
                    (versions / 'A').mkdir(parents=True)
                    (versions / 'A/F').write_text('x\n')
                    (versions / 'Current').symlink_to('A')
                    (Path(directory) / 'dst').mkdir()
                    subprocess.run(['/bin/cp', *options, str(Path(directory) / 'src.app'), str(Path(directory) / 'dst')],
                                   check=True)
                    copied = Path(directory) / 'dst/src.app/Contents/Frameworks/F.framework/Versions/Current'
                    self.assertTrue(copied.is_symlink(), where)

    def test_aerospace_app_is_made_readable_after_each_copy(self):
        """cp -R copies under the run's umask (077 from the dotfiles zshrc): each copy is followed by a chmod of the
        same bundle, as the same user, run only when that copy ran. On a Mac, that cp then chmod under umask 077
        leaves directories and executables 0755 and other files 0644, and the chmod follows no symlink."""
        def lists(node):  # every task list (a file's, a block's), to find the one holding the copies
            if isinstance(node, list):
                yield node
                for item in node:
                    yield from lists(item)
            elif isinstance(node, dict):
                for value in node.values():
                    yield from lists(value)
        tasks = next(lst for lst in lists(yaml.safe_load((ROOT / 'roles/packages/tasks/install_brew.yml').read_text()))
                     if any(isinstance(t, dict) and str(t.get('name', '')).startswith('Install AeroSpace.app to')
                            for t in lst))
        names = [t.get('name') for t in tasks]
        pairs = 0
        for index, task in enumerate(tasks):
            argv = (task.get('ansible.builtin.command') or {}).get('argv') or []
            if argv[:1] != ['/bin/cp']:
                continue
            chmod = tasks[index + 1]
            self.assertEqual(chmod['ansible.builtin.command']['argv'],
                             ['/bin/chmod', '-R', 'u+rwX,go+rX,go-w', '/Applications/AeroSpace.app'], names[index + 1])
            self.assertEqual(argv[-1], '/Applications/')  # the copy lands where the chmod looks
            self.assertEqual(bool(chmod.get('become')), bool(task.get('become')), names[index + 1])
            self.assertEqual(chmod.get('tags'), task.get('tags'), names[index + 1])
            self.assertEqual(chmod['when'], f"{task['register']} is changed", names[index + 1])
            pairs += 1
        self.assertEqual(pairs, 2)
        if os.uname().sysname != 'Darwin':
            return
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            macos = base / 'src/AeroSpace.app/Contents/MacOS'
            macos.mkdir(parents=True)
            (macos / 'AeroSpace').write_text('x\n')
            (macos / 'AeroSpace').chmod(0o755)
            (base / 'src/AeroSpace.app/Contents/Info.plist').write_text('<plist/>\n')
            outside = base / 'outside'
            outside.write_text('secret\n')
            outside.chmod(0o600)
            (base / 'src/AeroSpace.app/Contents/link').symlink_to(outside)
            (base / 'Applications').mkdir()
            copy = next(t for t in tasks if (t.get('ansible.builtin.command') or {}).get('argv', [''])[0] == '/bin/cp')
            options = [a for a in copy['ansible.builtin.command']['argv'][1:] if a.startswith('-')]
            chmod = tasks[tasks.index(copy) + 1]['ansible.builtin.command']['argv']
            app = base / 'Applications/AeroSpace.app'
            subprocess.run(['/bin/sh', '-c', 'umask 077 && "$@"', 'sh', '/bin/cp', *options,
                            str(base / 'src/AeroSpace.app'), str(base / 'Applications')], check=True)
            self.assertEqual(app.stat().st_mode & 0o777, 0o700)  # what the copy alone leaves
            subprocess.run(['/bin/sh', '-c', 'umask 077 && "$@"', 'sh', *chmod[:-1], str(app)], check=True)
            modes = {p.relative_to(app).as_posix(): p.lstat().st_mode & 0o777 for p in [app, *app.rglob('*')]
                     if not p.is_symlink()}
            self.assertEqual(modes, {'.': 0o755, 'Contents': 0o755, 'Contents/MacOS': 0o755,
                                     'Contents/MacOS/AeroSpace': 0o755, 'Contents/Info.plist': 0o644})
            self.assertEqual(outside.stat().st_mode & 0o777, 0o600)  # the link's target is untouched

    def test_claude_queue_job_is_not_throttled_as_background(self):
        """The queue poller runs real agent work under a timeout: launchd's Background type would throttle it."""
        import plistlib
        env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
        env.filters['regex_replace'] = lambda value, pattern, repl='': re.sub(pattern, repl, value)
        text = env.from_string((ROOT / 'roles/claude/templates/claude-queue.plist.j2').read_text()).render(
            ansible_facts={'user_dir': '/Users/u'}, ansible_managed='test', claude_bin_path='/Users/u/.local/bin/claude',
            claude_log_dir='/Users/u/.local/state/claude/logs', claude_queue_allowed_authors=['u'],
            claude_queue_config_dir='/Users/u/.config/claude', claude_queue_host_label='mac',
            claude_queue_max_issues_per_run=5, claude_queue_max_turns=50, claude_queue_poll_interval_sec=120,
            claude_queue_poller_path='/Users/u/.local/bin/claude-queue-poller', claude_queue_repo='u/tasks',
            claude_queue_timeout=1800, dotfiles_github_user='u', dotfiles_homebrew_prefix='/opt/homebrew')
        self.assertEqual(plistlib.loads(text.encode())['ProcessType'], 'Standard')

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
                                            ([dangling], {'exists': True, 'executable': False}, False),
                                            # stat failed (failed_when: false; e.g. a root-only /usr/local/bin): no stat key
                                            ([None, dangling], podman, True),
                                            ([None, {'exists': True, 'executable': True}], podman, False)]:
            with self.subTest(docker=docker, podman=podman_stat):
                results = run_tasks([task, {'ansible.builtin.debug': {'var': '_containers_docker_shim_wanted'}}], {
                    'containers_macos_docker_shim': True, 'containers_macos_machine_enabled': True,
                    '_containers_podman_bin_stat': {'stat': podman_stat},
                    '_containers_real_docker': {'results': [{'msg': 'Permission denied'} if stat is None else {'stat': stat}
                                                            for stat in docker]}})
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

    def test_macos_sshd_effective_settings_must_be_key_only(self):
        """sshd -T sample output (lowercase keys, one per line, one allowusers line per entry): every one of the three
        settings must be there, and the allowusers lines are exactly user@<range>, for both probe addresses."""
        good = ('port 22\npasswordauthentication no\nkbdinteractiveauthentication no\npubkeyauthentication yes\n'
                'authenticationmethods publickey\nallowusers u@100.64.0.0/10\nallowusers u@fd7a:115c:a1e0::/48\n')
        sshd_effective_check(good)
        for wrong in ('passwordauthentication yes', 'kbdinteractiveauthentication yes', 'authenticationmethods any',
                      'authenticationmethods publickey,password'):
            key = wrong.split()[0]
            text = '\n'.join(wrong if line.startswith(key + ' ') else line for line in good.splitlines())
            for outputs in ([text], [good, text]):  # a tailnet login, or only one from outside it (a Match Address block)
                with self.subTest(wrong, outside_only=len(outputs) == 2):
                    with self.assertRaisesRegex(AssertionError, f'from {SSHD_PROBE_ADDRESSES[len(outputs) - 1]} .*shows .*{wrong}'):
                        sshd_effective_check(*outputs)
        with self.assertRaisesRegex(AssertionError, 'must Include'):
            sshd_effective_check('passwordauthentication no\nkbdinteractiveauthentication no\n')
        allow = 'allowusers u@100.64.0.0/10\nallowusers u@fd7a:115c:a1e0::/48\n'
        for name, users in [('any user', allow + 'allowusers *\n'), ('only a pattern', 'allowusers *\n'),
                            ('another user', allow + 'allowusers other@100.64.0.0/10\n'),
                            ('any address', 'allowusers u\n'), ('a range missing', 'allowusers u@100.64.0.0/10\n'),
                            ('a wider range', allow.replace('/10', '/8')), ('none', '')]:
            text = good.replace(allow, users)
            for outputs in ([text], [good, text]):
                with self.subTest(name, outside_only=len(outputs) == 2):
                    with self.assertRaisesRegex(AssertionError, 'allowusers'):
                        sshd_effective_check(*outputs)
        # the configured ranges, as written, in any order
        sshd_effective_check(good.replace(allow, 'allowusers u@fd7a:115c:a1e0::/48\nallowusers u@100.64.0.0/10\n'))
        sshd_effective_check(good.replace(allow, 'allowusers u@10.0.0.0/8\n'), allow_from=['10.0.0.0/8'])
        with self.assertRaisesRegex(AssertionError, 'as root shows'):
            sshd_effective_check(good.replace('allowusers u@', 'allowusers root@'), user='root')
        # it runs before the job is deployed and (re)loaded, after the drop-in and sshd -t
        tasks = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        block = next(t for t in tasks if t.get('name') == 'Apply the sshd login policy and start sshd (macOS)')
        names = [t['name'] for t in block['block']]
        read = names.index('Read the effective sshd settings for a tailnet and an outside login (macOS)')
        self.assertLess(names.index('Validate the sshd configuration (macOS)'), read)
        self.assertEqual(names[read + 1], 'Check the effective sshd settings are key-only and this user only (macOS)')
        self.assertLess(read + 1, names.index('Deploy the sshd launchd job on the SSH port (macOS)'))
        self.assertEqual(block['block'][read]['ansible.builtin.command']['argv'][:3], ['/usr/sbin/sshd', '-T', '-C'])
        self.assertEqual(block['block'][read]['loop'], SSHD_PROBE_ADDRESSES)

    def test_macos_sshd_probe_asks_for_the_real_user(self):
        """sshd -T -C user= is the gathered login user (Play 2 gathers facts without become), never root: the block's
        become does not change ansible_facts['user_id'], and the drop-in's AllowUsers names the same user."""
        read = production_task('roles/desktop/tasks/main.yml',
                               'Read the effective sshd settings for a tailnet and an outside login (macOS)')
        argv = read.pop('ansible.builtin.command')['argv']
        self.assertEqual(argv[3], "user={{ ansible_facts['user_id'] }},addr={{ item }},host=x")
        template = (ROOT / 'roles/desktop/templates/sshd-macos.conf.j2').read_text()
        self.assertIn("{{ ansible_facts['user_id'] }}@{{ net }}", template)
        read.pop('register')
        read['ansible.builtin.debug'] = {'msg': argv[3]}
        results = run_tasks([read], {'ansible_facts': {'user_id': 'alice'}})[0]['results']
        self.assertEqual([r['msg'] for r in results], [f'user=alice,addr={a},host=x' for a in SSHD_PROBE_ADDRESSES])
        play = next(p for p in yaml.safe_load((ROOT / 'site.yml').read_text())
                    if any(isinstance(r, dict) and r.get('role') == 'desktop' for r in p.get('roles', [])))
        self.assertIs(play['become'], False)
        self.assertIs(play['gather_facts'], True)

    def test_macos_ssh_allow_from_entries_must_be_cidrs(self):
        task = production_task('roles/desktop/tasks/main.yml', 'Check the sshd tailnet ranges are CIDRs (macOS)')
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        names = [t.get('name') for t in yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())]
        self.assertLess(names.index(task['name']), names.index('Apply the sshd login policy and start sshd (macOS)'))

        def check(value):
            run_tasks([task], {'is_macos': True, 'desktop_macos_remote_login': True, 'desktop_macos_ssh_allow_from': value})
        check(defaults['desktop_macos_ssh_allow_from'])
        check(['10.0.0.0/8', '192.168.1.7/32', '0.0.0.0/0', '255.249.199.100/32', '::1/128', 'FD7A:115C:A1E0::/48',
               '2001:db8::/32', '::/0', 'fd7a::/48', '1:2:3:4:5:6:7:8/64', '1:2:3:4:5:6:7::/64', '::2:3:4:5:6:7:8/64',
               '1::8/128', '1:2::7:8/64', 'abcd:ef01::/0'])
        for bad in (['100.64.0.0'], ['100.64.0.0/10 10.0.0.0/8'], ['100.64.0.0/10\nMatch all'], ['100.64.0.0/10\n'],
                    ['all'], ['*'], ['256.1.1.1/8'], ['100.64.0.0/33'], ['fd7a::/129'], ['fd7a::'], ['fd7a::g/48'],
                    [' 100.64.0.0/10'], ['100.64.0.0/10', '!10.0.0.0/8'], [10], '100.64.0.0/10', [],
                    [':/0'], [':::::/1'], ['1:2:3:4:5:6:7:8:9:10/64'], ['1:2:3:4:5:6:7:8:9/64'], ['1:2:3:4:5:6:7/64'],
                    ['1::2::3/64'], [':::/0'], ['1:::2/64'], ['12345::/16'], ['1:2:3:4:5:6:7:8::/64'], ['::1:2:3:4:5:6:7:8/64'],
                    ['fd7a:/48'], [':fd7a::/48'], ['fd7a::/048'], ['::ffff:1.2.3.4/96'], ['010.0.0.0/8'], ['1.2.3/24'],
                    ['1.2.3.4/032'], ['1.2.3.4.5/8']):
            with self.subTest(bad=bad), self.assertRaisesRegex(AssertionError, 'must be a non-empty list of CIDRs'):
                check(bad)

    def test_root_commands_are_system_paths(self):
        """sudo keeps /opt/homebrew/bin first on PATH on a Mac: a bare name under become would run a user-writable binary
        (Homebrew's ssh-keygen also writes host keys to its own etc/, which Apple's sshd never reads)."""
        # tailscaled installs itself as the system daemon: running the Homebrew binary as root is the point
        allowed = {'{{ dotfiles_homebrew_prefix }}/bin/tailscaled'}
        scan, linux_roles = scan_root_commands()
        self.assertEqual(linux_roles, {'system', 'repos_dnf', 'distrobox'})  # site.yml runs these only on Linux
        # LINUX_TERM trusts this name: it must keep ANDing is_linux
        definition = production_task('roles/packages/tasks/main.yml', 'Set Linux x86 work binary install fact')
        self.assertIn('and is_linux', definition['ansible.builtin.set_fact']['_packages_linux_x86_work'])
        self.assertEqual(scan.rejected, [])
        for path, name, command in scan.commands:
            self.assertTrue(command.startswith('/') or command in allowed, f'{path}: {name}: {command}')
        checked = {command for _, _, command in scan.commands}
        for command in ('/usr/bin/ssh-keygen', '/usr/sbin/sshd', '/bin/launchctl', '/usr/bin/pmset', '/bin/cp',
                        '/usr/libexec/ApplicationFirewall/socketfilterfw', '/usr/bin/true'):
            self.assertIn(command, checked)  # the walk reaches the remote-access, power, AeroSpace and probe tasks

    def test_root_command_scan_catches_bare_names(self):
        """Mutation checks: a bare name in each form the scan reads is reported; Linux-only and non-root ones are not."""
        def found(tasks, files=None):
            scan = RootCommandScan(load=lambda path: (files or {})[path])
            scan.walk('roles/x/tasks/main.yml', tasks, False, False)
            return [command for _, _, command in scan.commands if not command.startswith('/')], scan.rejected

        root = {'become': True}
        shell = lambda cmd: [dict(root, **{'ansible.builtin.shell': {'cmd': cmd}})]
        for cmd in ('launchctl list', '/bin/true\nlaunchctl list', '/bin/true; launchctl list', '/bin/true && launchctl list',
                    '/bin/true || launchctl list', '/bin/true | launchctl list', '/bin/true & launchctl list',
                    '/bin/echo "$(launchctl list)"', '/bin/echo `launchctl list`', 'FOO=1 BAR="a b" launchctl list',
                    'if launchctl list; then /bin/true; fi', 'until launchctl list; do /bin/sleep 1; done',
                    '/bin/true 2>&1 | launchctl list', '{{ some_bin }} list', '/bin/echo {{ x | quote }}; launchctl list',
                    '"$cmd" list', 'exec launchctl list', '! launchctl list'):
            with self.subTest(cmd=cmd):
                self.assertEqual(len(found(shell(cmd))[0]), 1, cmd)
        self.assertEqual(found([dict(root, **{'ansible.builtin.shell': 'launchctl list'})])[0], ['launchctl'])
        self.assertEqual(found([dict(root, **{'ansible.builtin.command': 'launchctl list'})])[0], ['launchctl'])
        self.assertEqual(found([dict(root, **{'ansible.builtin.command': {'argv': ['launchctl', 'list']}})])[0], ['launchctl'])
        self.assertEqual(found([dict(root, **{'ansible.builtin.command': None, 'args': {'cmd': 'launchctl list'}})])[0],
                         ['launchctl'])
        self.assertEqual(found([dict(root, **{'ansible.builtin.template': {'src': 'a', 'dest': 'b',
                                                                            'validate': 'sshd -t -f %s'}})])[0], ['sshd'])
        for module in ('raw', 'script', 'command', 'shell', 'ansible.builtin.raw', 'ansible.builtin.script'):
            with self.subTest(module=module):
                self.assertEqual([m for _, _, m in found([dict(root, **{module: '/bin/ls'})])[1]], [module])
        # become from a block, an include site or include_tasks apply
        bare = {'ansible.builtin.command': {'argv': ['launchctl']}}
        self.assertEqual(found([dict(root, block=[bare])])[0], ['launchctl'])
        self.assertEqual(found([{'become': True, 'block': [{'block': [bare]}], 'rescue': [bare]}])[0], ['launchctl'] * 2)
        files = {'roles/x/tasks/sub.yml': [bare]}
        self.assertEqual(found([dict(root, **{'ansible.builtin.import_tasks': 'sub.yml'})], files)[0], ['launchctl'])
        self.assertEqual(found([{'ansible.builtin.include_tasks': {'file': 'sub.yml', 'apply': {'become': True}}}],
                               files)[0], ['launchctl'])
        # not reported: no become, become: false inside a become block, Linux-only (task, block or include site)
        self.assertEqual(found([bare])[0], [])
        self.assertEqual(found([dict(root, block=[dict(bare, become=False)])])[0], [])
        for when in ('is_linux', ['is_macos | default(false) or true', 'is_dnf | bool'], "ansible_facts['system'] == 'Linux'",
                     "ansible_facts['system'] | default('Linux') == 'Linux' and x", 'is_apt', 'not is_macos'):
            with self.subTest(when=when):
                self.assertEqual(found([dict(root, when=when, **bare)])[0], [])
                self.assertEqual(found([dict(root, when=when, block=[bare])])[0], [])
                self.assertEqual(found([dict(root, when=when, **{'ansible.builtin.import_tasks': 'sub.yml'})], files)[0], [])
        # a Linux term ORed with anything else does not limit the task
        for when in ('is_linux or is_macos', 'is_macos', "ansible_facts['system'] == 'Darwin'", 'not is_linux'):
            with self.subTest(when=when):
                self.assertEqual(found([dict(root, when=when, **bare)])[0], ['launchctl'])
        # become fails closed: anything not clearly false is root, on a task, a block or include_tasks apply
        for value in (True, 'True', 'yes', 'on', 1, '{{ needs_root }}', "{{ x | default(false) }}", None, 'maybe'):
            with self.subTest(become=value):
                self.assertEqual(found([dict(bare, become=value)])[0], ['launchctl'])
                self.assertEqual(found([{'become': value, 'block': [bare]}])[0], ['launchctl'])
                self.assertEqual(found([{'ansible.builtin.include_tasks': {'file': 'sub.yml', 'apply': {'become': value}}}],
                                       files)[0], ['launchctl'])
        for value in (False, 'false', 'False', 'no', 'NO', 'off', 0, '0', 'n'):
            with self.subTest(become=value):
                self.assertEqual(found([dict(bare, become=value)])[0], [])
                self.assertEqual(found([dict(root, block=[dict(bare, become=value)])])[0], [])
        # include_role and import_role: tasks_from, apply, the include site's become and when
        files = {'roles/y/tasks/main.yml': [bare], 'roles/y/tasks/other.yml': [{'ansible.builtin.shell': 'killall x'}]}
        for module in INCLUDE_ROLE_MODULES:
            with self.subTest(module=module):
                self.assertEqual(found([dict(root, **{module: {'name': 'y'}})], files)[0], ['launchctl'])
                self.assertEqual(found([dict(root, **{module: {'name': 'y', 'tasks_from': 'other'}})], files)[0], ['killall'])
                self.assertEqual(found([{module: {'name': 'y'}}], files)[0], [])
                self.assertEqual(found([dict(root, when='is_linux', **{module: {'name': 'y'}})], files)[0], [])
                with self.assertRaisesRegex(AssertionError, 'cannot resolve role'):
                    found([dict(root, **{module: {'name': '{{ role_name_var }}'}})], files)
        self.assertEqual(found([{'ansible.builtin.include_role': {'name': 'y', 'apply': {'become': True}}}], files)[0],
                         ['launchctl'])
        self.assertEqual(found([dict(root, **{'ansible.builtin.include_role': 'y'})], files)[0], ['launchctl'])

    def test_root_command_scan_follows_site_role_become(self):
        """A role site.yml runs with become (on the role, or from the play) is scanned as root; Linux-only only when
        every entry of the role is."""
        roles = site_roles([{'become': True, 'roles': [{'role': 'a'}, {'role': 'b', 'become': False}]},
                            {'roles': ['c', {'role': 'd', 'become': '{{ x }}'}, {'role': 'e', 'when': 'is_linux'},
                                       {'role': 'f', 'when': 'is_linux', 'become': 'True'}, {'role': 'f'}]}])
        self.assertEqual(roles, {'a': (True, False), 'b': (False, False), 'c': (False, False), 'd': (True, False),
                                 'e': (False, True), 'f': (True, False)})
        site = yaml.safe_load((ROOT / 'site.yml').read_text())
        bare = lambda scan: sorted({c for _, _, c in scan.commands if not c.startswith('/') and '{{' not in c})
        self.assertEqual(bare(scan_root_commands(site)[0]), [])
        for play in site:
            for entry in play.get('roles', []):
                if entry['role'] == 'desktop':
                    entry['become'] = True
        self.assertIn('killall', bare(scan_root_commands(site)[0]))  # the desktop role's non-root tasks


def find_task(tasks, name):
    """The task called name in a task list, looking inside blocks."""
    for task in tasks:
        if task.get('name') == name:
            return task
        for key in ('block', 'rescue', 'always'):
            found = find_task(task.get(key) or [], name)
            if found:
                return found
    return None


class YesNoOptionTests(unittest.TestCase):
    """An unquoted yes/no/true/false/on/off in config.yml is a YAML boolean. Options that take yes/no words must be
    written as the word (not Python's True/False), and their asserts must take booleans and refuse other words."""

    # Unquoted, as config.yml has them (YAML 1.1: all booleans), then quoted strings
    CASES = """cases:
  - {id: u_yes, value: yes, want: 'yes'}
  - {id: u_no, value: no, want: 'no'}
  - {id: u_true, value: true, want: 'yes'}
  - {id: u_false, value: false, want: 'no'}
  - {id: u_on, value: on, want: 'yes'}
  - {id: u_off, value: off, want: 'no'}
  - {id: u_cap, value: No, want: 'no'}
  - {id: q_yes, value: 'yes', want: 'yes'}
  - {id: q_no, value: "no", want: 'no'}
"""
    # option: (file, render via, {var: line prefix}, extra quoted words it takes as (value, written as),
    #          asserting task (file, name))
    OPTIONS = {
        'llmnr': ('roles/system/tasks/dns_over_tls.yml', 'copy', {
            'system_resolved_llmnr': 'LLMNR=', 'system_resolved_multicast_dns': 'MulticastDNS='},
            [('resolve', 'resolve'), ('Resolve', 'resolve'), ('YES', 'yes')],  # resolved takes lowercase resolve only
            ('common/tasks/pre_flight_checks.yml',
             'Assert system_resolved_llmnr and system_resolved_multicast_dns are valid enums')),
        'coredump': ('roles/system/templates/coredump.conf.j2', 'template', {'system_coredump_compress': 'Compress='},
                     [], ('roles/system/tasks/coredumps.yml', 'Assert system_coredump_compress is a boolean')),
        'journald': ('roles/system/templates/journald-99-hardening.conf.j2', 'template',
                     {'system_journald_compress': 'Compress='},
                     [('8K', '8K'), ('512', '512'), ('8B', '8B'), ('1.5M', '1.5M'), ('TRUE', 'TRUE')],
                     ('roles/system/tasks/journald.yml', 'Assert system_journald_compress is a boolean or a size')),
        'sshd_agent': ('roles/system/templates/sshd-hardening.conf.j2', 'template',
                       {'system_ssh_allow_agent_forwarding': 'AllowAgentForwarding '}, [],
                       ('roles/system/tasks/sshd.yml', 'Assert system_ssh_allow_agent_forwarding is a recognised sshd value')),
        'sshd_tcp': ('roles/system/templates/sshd-hardening.conf.j2', 'template',
                     {'system_ssh_allow_tcp_forwarding': 'AllowTcpForwarding '}, [('local', 'local')],
                     ('roles/system/tasks/sshd.yml', 'Assert system_ssh_allow_tcp_forwarding is a recognised sshd value')),
        'macos_sshd': ('roles/desktop/templates/sshd-macos.conf.j2', 'template', {
            'desktop_macos_ssh_allow_agent_forwarding': 'AllowAgentForwarding ',
            'desktop_macos_ssh_allow_tcp_forwarding': 'AllowTcpForwarding '}, [],
            ('roles/desktop/tasks/main.yml', 'Check the sshd forwarding settings are sshd words (macOS)')),
        # the macOS settings default to the Linux ones: a boolean set there reaches the Mac's drop-in
        'macos_inherit': ('roles/desktop/templates/sshd-macos.conf.j2', 'template', {
            'system_ssh_allow_agent_forwarding': 'AllowAgentForwarding ',
            'system_ssh_allow_tcp_forwarding': 'AllowTcpForwarding '}, [], None),
        # sway's focus_follows_mouse takes yes, no or always; '' (the default) leaves it out
        'sway_focus': ('roles/desktop/templates/sway.config.j2', 'template',
                       {'desktop_sway_focus_follows_mouse': 'focus_follows_mouse '}, [('always', 'always')], None),
    }
    BAD = ['maybe', 'True ', 'yes no']  # words none of these options take
    REFUSED = {'journald': ['8k']}  # systemd ignores a lowercase size suffix

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = cls.out = Path(cls.tmp.name)
        cases = yaml.safe_load(cls.CASES)['cases']
        (out / 'cases.yml').write_text(cls.CASES)
        tasks = []
        for option, (src, kind, variables, extra, check) in cls.OPTIONS.items():
            items = '{{ cases + %s }}' % ([{'id': f'q_{w}', 'value': w, 'want': want} for w, want in extra])
            item_vars = {var: '{{ item.value }}' for var in variables}
            if kind == 'copy':
                task = dict(find_task(yaml.safe_load((ROOT / src).read_text()), 'Disable LLMNR and mDNS via resolved drop-in'))
                for key in ('notify', 'when'):
                    task.pop(key, None)
                task['ansible.builtin.copy'] = dict(task['ansible.builtin.copy'], dest=f'{out}/{option}-{{{{ item.id }}}}')
            else:
                task = {'ansible.builtin.template': {'src': str(ROOT / src), 'dest': f'{out}/{option}-{{{{ item.id }}}}',
                                                     'mode': '0600'}}
            # a failed render leaves its file out, which fails that option's test only
            tasks.append(dict(task, name=f'render {option}', vars=item_vars, loop=items, ignore_errors=True))
            if check:
                path, name = check
                found = find_task(yaml.safe_load((ROOT / path).read_text()), name)
                if found is None:  # no assert for this option: record that, the test fails on it
                    (out / f'{option}-asserts.json').write_text('null')
                    continue
                assert_task = {k: v for k, v in found.items() if k not in ('when', 'name')}
                assert_task['vars'] = dict(assert_task.get('vars') or {}, **item_vars)
                bad = '{{ cases + %s }}' % ([{'id': 'bad', 'value': w} for w in cls.BAD + cls.REFUSED.get(option, [])]
                                            + [{'id': 'extra', 'value': w} for w, _ in extra])
                tasks += [dict(assert_task, name=f'assert {option}', loop=bad, ignore_errors=True, register='_checked'),
                          {'name': f'record {option}', 'ansible.builtin.copy': {
                              'dest': f'{out}/{option}-asserts.json', 'mode': '0600',
                              'content': "{{ _checked.results | map(attribute='failed') | list | to_json }}"}}]
        play = [{'hosts': 'localhost', 'gather_facts': True, 'gather_subset': ['min'],
                 'vars_files': [str(ROOT / p) for p in VARS_FILES] + [str(out / 'cases.yml')],
                 'vars': BASE, 'tasks': tasks}]
        (out / 'play.yml').write_text(yaml.safe_dump(play))
        result = subprocess.run(
            [str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(out / 'play.yml')], cwd=ROOT,
            env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
            capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AssertionError(result.stdout[-3000:] + result.stderr)
        cls.cases = cases

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_options_are_written_as_yes_or_no(self):
        for option, (_, _, variables, extra, _) in self.OPTIONS.items():
            for case in self.cases + [{'id': f'q_{w}', 'want': want} for w, want in extra]:
                rendered = self.out / f'{option}-{case["id"]}'
                lines = rendered.read_text().splitlines() if rendered.exists() else ['(render failed)']
                for prefix in variables.values():
                    with self.subTest(option=option, case=case['id'], line=prefix):
                        self.assertIn(prefix + case['want'], lines)

    def test_asserts_take_booleans_and_refuse_other_words(self):
        for option, (_, _, _, extra, check) in self.OPTIONS.items():
            if not check:
                continue
            with self.subTest(option=option):
                failed = json.loads((self.out / f'{option}-asserts.json').read_text())
                self.assertIsNotNone(failed, f'no assert called {check[1]!r} in {check[0]}')
                self.assertEqual(failed, [False] * len(self.cases) + [True] * len(self.BAD + self.REFUSED.get(option, []))
                                 + [False] * len(extra))



class WindowManagerConfigsManaged(unittest.TestCase):
    """The i3, Sway and AeroSpace configs follow their settings on every run (hand edits kept as backups): a config
    seeded once (force: false) keeps the values of its first run, so a later desktop_wm_idle_lock_timeout never applies."""

    def test_window_manager_configs_are_rewritten_with_a_backup(self):
        def tasks(items):
            for item in items or []:
                yield item
                yield from tasks(item.get('block'))
        found = {}
        for task in tasks(yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())):
            module = task.get('ansible.builtin.template') or {}
            if module.get('src') in ('i3.config.j2', 'sway.config.j2', 'aerospace.toml.j2'):
                found[module['src']] = module
        self.assertEqual(sorted(found), ['aerospace.toml.j2', 'i3.config.j2', 'sway.config.j2'])
        for src, module in found.items():
            with self.subTest(src=src):
                self.assertNotEqual(module.get('force'), False)
                self.assertIs(module.get('backup'), True)


if __name__ == '__main__':
    unittest.main()
