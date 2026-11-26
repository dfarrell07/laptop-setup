#!/usr/bin/env python3
"""Repos whose URLs come from config.yml cannot add unsigned code: run the real repos_dnf and packages tasks on localhost.

The tasks run inside a scratch copy of their role, so role_path and variable precedence are as in production: role
defaults come from a defaults file, config.yml values are set as facts from the parsed file (as common/tasks/pre_flight_checks.yml
does, precedence 19), group_vars come from the inventory and -e from the command line. Package-manager modules (yum_repository, dnf,
dnf_versionlock, package_facts) are replaced by stubs that log their arguments, become is dropped, and curl, rpm and dnf
are shims on PATH (rpm passes scratch-rpmdb calls through to the real rpm), so nothing touches /etc/yum.repos.d, the
system rpmdb or the network. gpg is the real one, so the fingerprint checks run as in production.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
# The real tools, found before the shims go first on PATH. The key check needs rpm (a scratch rpmdb) and gpg, and the
# tasks under test are dnf-only: on macOS, or without either tool, the suite is skipped.
GPG = shutil.which('gpg')
RPM = shutil.which('rpm')
TEST_TIMEOUT = max(120, int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0')))
REPOS_DNF = ROOT / 'roles/repos_dnf'
STANDALONE = ROOT / 'roles/packages/tasks/install_standalone_binaries.yml'
KEY_DIR = '/etc/pki/rpm-gpg/RPM-GPG-KEY-laptop-setup-'  # the fixed key files: redhat-internal, tailscale
REPOS_DIR = '/etc/yum.repos.d/'
CHROME_DEFAULTS = '/etc/default/google-chrome'
STUBBED = ('ansible.builtin.yum_repository', 'ansible.builtin.dnf', 'community.general.dnf_versionlock',
           'ansible.builtin.package_facts')

SHIMS = {
    'curl': """#!/bin/sh
out=
while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift ;; esac; shift; done
echo "$out" >> "$TEST_LOG_DIR/curl"
cp "$TEST_SERVED_KEY" "$out"
""",
    # scratch-rpmdb calls (the key check) run the real rpm; nothing reaches the system rpmdb
    'rpm': """#!/bin/sh
case " $* " in *" --dbpath "*) exec @RPM@ "$@" ;; esac
case "$1" in
  --import)
    echo "$2" >> "$TEST_LOG_DIR/rpm-import"
    cp "$2" "$TEST_LOG_DIR/imported-$(wc -l < "$TEST_LOG_DIR/rpm-import")"
    exit 0 ;;
  --version) echo "RPM version ${TEST_RPM_VERSION:-6.0.2}"; exit 0 ;;
  -e) echo "$2" >> "$TEST_LOG_DIR/rpm-erase"; exit 0 ;;
esac
# TEST_IMPORTED: the keyring's gpg-pubkey versions (rpm 6: full fingerprints, older rpm: 8-hex key IDs)
case " $* " in *" gpg-pubkey "*) if [ -n "${TEST_IMPORTED:-}" ]; then
  for v in $TEST_IMPORTED; do case "$*" in *RELEASE*) echo "$v-5f000000" ;; *) echo "$v" ;; esac; done; exit 0
fi ;; esac
case " $* " in *" google-cloud-cli "*) printf '600.0.0'; exit 0 ;; esac  # installed above the version floor
exit 1  # rpm -q: nothing else installed, no gpg-pubkey imported
""",
    # minimal images have no cmp (diffutils): the key check must not depend on it
    'cmp': """#!/bin/sh
echo "cmp: command not found" >&2
exit 127
""",
    'dnf': """#!/bin/sh
[ "$1 $2" = "versionlock list" ] && printf '%s\\n' "${TEST_VERSIONLOCK:-}"
exit 0
""",
    # TEST_GPG_HIDE_SECRET models a gpg that does not report secret packets, to exercise the gpg/rpm cross-check
    'gpg': """#!/bin/sh
if [ -n "${TEST_GPG_HIDE_SECRET:-}" ]; then @GPG@ "$@" | grep -v -e '^sec:' -e '^ssb:'; exit 0; fi
exec @GPG@ "$@"
""",
}


def gpg(home, *args):
    return subprocess.run([GPG, '--batch', '--homedir', str(home), *args], check=True, capture_output=True,
                          timeout=TEST_TIMEOUT).stdout


def tasks_named(path, *names):
    tasks = yaml.safe_load(path.read_text())
    return [task for task in tasks if task.get('name') in names] if names else tasks


class Harness(unittest.TestCase):
    LINUX_ONLY = True  # the dnf tasks and the key check (rpm in a scratch rpmdb); a class that needs neither clears it

    @classmethod
    def setUpClass(cls):
        if cls.LINUX_ONLY and platform.system() == 'Darwin':
            raise unittest.SkipTest('repos_dnf and the dnf acli install are Linux-only')
        if not GPG or (cls.LINUX_ONLY and not RPM):
            raise unittest.SkipTest('needs gpg' + (' and rpm (the key check imports into a scratch rpmdb)' if cls.LINUX_ONLY else ''))
        cls.tmp = Path(tempfile.mkdtemp(prefix='test-repos-dnf-'))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.gnupg = cls.tmp / 'gnupg'
        cls.gnupg.mkdir(mode=0o700)
        cls.addClassCleanup(subprocess.run, ['gpgconf', '--homedir', str(cls.gnupg), '--kill', 'all'],
                            capture_output=True, check=False)
        cls.fpr, cls.key = {}, {}
        for name in ('A', 'B'):
            gpg(cls.gnupg, '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-gen-key',
                f'Test {name} <{name.lower()}@example.invalid>', 'ed25519', 'sign', 'never')
            listing = gpg(cls.gnupg, '--with-colons', '--list-keys', f'{name.lower()}@example.invalid').decode()
            cls.fpr[name] = next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
            cls.key[name] = cls.tmp / f'{name}.asc'
            cls.key[name].write_bytes(gpg(cls.gnupg, '--armor', '--export', cls.fpr[name]))
        cls.key['AB'] = cls.tmp / 'AB.asc'
        cls.key['AB'].write_bytes(gpg(cls.gnupg, '--armor', '--export', cls.fpr['A'], cls.fpr['B']))
        # B's secret key relabelled as a public key block: gpg lists it as "sec", rpm 6 imports it as a public key
        secret = gpg(cls.gnupg, '--pinentry-mode', 'loopback', '--passphrase', '', '--armor', '--export-secret-keys',
                     cls.fpr['B']).decode().replace('PRIVATE KEY BLOCK', 'PUBLIC KEY BLOCK')
        cls.key['A+Bsecret'] = cls.tmp / 'A+Bsecret.asc'
        cls.key['A+Bsecret'].write_text(cls.key['A'].read_text() + secret)
        cls.key['A.bin'] = cls.tmp / 'A.bin'
        cls.key['A.bin'].write_bytes(gpg(cls.gnupg, '--export', cls.fpr['A']))
        # A with a new signing subkey (a rotation under the same primary key, as Google does)
        gpg(cls.gnupg, '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-add-key', cls.fpr['A'], 'ed25519', 'sign')
        cls.key['A-rotated'] = cls.tmp / 'A-rotated.asc'
        cls.key['A-rotated'].write_bytes(gpg(cls.gnupg, '--armor', '--export', cls.fpr['A']))
        cls.shims = cls.tmp / 'bin'
        cls.shims.mkdir()
        for name, body in SHIMS.items():
            (cls.shims / name).write_text(body.replace('@RPM@', RPM or 'false').replace('@GPG@', GPG))
            (cls.shims / name).chmod(0o755)

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp(dir=self.tmp))
        self.log = self.run_dir / 'modules.jsonl'
        self.key_dest = self.run_dir / 'key-redhat-internal'
        self.repos_dir = self.run_dir / 'repos.d'
        self.repos_dir.mkdir()
        self.redhat_repo_file = self.repos_dir / 'redhat-internal.repo'
        self.chrome_defaults = self.run_dir / 'default-google-chrome'

    def stub(self, node, overrides):
        """Swap package-manager modules for a log line, drop become, point the get_url download at the fixture."""
        if isinstance(node, list):
            return [self.stub(item, overrides) for item in node]
        task = dict(node)
        task.pop('become', None)
        for module in ('ansible.builtin.lineinfile', 'ansible.builtin.copy', 'ansible.builtin.file'):
            if isinstance(task.get(module), dict):  # not root here: files stay the test user's
                task[module] = {k: v for k, v in task[module].items() if k not in ('owner', 'group')}
        for key in ('block', 'rescue', 'always'):
            if key in task:
                task[key] = self.stub(task[key], overrides)
        if 'ansible.builtin.get_url' in task and 'url' in overrides:
            task['ansible.builtin.get_url'] = dict(task['ansible.builtin.get_url'], url=overrides['url'])
        for module in STUBBED:
            if module in task:
                task['vars'] = dict(task.get('vars') or {}, _stub_args=task.pop(module))
                task['ansible.builtin.lineinfile'] = {
                    'path': str(self.log), 'create': True, 'mode': '0600',
                    'line': '{{ {"module": "%s", "args": _stub_args} | to_json }}' % module}
        return task

    def play(self, role, tasks, defaults, config=None, group_vars=None, extra=None, served_key=None, overrides=None,
             facts=False, env=None):
        """Run tasks as role `role` (a scratch copy). defaults: the role defaults; config: set as facts, as production does."""
        role_dir = self.run_dir / 'roles' / role
        (role_dir / 'tasks').mkdir(parents=True)
        (role_dir / 'defaults').mkdir()
        if role == 'repos_dnf':
            for path in ('tasks/import_gpg_key.yml', 'files'):  # the include file exists only in older versions
                if (REPOS_DNF / path).is_dir():
                    shutil.copytree(REPOS_DNF / path, role_dir / path)
                elif (REPOS_DNF / path).exists():
                    shutil.copy(REPOS_DNF / path, role_dir / path)
        # package_facts is stubbed: the cleanup tasks read an empty package list instead
        text = yaml.safe_dump(self.stub(tasks, overrides or {}), allow_unicode=True, width=1000)
        text = text.replace('ansible_facts.packages', '_test_packages').replace(KEY_DIR, f'{self.run_dir}/key-')
        text = text.replace(REPOS_DIR, f'{self.repos_dir}/').replace(CHROME_DEFAULTS, str(self.chrome_defaults))
        (role_dir / 'tasks/main.yml').write_text(text)
        (role_dir / 'defaults/main.yml').write_text(yaml.safe_dump(
            dict({'_test_packages': {}, 'common_project_root': str(ROOT)}, **defaults), width=1000))
        inventory = self.run_dir / 'inventory'
        (inventory / 'group_vars').mkdir(parents=True)
        (inventory / 'hosts').write_text('localhost ansible_connection=local\n')
        if group_vars:
            (inventory / 'group_vars/all.yml').write_text(yaml.safe_dump(group_vars))
        play = {'name': 'repos_dnf signing regression', 'hosts': 'localhost', 'gather_facts': facts, 'roles': [role]}
        if facts:
            play['gather_subset'] = ['!all', 'min']
        if config is not None:
            (self.run_dir / 'config.yml').write_text(yaml.safe_dump(config, width=1000))
            play['pre_tasks'] = [
                {'name': 'Read config.yml', 'ansible.builtin.include_vars': {'file': str(self.run_dir / 'config.yml'),
                                                                             'name': '_test_config'}},
                {'name': 'Load user overrides from the parsed config', 'ansible.builtin.set_fact': {
                    '{{ _test_config_item.key }}': '{{ _test_config_item.value }}'},
                 'loop': '{{ _test_config | dict2items }}', 'loop_control': {'loop_var': '_test_config_item'}}]
        playbook = self.run_dir / 'play.yml'
        playbook.write_text(yaml.safe_dump([play], allow_unicode=True, width=1000))
        environment = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'),
                           ANSIBLE_ROLES_PATH=str(self.run_dir / 'roles'),
                           PATH=f'{self.shims}:{os.environ["PATH"]}', GNUPGHOME=str(self.gnupg),
                           TEST_LOG_DIR=str(self.run_dir), TEST_SERVED_KEY=str(served_key or os.devnull), **(env or {}))
        environment.pop('SSH_CONNECTION', None)
        command = [str(ANSIBLE), '-i', str(inventory / 'hosts'), str(playbook)]
        if extra:
            command += ['-e', json.dumps(extra)]
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=TEST_TIMEOUT)
        result.output = result.stdout + result.stderr
        return result

    def calls(self, module=None):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [call['args'] for call in map(json.loads, lines) if module is None or call['module'] == module]

    def imported_keys(self):
        """The bytes of each key file handed to the system rpm --import, in order."""
        return [(self.run_dir / f'imported-{n}').read_bytes() for n in range(1, len(self.imports()) + 1)]

    def erased(self):
        path = self.run_dir / 'rpm-erase'
        return path.read_text().splitlines() if path.exists() else []

    def imports(self):
        path = self.run_dir / 'rpm-import'
        return path.read_text().splitlines() if path.exists() else []

    def assert_refused(self, result, message=None):
        self.assertEqual(result.returncode, 2, result.output)
        self.assertNotRegex(result.output, r'(?i)is undefined|templating error|syntax error|error while evaluating')
        if message:
            self.assertIn(message, result.output)
        self.assertEqual(self.imports(), [], 'no key may be imported')
        self.assertEqual(self.calls('ansible.builtin.yum_repository'), [], 'no repo may be written')


class RedHatInternalRepoTests(Harness):
    def redhat(self, trusted, served, config=None, **kwargs):
        """Run the Red Hat internal repo block. trusted: the pins in the tracked defaults file; config: config.yml."""
        defaults = {'repo_redhat': True, 'profile': 'work', 'csb_rhel': False, 'csb_detected': False,
                    'repos_dnf_internal_hosts': [],
                    'repos_dnf_redhat_trusted_fingerprints': trusted, 'repos_dnf_redhat_packages': [],
                    'repos_dnf_redhat_repo_gpgcheck': True}
        base_config = {
            'repos_dnf_internal_hosts': ['pkgs.example.internal'],
            'repos_dnf_redhat_baseurl': 'https://pkgs.example.internal/repo/',
            'repos_dnf_redhat_gpgkey': 'https://pkgs.example.internal/repo/key.gpg',
            'repos_dnf_redhat_packages': ['rhpkg'],
            # the earlier design took the fingerprint from config.yml: an attacker sets it to their own key
            'repos_dnf_redhat_gpg_fingerprint': self.fpr['B'],
        }
        defaults.update(kwargs.pop('defaults', {}))
        return self.play('repos_dnf', tasks_named(REPOS_DNF / 'tasks/main.yml', 'Add Red Hat internal repos'), defaults,
                         config=dict(base_config, **(config or {})), served_key=self.key[served], **kwargs)

    def test_config_key_without_tracked_pin_refused(self):
        self.assert_refused(self.redhat([], 'B'), 'roles/repos_dnf/defaults/main.yml')

    def test_key_not_matching_tracked_pin_refused(self):
        self.assert_refused(self.redhat([self.fpr['A']], 'B'), f'[{self.fpr["B"]}]')

    def test_pin_override_from_config_refused(self):
        result = self.redhat([self.fpr['A']], 'B', config={'repos_dnf_redhat_trusted_fingerprints': [self.fpr['B']]})
        self.assert_refused(result, 'must not be set in config.yml')

    def test_pin_override_from_group_vars_and_intermediate_vars_refused(self):
        attack = [self.fpr['B']]
        result = self.redhat([], 'B', config={'_redhat_trusted_tracked': attack, '_tracked': attack,
                                              '_gpg_fingerprints': attack},
                             group_vars={'repos_dnf_redhat_trusted_fingerprints': attack})
        self.assert_refused(result)

    def test_pin_override_from_extra_vars_refused(self):
        attack = [self.fpr['B']]
        result = self.redhat([self.fpr['A']], 'B', extra={'repos_dnf_redhat_trusted_fingerprints': attack,
                                                          '_redhat_trusted_tracked': attack, '_tracked': attack,
                                                          '_gpg_fingerprints': attack})
        self.assert_refused(result)

    def test_key_path_from_config_cannot_inject_commands(self):
        marker = self.run_dir / 'PWNED'
        injection = f"/x'; touch {marker}; echo '"
        result = self.redhat([self.fpr['A']], 'A', config={'_redhat_gpg_dest': injection, '_gpg_dest': injection,
                                                           'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']})
        self.assertEqual(result.returncode, 0, result.output)
        self.assertFalse(marker.exists(), 'a config.yml value ran as a shell command')
        self.assertEqual(self.imported_keys(), [self.key_dest.read_bytes()])
        [repo] = self.calls('ansible.builtin.yum_repository')
        self.assertEqual(repo['gpgkey'], f'file://{self.key_dest}')

    def test_host_with_shell_metacharacters_refused(self):
        marker = self.run_dir / 'PWNED'
        host = f'x$(touch {marker})'
        result = self.redhat([self.fpr['A']], 'A', config={
            'repos_dnf_internal_hosts': [host], 'repos_dnf_redhat_baseurl': f'https://{host}/repo/',
            'repos_dnf_redhat_gpgkey': f'https://{host}/key.gpg', 'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']})
        self.assertFalse(marker.exists(), 'a config.yml host ran as a shell command')
        self.assert_refused(result, 'repos_dnf_internal_hosts')

    def test_key_file_with_an_extra_unpinned_key_refused(self):
        self.assert_refused(self.redhat([self.fpr['A']], 'AB', config={'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']}),
                            self.fpr['B'])

    def test_secret_key_relabelled_as_public_refused(self):
        result = self.redhat([self.fpr['A']], 'A+Bsecret', config={'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']})
        self.assert_refused(result, 'secret key material')

    def test_gpg_and_rpm_disagreeing_refused(self):
        result = self.redhat([self.fpr['A']], 'A+Bsecret', config={'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']},
                             env={'TEST_GPG_HIDE_SECRET': '1'})
        # rpm 6 imports the secret packets as a key, so the sets differ; rpm 4.x rejects the file outright.
        # Either way the key is refused before anything is imported or written.
        self.assert_refused(result)
        self.assertRegex(result.output, r'gpg and rpm see different keys|rpm does not accept the key at .* as public keys only')

    def test_binary_key_refused_with_a_clear_message(self):
        result = self.redhat([self.fpr['A']], 'A.bin', config={'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']})
        self.assert_refused(result, 'not an ASCII-armored public key block')

    def test_package_globs_refused(self):
        self.assert_refused(self.redhat([self.fpr['A']], 'A', config={'repos_dnf_redhat_gpg_fingerprint': self.fpr['A'],
                                                                      'repos_dnf_redhat_packages': ['*']}),
                            'repos_dnf_redhat_packages')

    def test_pinned_key_gives_a_signed_repo_limited_to_named_packages(self):
        result = self.redhat([self.fpr['A']], 'A', config={'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']})
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(self.imported_keys(), [self.key_dest.read_bytes()])
        self.assertEqual(self.key_dest.read_bytes(), self.key['A'].read_bytes())
        [repo] = self.calls('ansible.builtin.yum_repository')
        self.assertIs(repo['gpgcheck'], True)
        self.assertIs(repo['repo_gpgcheck'], True)
        self.assertEqual(repo['includepkgs'], ['rhpkg'])
        self.assertEqual(repo['gpgkey'], f'file://{self.key_dest}')  # never the configured URL, which dnf would trust


    def test_unpinned_repo_file_from_before_is_removed_before_an_abort(self):
        """A redhat-internal.repo trusting a config.yml key URL goes even when the empty pin list then stops the run."""
        self.redhat_repo_file.write_text('[redhat-internal]\nbaseurl = https://pkgs.example.internal/repo/\ngpgcheck = 1\n'
                                         'gpgkey = https://pkgs.example.internal/repo/key.gpg\nenabled = 1\n')
        result = self.redhat([], 'B')
        self.assertEqual(result.returncode, 2, result.output)
        self.assertEqual(self.calls('ansible.builtin.yum_repository'), [{'name': 'redhat-internal', 'state': 'absent'}])
        self.assertEqual(self.imports(), [])

    def test_csb_repo_file_is_left_alone_when_the_run_stops(self):
        """On CSB redhat-internal.repo may be IT's: nothing removes it, and the empty pin list writes nothing either."""
        self.redhat_repo_file.write_text('[redhat-internal]\nbaseurl = https://it.example.internal/repo/\ngpgcheck = 1\n'
                                         'gpgkey = file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release\n')
        result = self.redhat([], 'A', defaults={'csb_detected': True, 'csb_rhel': True})
        self.assertEqual(result.returncode, 2, result.output)
        self.assertEqual(self.calls('ansible.builtin.yum_repository'), [])
        self.assertEqual(self.imports(), [])

    def test_repo_file_in_the_pinned_shape_is_kept(self):
        self.redhat_repo_file.write_text(f'[redhat-internal]\nbaseurl = https://pkgs.example.internal/repo/\ngpgcheck = 1\n'
                                         f'gpgkey = file://{self.key_dest}\nincludepkgs = rhpkg\nenabled = 1\n')
        result = self.redhat([self.fpr['A']], 'A', config={'repos_dnf_redhat_gpg_fingerprint': self.fpr['A']})
        self.assertEqual(result.returncode, 0, result.output)
        self.assertNotIn({'name': 'redhat-internal', 'state': 'absent'}, self.calls('ansible.builtin.yum_repository'))


class PublicRepoTests(Harness):
    """Tailscale and RPM Fusion: fixed URLs and pins in the tracked defaults file; config.yml must not change either."""
    BASE = {'is_fedora': True, 'csb_detected': False, 'csb_rhel': False, 'repo_tailscale': True,
            'repo_rpmfusion_free': True}

    def run_block(self, name, served, config, env=None):
        defaults = dict(self.BASE, repos_dnf_tailscale_gpg_fingerprint=self.fpr['A'],
                        repos_dnf_rpmfusion_gpg_fingerprint=self.fpr['A'])
        return self.play('repos_dnf', tasks_named(REPOS_DNF / 'tasks/main.yml', name), defaults, config=config,
                         served_key=self.key[served] if served else self.run_dir / 'offline', facts=True, env=env)

    def test_rotated_subkey_in_a_refetched_key_is_reimported(self):
        """The key file is fetched every run: a new subkey under the pinned primary key reaches the keyring.

        rpm 6 merges it on import; older rpm keeps the installed copy, so that copy (exactly the pinned primary) is
        removed first."""
        short = self.fpr['A'][-8:].lower()
        for version, installed, erased in [('6.0.2', self.fpr['A'].lower(), []),
                                           ('4.19.1', f'{short} deadbeef', [f'gpg-pubkey-{short}-5f000000']),
                                           ('4.16.1', f'deadbeef {short}', [f'gpg-pubkey-{short}-5f000000'])]:
            with self.subTest(rpm=version):
                self.setUp()
                key_file = self.run_dir / 'key-tailscale'
                key_file.write_bytes(self.key['A'].read_bytes())
                result = self.run_block('Add Tailscale repo', 'A-rotated', {},
                                        env={'TEST_IMPORTED': installed, 'TEST_RPM_VERSION': version})
                self.assertEqual(result.returncode, 0, result.output)
                self.assertEqual(self.erased(), erased)
                self.assertEqual(self.imported_keys(), [self.key['A-rotated'].read_bytes()])
                self.assertEqual(key_file.read_bytes(), self.key['A-rotated'].read_bytes())

    def test_unchanged_key_is_not_reimported_without_cmp(self):
        key_file = self.run_dir / 'key-tailscale'
        key_file.write_bytes(self.key['A'].read_bytes())
        result = self.run_block('Add Tailscale repo', 'A', {}, env={'TEST_IMPORTED': self.fpr['A'].lower()})
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(self.imports(), [])
        self.assertEqual(self.erased(), [])

    def test_offline_rerun_keeps_the_checked_key(self):
        key_file = self.run_dir / 'key-tailscale'
        key_file.write_bytes(self.key['A'].read_bytes())
        result = self.run_block('Add Tailscale repo', None, {}, env={'TEST_IMPORTED': self.fpr['A'].lower()})
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(key_file.read_bytes(), self.key['A'].read_bytes())
        self.assertEqual(self.imports(), [])
        [repo] = self.calls('ansible.builtin.yum_repository')
        self.assertEqual(repo['gpgkey'], f'file://{key_file}')

    def test_tailscale_path_from_config_cannot_add_a_second_url(self):
        result = self.run_block('Add Tailscale repo', 'A', {'_ts_path': 'fedora/repo.gpg?,https://evil.example/x'})
        self.assertEqual(result.returncode, 0, result.output)
        [repo] = self.calls('ansible.builtin.yum_repository')
        self.assertEqual(repo['baseurl'], 'https://pkgs.tailscale.com/stable/fedora/$basearch')
        self.assertEqual(repo['gpgkey'], f'file://{self.run_dir}/key-tailscale')

    def test_tailscale_repo_file_without_the_pinned_shape_is_removed_before_an_abort(self):
        repo_file = self.repos_dir / 'tailscale-stable.repo'
        repo_file.write_text('[tailscale-stable]\nbaseurl = https://pkgs.tailscale.com/stable/fedora/$basearch\ngpgcheck = 1\n'
                             'gpgkey = https://pkgs.tailscale.com/stable/fedora/repo.gpg\n')
        result = self.run_block('Add Tailscale repo', 'B', {})
        self.assertEqual(result.returncode, 2, result.output)
        self.assertEqual(self.calls('ansible.builtin.yum_repository'), [{'name': 'tailscale-stable', 'state': 'absent'}])
        self.setUp()  # the shape this role writes is kept
        (self.repos_dir / 'tailscale-stable.repo').write_text(
            f'[tailscale-stable]\ngpgcheck = 1\ngpgkey = file://{self.run_dir}/key-tailscale\nincludepkgs = tailscale\n')
        result = self.run_block('Add Tailscale repo', 'A', {})
        self.assertEqual(result.returncode, 0, result.output)
        [repo] = self.calls('ansible.builtin.yum_repository')
        self.assertEqual(repo['includepkgs'], ['tailscale'])

    def test_tailscale_pin_override_from_config_refused(self):
        result = self.run_block('Add Tailscale repo', 'B', {'repos_dnf_tailscale_gpg_fingerprint': self.fpr['B']})
        self.assert_refused(result, 'must not be overridden')

    def test_rpmfusion_pin_override_from_config_refused(self):
        result = self.run_block('Enable RPM Fusion free repo', 'B', {'repos_dnf_rpmfusion_gpg_fingerprint': self.fpr['B']})
        self.assert_refused(result, 'must not be overridden')
        self.assertEqual(self.calls('ansible.builtin.dnf'), [])

    def test_tailscale_repo_trusts_the_checked_key_file(self):
        """gpgkey is the local file holding the checked bytes, so dnf never imports a later key from the URL."""
        result = self.run_block('Add Tailscale repo', 'A', {})
        self.assertEqual(result.returncode, 0, result.output)
        key_file = self.run_dir / 'key-tailscale'
        self.assertEqual(self.imported_keys(), [key_file.read_bytes()])
        self.assertEqual(key_file.read_bytes(), self.key['A'].read_bytes())
        [repo] = self.calls('ansible.builtin.yum_repository')
        self.assertEqual(repo['gpgkey'], f'file://{key_file}')


class VendorRepoKeyTests(Harness):
    """gh-cli, google-chrome, google-cloud-cli and mullvad: pinned key, checked bytes in a local file, includepkgs."""
    REPOS = {
        # block name: (repo file name, pin variable, enabling defaults, expected includepkgs)
        'Add GitHub CLI repo': ('gh-cli', 'repos_dnf_gh_cli_gpg_fingerprints',
                                {'repo_gh_cli': True, 'is_fedora': False}, ['gh']),
        'Add Google Chrome repo': ('google-chrome', 'repos_dnf_google_chrome_gpg_fingerprints',
                                   {'repo_google_chrome': True}, ['google-chrome-*']),
        'Add Google Cloud SDK repo': ('google-cloud-cli', 'repos_dnf_gcloud_gpg_fingerprints',
                                      {'repo_google_cloud_sdk': True}, ['google-cloud-cli*']),
        'Add Mullvad VPN repo': ('mullvad', 'repos_dnf_mullvad_gpg_fingerprints',
                                 {'repo_mullvad': True, 'profile': 'personal'}, ['mullvad-vpn']),
    }

    def pins(self, var, fpr):
        return {'el9': [fpr], 'el10': [fpr]} if var == 'repos_dnf_gcloud_gpg_fingerprints' else [fpr]

    def run_repo(self, block, served, config=None):
        name, var, enable, _ = self.REPOS[block]
        defaults = dict({'is_fedora': True, 'csb_detected': False, 'csb_rhel': False, 'profile': 'work',
                         'repos_dnf_google_chrome_repo': 'https://dl.google.com/linux/chrome/rpm/stable/x86_64',
                         'repos_dnf_gcloud_min_version': '500.0.0', var: self.pins(var, self.fpr['A'])}, **enable)
        return self.play('repos_dnf', tasks_named(REPOS_DNF / 'tasks/main.yml', block), defaults, config=config or {},
                         served_key=self.key[served], facts=True)

    def test_repo_trusts_only_the_checked_key_file_and_its_vendor_packages(self):
        if platform.machine() != 'x86_64':
            self.skipTest('the Chrome and Cloud SDK repos are x86_64 only')
        for block, (name, _, _, includepkgs) in self.REPOS.items():
            with self.subTest(repo=name):
                self.setUp()
                result = self.run_repo(block, 'A')
                self.assertEqual(result.returncode, 0, result.output)
                key_file = self.run_dir / f'key-{name}'
                self.assertEqual(self.imported_keys(), [key_file.read_bytes()])
                self.assertEqual(key_file.read_bytes(), self.key['A'].read_bytes())
                [repo] = self.calls('ansible.builtin.yum_repository')
                self.assertEqual(repo['gpgkey'], f'file://{key_file}')
                self.assertEqual(repo['includepkgs'], includepkgs)
                self.assertIs(repo['gpgcheck'], True)

    def test_chrome_package_scripts_cannot_rewrite_the_repo_file(self):
        """Chrome's postinst writes repo_add_once="true" only when the defaults file is missing, then rewrites the repo."""
        if platform.machine() != 'x86_64':
            self.skipTest('the Chrome repo is x86_64 only')
        for existing in (None, 'repo_add_once="true"\ninstall_device_trust_key_management_command=true\n'):
            with self.subTest(existing=existing):
                self.setUp()
                if existing:
                    self.chrome_defaults.write_text(existing)
                result = self.run_repo('Add Google Chrome repo', 'A')
                self.assertEqual(result.returncode, 0, result.output)
                text = self.chrome_defaults.read_text()
                self.assertIn('repo_add_once="false"', text.splitlines())
                self.assertNotIn('repo_add_once="true"', text)
                if existing:
                    self.assertIn('install_device_trust_key_management_command=true', text)
                # the postinst logic of google-chrome-stable (DEFAULTS_FILE handling), run against the result
                repo = self.run_dir / 'google-chrome.repo'
                repo.write_text('gpgkey = file:///etc/pki/rpm-gpg/RPM-GPG-KEY-laptop-setup-google-chrome\n')
                postinst = ('DEFAULTS_FILE="$1"; [ -e "$DEFAULTS_FILE" ] || echo \'repo_add_once="true"\' > "$DEFAULTS_FILE"; '
                            '. "$DEFAULTS_FILE"; if [ "$repo_add_once" = "true" ]; then '
                            'echo "gpgkey=https://dl.google.com/linux/linux_signing_key.pub" > "$2"; fi')
                subprocess.run(['sh', '-c', postinst, 'postinst', str(self.chrome_defaults), str(repo)], check=True)
                self.assertNotIn('https://', repo.read_text())

    def test_repo_file_without_the_pinned_shape_is_removed_before_an_abort(self):
        """An older repo file (https gpgkey, no includepkgs) goes even when the unpinned key then stops the run."""
        if platform.machine() != 'x86_64':
            self.skipTest('the Chrome and Cloud SDK repos are x86_64 only')
        for block, (name, _, _, _) in self.REPOS.items():
            with self.subTest(repo=name):
                self.setUp()
                (self.repos_dir / f'{name}.repo').write_text(
                    f'[{name}]\nbaseurl = https://example.invalid/\ngpgcheck = 1\ngpgkey = https://example.invalid/key.asc\n')
                result = self.run_repo(block, 'B')
                self.assertEqual(result.returncode, 2, result.output)
                self.assertEqual(self.calls('ansible.builtin.yum_repository'), [{'name': name, 'state': 'absent'}])
                self.setUp()
                (self.repos_dir / f'{name}.repo').write_text(
                    f'[{name}]\ngpgcheck = 1\ngpgkey = file://{self.run_dir}/key-{name}\nincludepkgs = x\n')
                result = self.run_repo(block, 'A')
                self.assertEqual(result.returncode, 0, result.output)
                self.assertNotIn({'name': name, 'state': 'absent'}, self.calls('ansible.builtin.yum_repository'))

    def test_chrome_repo_url_must_be_plain_https(self):
        if platform.machine() != 'x86_64':
            self.skipTest('the Chrome repo is x86_64 only')
        for url in ('http://dl.google.com/linux/chrome/rpm/stable/x86_64', 'ftp://mirror.example/chrome',
                    'https://dl.google.com/x,https://evil.example/y'):
            with self.subTest(url=url):
                self.setUp()
                self.assert_refused(self.run_repo('Add Google Chrome repo', 'A', config={'repos_dnf_google_chrome_repo': url}),
                                    'plain https:// URL')

    def test_unpinned_key_refused(self):
        if platform.machine() != 'x86_64':
            self.skipTest('the Chrome and Cloud SDK repos are x86_64 only')
        for block, (name, _, _, _) in self.REPOS.items():
            with self.subTest(repo=name):
                self.setUp()
                self.assert_refused(self.run_repo(block, 'B'), f'[{self.fpr["B"]}]')

    def test_pin_override_from_config_refused(self):
        if platform.machine() != 'x86_64':
            self.skipTest('the Chrome and Cloud SDK repos are x86_64 only')
        for block, (name, var, _, _) in self.REPOS.items():
            with self.subTest(repo=name):
                self.setUp()
                self.assert_refused(self.run_repo(block, 'B', config={var: self.pins(var, self.fpr['B'])}),
                                    'must not be overridden')


class MacAcliTests(Harness):
    """The macOS acli install: no dnf or rpm involved, so this runs on the Mac too (rpm is stubbed)."""
    LINUX_ONLY = False

    def test_macos_acli_path_from_config_refused(self):
        # _acli_have keeps the old code away from the download, so nothing is fetched or written either way
        config = {'_acli_bin': str(self.run_dir / 'evil'), '_acli_have': '999'}
        defaults = {'packages_install_binaries': True, 'is_macos': True, 'packages_acli_version': '1.3.40-stable'}
        result = self.play('packages', tasks_named(STANDALONE, 'Install acli (Atlassian CLI) from its pinned tarball (macOS)'),
                           defaults, config=config, overrides={'url': f'file://{self.run_dir}/absent'}, facts=True)
        self.assertEqual(result.returncode, 2, result.output)
        self.assertIn('_acli_bin and _acli_arch are internal', result.output)


class AcliTests(Harness):
    LEGACY = {'repos_dnf_acli_baseurl': 'https://evil.example.net/repo/', 'repos_dnf_internal_hosts': ['evil.example.net']}
    REPOS_OFF = {'profile': 'work', 'is_fedora': True, 'csb_detected': False, 'csb_rhel': False, 'repo_gh_cli': False,
                 'repo_google_chrome': False, 'repo_google_cloud_sdk': False, 'repo_mullvad': False, 'repo_redhat': False,
                 'repo_rpmfusion_free': False, 'repo_rpmfusion_nonfree': False, 'repo_tailscale': False,
                 'repos_dnf_internal_hosts': []}

    def repos_dnf(self, config, env=None):
        return self.play('repos_dnf', tasks_named(REPOS_DNF / 'tasks/main.yml'), self.REPOS_OFF, config=config, env=env)

    def test_config_cannot_add_an_unsigned_acli_repo(self):
        result = self.repos_dnf(self.LEGACY)
        self.assertEqual(result.returncode, 0, result.output)
        written = [repo for repo in self.calls('ansible.builtin.yum_repository') if repo.get('state') != 'absent']
        self.assertEqual(written, [])
        self.assertEqual(self.calls('ansible.builtin.dnf'), [], 'nothing may be installed from a config.yml repo')
        self.assertIn({'name': 'acli', 'state': 'absent'}, self.calls('ansible.builtin.yum_repository'))

    def test_stale_acli_versionlock_warned_without_the_legacy_variable(self):
        """The repo file existed (the stub reports a change) and acli is locked: warn even with the variable gone."""
        result = self.repos_dnf({}, env={'TEST_VERSIONLOCK': 'acli-0:1.3.30-1.*'})
        self.assertEqual(result.returncode, 0, result.output)
        self.assertRegex(result.output, r'sudo\s+dnf\s+versionlock\s+delete\s+acli')
        self.setUp()  # a fresh scratch role and log
        quiet = self.repos_dnf({}, env={'TEST_VERSIONLOCK': 'google-cloud-cli-0:500.0.0-1.*'})
        self.assertNotRegex(quiet.output, r'sudo\s+dnf\s+versionlock\s+delete\s+acli')

    def test_cleanup_list_from_config_cannot_remove_other_repo_files(self):
        config = {'_repos_dnf_toggled': [{'repo': 'fedora', 'pkg': 'not-installed', 'toggle': 'x', 'enabled': False}]}
        result = self.repos_dnf(config)
        self.assertEqual(result.returncode, 0, result.output)
        removed = {repo['name'] for repo in self.calls('ansible.builtin.yum_repository') if repo.get('state') == 'absent'}
        self.assertNotIn('fedora', removed)
        self.assertIn('tailscale-stable', removed)

    def acli(self, served, pinned):
        if platform.machine() != 'x86_64':
            self.skipTest('the acli rpm task runs on x86_64 only')
        rpm = self.run_dir / 'acli.rpm'
        rpm.write_bytes(served)
        defaults = {'packages_install_binaries': True, 'is_dnf': True, 'csb_rhel': False,
                    'packages_acli_version': '1.3.40-stable', 'packages_acli_rpm_sha256': hashlib.sha256(pinned).hexdigest()}
        return self.play('packages', tasks_named(STANDALONE, 'Install acli (Atlassian CLI) from its pinned rpm'), defaults,
                         config=self.LEGACY, overrides={'url': f'file://{rpm}'}, facts=True)

    def test_acli_rpm_checksum_mismatch_stops_the_play(self):
        result = self.acli(b'tampered acli rpm', b'genuine acli rpm')
        self.assertEqual(result.returncode, 2, result.output)
        self.assertIn('SECURITY: the downloaded acli rpm does not match packages_acli_rpm_sha256', result.output)
        self.assertEqual(self.calls('ansible.builtin.dnf'), [])

    def test_acli_rpm_matching_its_pin_is_installed_as_a_file(self):
        result = self.acli(b'genuine acli rpm', b'genuine acli rpm')
        self.assertEqual(result.returncode, 0, result.output)
        [install] = self.calls('ansible.builtin.dnf')
        self.assertTrue(install['name'].endswith('/acli.rpm'), install)


if __name__ == '__main__':
    unittest.main()
