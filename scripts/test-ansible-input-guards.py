#!/usr/bin/env python3
"""Exercise production assertions locally without running provisioning tasks."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TEST_TIMEOUT = max(30, int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0')))


def guard(filename, name):
    tasks = yaml.safe_load((ROOT / filename).read_text())
    def walk(items):
        for task in items:
            yield task
            for key in ['block', 'rescue', 'always', 'tasks', 'pre_tasks', 'post_tasks']:
                yield from walk(task.get(key, []))
    result = next(task for task in walk(tasks) if name in task.get('name', ''))
    assert 'ansible.builtin.assert' in result, 'Guard tests must execute assertions only'
    return result


SSH_ENV = {'SSH_CONNECTION': '192.0.2.1 50000 192.0.2.2 22'}


class InputGuardTests(unittest.TestCase):
    def test_personal2_config_path_cannot_inject_tmpfiles_directives(self):
        task = guard('roles/claude/tasks/main.yml', 'SECURITY assert active Claude instance paths cannot inject tmpfiles directives')
        for path, accepted in [('/home/fixture/.claude-per2', True),
                               ('/home/fixture/.claude-per2\n', False),
                               ('/home/fixture/.claude-per2\nz /fixture/unrelated 0000 - - -\n#', False)]:
            with self.subTest(path=path):
                self.check(task, {'_claude_instances': [path]}, accepted)

    def test_extra_claude_config_dirs_checked_before_use(self):
        """The assert runs before the set_fact that concatenates the list, so a string fails with its message."""
        tasks = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        names = [task.get('name') for task in tasks]
        check = names.index('Assert extra Claude config dirs are ~/.claude-<name>')
        self.assertEqual(names[check + 1], 'Set active Claude instance directories')
        base = {'ansible_facts': {'user_dir': '/home/fixture'}, 'claude_default_config_dir': '/home/fixture/.claude',
                'claude_personal2_config_dir': '/home/fixture/.claude-per2', 'claude_personal2_instance_enabled': True,
                'claude_work_config_dir': '/home/fixture/.claude-work', 'profile': 'work'}
        for value, accepted in [(['/home/fixture/.claude-second'], True), ('/home/fixture/.claude-second', False),
                                (['/home/fixture/.claude-x y'], False), (['/home/other/.claude-second'], False)]:
            with self.subTest(value=value):
                self.check(tasks[check:check + 2], dict(base, claude_extra_config_dirs=value), accepted)

    def check(self, task, variables, accepted, env=None):
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / 'guard.yml'
            playbook.write_text(yaml.safe_dump([{
                'name': 'Production input guard regression', 'hosts': 'localhost',
                'gather_facts': False, 'vars': variables, 'tasks': task if isinstance(task, list) else [task],
            }], allow_unicode=True))
            env_vars = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'))
            env_vars.pop('SSH_CONNECTION', None)  # lockout guards differ for runs made over SSH
            env_vars.update(env or {})
            result = subprocess.run(
                [str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                cwd=ROOT, env=env_vars, capture_output=True, text=True, timeout=TEST_TIMEOUT,
            )
            self.assertEqual(result.returncode, 0 if accepted else 2, result.stdout + result.stderr)
            if not accepted:
                # rc 2 alone also matches fixture errors; the failure must come from the guard itself.
                self.assertNotRegex(result.stdout + result.stderr, r'(?i)is undefined|templating error|syntax error|error while evaluating')

    def test_managed_ssh_agent_unit(self):
        task = guard('roles/claude/tasks/main.yml', 'Assert the SSH agent unit matches')
        template = (ROOT / 'roles/dotfiles/templates/ssh-agent.service.j2').read_text()
        expected = template.replace('{{ ansible_managed }}', 'Ansible managed').replace('{{ dotfiles_ssh_agent_timeout | int }}', '28800')
        for content, accepted in [(expected, True), (expected.replace('ExecStart=/usr/bin/ssh-agent', 'ExecStart=/tmp/rogue'), False)]:
            with self.subTest(accepted=accepted):
                self.check(task, {
                    'role_path': str(ROOT / 'roles/claude'), 'dotfiles_ssh_agent_timeout': 28800,
                    '_claude_ssh_agent_units': {'results': [{'content': base64.b64encode(content.encode()).decode()}]},
                }, accepted)

    def test_foreign_and_symlinked_user_units(self):
        tasks = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        block = next(t['block'] for t in tasks if t.get('name') == 'Deploy and enable task queue systemd units')
        task = next(t for t in block if t.get('name', '').startswith('Fail on unmanaged systemd user unit files'))
        for filename, regular, accepted in [('ssh-agent.service', True, True),
                                            ('claude-queue.timer', True, True),
                                            ('foreign.service', True, False),
                                            ('ssh-agent.service', False, False)]:
            with self.subTest(filename=filename, regular=regular):
                self.check(task, {'_systemd_user_units': {'files': [
                    {'path': '/fixture/' + filename, 'isreg': regular}]}}, accepted)

    def test_managed_gnome_agent_mask(self):
        task = guard('roles/claude/tasks/main.yml', 'Assert the managed GNOME SSH agent mask')
        for regular, target, accepted in [(False, '/dev/null', True),
                                          (False, '/fixture/executable.service', False),
                                          (True, '/dev/null', False)]:
            with self.subTest(regular=regular, target=target):
                self.check(task, {'_claude_gcr_agent_mask': {'stat': {
                    'islnk': not regular, 'lnk_source': target}}}, accepted)

    def test_empty_ssh_audit_watch_directory(self):
        tasks = yaml.safe_load((ROOT / 'roles/system/tasks/main.yml').read_text())
        recognize = next(t for t in tasks if t.get('name') == 'Recognize the safe empty SSH service drop-in directory')
        assertion = guard('roles/system/tasks/main.yml', 'Fail on unexpected systemd drop-in directories')
        base = {'isdir': True, 'islnk': False, 'uid': 0, 'mode': '0755'}
        cases = [(base, 0, True), (base, 1, False),
                 (dict(base, uid=1000), 0, False), (dict(base, mode='0777'), 0, False),
                 (dict(base, islnk=True), 0, False), (dict(base, isdir=False), 0, False)]
        for metadata, entries, accepted in cases:
            with self.subTest(metadata=metadata, entries=entries):
                self.check([recognize, assertion], {
                    'system_is_container': False, 'csb_detected': False,
                    'system_systemd_dropin_whitelist': [],
                    '_system_ssh_dropin_stat': {'stat': metadata},
                    '_system_ssh_dropin_entries': {'matched': entries},
                    '_systemd_dropin_find': {'files': [{'path': '/etc/systemd/system/sshd.service.d'}]},
                }, accepted)

    def test_managed_claude_logrotate_stanza(self):
        tasks = yaml.safe_load((ROOT / 'roles/system/tasks/logrotate_sudo.yml').read_text())[0]['block']
        recognize = next(t for t in tasks if t.get('name') == 'Recognize the exact managed Claude logrotate stanza')
        reject = next(t for t in tasks if t.get('name') == 'Fail if unexpected files exist in /etc/logrotate.d')
        template = (ROOT / 'roles/claude/templates/claude-queue-poller-logrotate.j2').read_text()
        content = (template.replace('{{ ansible_managed }}', 'Ansible managed')
                   .replace('{{ claude_log_dir }}', '/home/fixture/old-logs')
                   .replace("{{ real_user_id | default(ansible_facts['user_id']) }}", 'fixture')
                   .replace('{{ _primary_group_name.stdout | trim }}', 'fixture-group'))
        cases = [(content, 0, '0644', True, True),
                 (content.replace('    copytruncate', '    postrotate\n        /fixture/rogue\n    endscript'), 0, '0644', True, False),
                 (content.replace('su fixture fixture-group', 'su root root'), 0, '0644', True, False),
                 (content, 1000, '0644', True, False), (content, 0, '0666', True, False),
                 (content, 0, '0644', False, False)]
        for text, uid, mode, regular, accepted in cases:
            with self.subTest(uid=uid, mode=mode, regular=regular, accepted=accepted):
                self.check([recognize, reject], {
                    'role_path': str(ROOT / 'roles/system'), 'is_dnf': True,
                    'real_user_id': 'fixture',
                    'claude_log_dir': '/home/fixture/new-logs',
                    'system_logrotated_whitelist': [],
                    '_logrotated_unowned': {'stdout_lines': ['/etc/logrotate.d/claude-queue-poller']},
                    '_system_claude_logrotate_stat': {'stat': {'isreg': regular, 'uid': uid, 'mode': mode}},
                    '_system_claude_logrotate_group': {'stdout': 'fixture-group'},
                    '_system_claude_logrotate_content': {'content': base64.b64encode(text.encode()).decode()},
                }, accepted)

    def test_existing_ssh_authorized_key_types(self):
        tasks = yaml.safe_load((ROOT / 'roles/ssh/tasks/main.yml').read_text())
        block = next(t['block'] for t in tasks if t.get('name', '').startswith('Validate existing authorized_keys'))
        # Execute the production parser and assertions with slurped fixture data.
        checks = block
        cases = [
            ('ssh-ed25519 AAAA comment\n', True),
            ('sk-ssh-ed25519@openssh.com AAAA comment\n', True),
            ('# comment\n  ssh-ed25519 AAAA\n', True),
            ('restrict,command="echo hello" ssh-ed25519 AAAA comment\n', True),
            ('ssh-rsa AAAA comment\n', False),
            ('restrict,command="echo hello" ssh-rsa AAAA\n', False),
            ('ssh-ed25519 AAAA\nssh-rsa BBBB\n', False),
            ('malformed entry\n', False),
            ('# no key\n', False),
        ]
        # Over SSH every incompatible case blocks; at the console only malformed entries do.
        for over_ssh in (True, False):
            for content, accepted in cases:
                expected = accepted or (not over_ssh and content != 'malformed entry\n')
                with self.subTest(content=content, over_ssh=over_ssh):
                    self.check(checks, {
                        '_authkeys_slurp': {'content': base64.b64encode(content.encode()).decode()},
                        'ssh_pubkey_accepted_algorithms': ['ssh-ed25519', 'sk-ssh-ed25519@openssh.com'],
                    }, expected, SSH_ENV if over_ssh else None)

    def test_go_checksum_server(self):
        task = guard('roles/packages/tasks/install_go_tools.yml', '(1/2)')
        valid = ['sum.golang.org', 'sum.corp.com:443', '1sum.example.com', 'sum.1corp.example.com:65535']
        invalid = ['off', '', 'localhost', 'localhost:443', '127.0.0.1', '127.0.0.1:443',
                   'sum.corp.com:0', 'sum.corp.com:65536', 'sum.-corp.com', 'sum.corp.com/path', 'sum.golang.org\n', 123]
        for value in valid + invalid:
            with self.subTest(value=value):
                self.check(task, {'packages_gosumdb': value}, value in valid)

    def test_package_version_lookup(self):
        task = guard('roles/packages/tasks/main.yml', 'SECURITY assert binary version variables')
        defaults = yaml.safe_load((ROOT / 'roles/packages/defaults/main.yml').read_text())
        for version, accepted in [('0.24.0', True), ('../../bad', False), ('0.24.0 --help', False)]:
            with self.subTest(version=version):
                self.check(task, dict(defaults, packages_subctl_version=version), accepted)

    def test_subctl_module_versions(self):
        task = guard('roles/packages/tasks/install_go_tools.yml', 'Validate selected subctl module versions')
        for version, accepted in [('v0.24.0', True), ('v0.18.0', True), ('latest', False),
                                  ('v0.24.0\n', False), ('v0.24.0 -extld=bad', False)]:
            with self.subTest(version=version):
                self.check(task, {'_packages_subctl_go_modules': [
                    'github.com/submariner-io/subctl/cmd@' + version]}, accepted)

    def test_container_subctl_versions(self):
        task = guard('roles/distrobox/tasks/container-provision.yml', 'Validate pinned container subctl version')
        for version, accepted in [('v0.24.0', True), ('v0.18.0', True), ('latest', False),
                                  ('v0.24.0\n', False), ('v0.24.0 -extld=bad', False)]:
            with self.subTest(version=version):
                self.check(task, {'distrobox_subctl_version': version}, accepted)

    def test_home_permission_failure_guard(self):
        task = guard('roles/system/tasks/account_hardening.yml', 'Abort if home directory is more permissive')
        for expected, actual, accepted in [('0700', '0700', True), ('0700', '0500', True),
                                          ('0700', '0550', False), ('0700', '0670', False),
                                          ('0700', '0000', True), ('0700', '1700', False),
                                          ('0750', '0710', True), ('0750', '0740', True),
                                          ('0750', '0730', False)]:
            with self.subTest(expected=expected, actual=actual):
                self.check(task, {'system_home_mode': expected, '_home_stat': {'stat': {'mode': actual}},
                                  'real_user_home': '/public-test-home', '_home_mode_result': {}}, accepted)

    def test_container_signature_policy(self):
        task = guard('molecule/shared/verify-containers.yml', 'Assert policy.json default policy is reject')
        for policy, accepted in [([{'type': 'reject'}], True), ([{'type': 'insecureAcceptAnything'}], False),
                                 ([{'type': 'reject'}, {'type': 'insecureAcceptAnything'}], False)]:
            with self.subTest(policy=policy):
                encoded = base64.b64encode(json.dumps({'default': policy}).encode()).decode()
                self.check(task, {'verify_container_policy': {'content': encoded}}, accepted)

    def test_claude_work_sandbox_policy(self):
        task = guard('molecule/shared/verify-claude-work.yml', 'Validate Claude work settings.json')
        for failure in [None, 'sandbox', 'fallback', 'mcp', 'deny', 'self-lockout']:
            with self.subTest(failure=failure):
                settings = {'sandbox': {'enabled': False, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False},
                            'enableAllProjectMcpServers': False,
                            'permissions': {'deny': ['Read(~/.claude-work/.credentials.json)',
                                                     'Edit(**/.git/hooks/**)', 'Edit(**/.githooks/**)']}}
                if failure == 'self-lockout':
                    settings['permissions']['deny'].append('Read(~/.claude-work/**)')
                if failure == 'sandbox':
                    settings['sandbox']['enabled'] = True  # claude_sandbox_enabled defaults to false
                if failure == 'fallback':
                    del settings['sandbox']['allowUnsandboxedCommands']
                if failure == 'mcp':
                    settings['enableAllProjectMcpServers'] = True
                if failure == 'deny':
                    settings['permissions']['deny'].pop()
                encoded = base64.b64encode(json.dumps(settings).encode()).decode()
                self.check(task, {'verify_claude_work_content': {'content': encoded},
                                  'verify_claude_work_settings': {'stat': {'exists': True}}}, failure is None)

    def test_debian_pam_stack_ordering(self):
        task = guard('molecule/debian/verify.yml', 'Assert pam_faillock preauth->pam_unix->authfail')
        preauth = 'auth required pam_faillock.so preauth'
        unix = 'auth [success=1 default=ignore] pam_unix.so nullok'
        authfail = 'auth [default=die] pam_faillock.so authfail'
        cases = [([preauth, unix, authfail], True),
                 ([preauth, 'auth required pam_unix.so', authfail], True),
                 ([preauth, authfail, unix], False), ([preauth, authfail], False),
                 ([preauth, 'auth [success=1 default=ignore pam_unix.so', authfail], False),
                 ([preauth, 'auth\nrequired pam_unix.so', authfail], False),
                 ([preauth, preauth, unix, authfail], False)]
        for lines, accepted in cases:
            with self.subTest(lines=lines):
                self.check(task, {'_pam_auth_content': '\n'.join(lines) + '\n'}, accepted)

    def test_sysctl_format(self):
        task = guard('roles/system/tasks/main.yml', 'Validate sysctl key and value format')
        cases = [({'net.ipv4.icmp_echo_ignore_broadcasts': 1}, True), ({'vm.swappiness': 0}, True),
                 ({'bad;key': 1}, False), ({'safe': '1\n'}, False), ({'safe': '1;bad'}, False),
                 # the kernel runs these as root; a leading '-' only makes systemd-sysctl ignore errors
                 ({'kernel.modprobe': '/home/user/.local/bin/x'}, False),
                 ({'-kernel.poweroff_cmd': '/home/user/.local/bin/x'}, False),
                 ({'kernel.hotplug': '/home/user/x'}, False), ({'kernel.usermodehelper.bset': '4294967295 31'}, False),
                 ({'kernel.core_pattern': '/home/user/core'}, False), ({'kernel.modprobe_extra': 1}, True),
                 # spellings systemd-sysctl or procps reduce to kernel.modprobe, and malformed keys
                 ({'kernel..modprobe': '/home/user/x'}, False), ({'.kernel.modprobe': '/home/user/x'}, False),
                 ({'kernel.modprobe.': '/home/user/x'}, False), ({'-.kernel.modprobe': '/home/user/x'}, False),
                 ({'kernel..core_pattern': '/home/user/x'}, False), ({'fs..binfmt_misc.register': 'x'}, False),
                 ({'Kernel.modprobe': '/home/user/x'}, False), ({'-vm.swappiness': 10}, True),
                 ({'net.bridge.bridge-nf-call-iptables': 1}, True)]
        for extra, accepted in cases:
            with self.subTest(extra=extra):
                self.check(task, {'system_is_container': False, 'csb_rhel': False,
                                  'system_sysctl_extra': extra, 'system_sysctl_extra_additional': {}}, accepted)

    def test_sysctl_extra_additional_is_checked_too(self):
        task = guard('roles/system/tasks/main.yml', 'Validate sysctl key and value format')
        self.check(task, {'system_is_container': False, 'csb_rhel': False, 'system_sysctl_extra': {},
                          'system_sysctl_extra_additional': {'kernel.modprobe': '/home/user/.local/bin/x'}}, False)

    def test_acli_site_is_an_atlassian_cloud_site(self):
        task = guard('roles/claude/tasks/acli.yml', 'SECURITY assert acli_site is an Atlassian Cloud site')
        for site, accepted in [('redhat.atlassian.net', True), ('my-team.atlassian.net', True),
                               ('evil.example', False), ('redhat.atlassian.net.evil.example', False),
                               ('redhat.atlassian.net\n', False), ('https://redhat.atlassian.net', False)]:
            with self.subTest(site=site):
                self.check(task, {'acli_site': site}, accepted)

    def test_macos_python_is_a_formula_name(self):
        task = guard('roles/dotfiles/tasks/main.yml', 'Assert dotfiles_macos_python is a Homebrew formula name')
        for value, accepted in [('python@3.14', True), ('python@3.14$(touch /tmp/x)', False), ('a b', False),
                                ('python@3.14"', False), ('`id`', False)]:
            with self.subTest(value=value):
                self.check(task, {'dotfiles_macos_python': value}, accepted)

    def test_ssh_key_basenames_are_plain_file_names(self):
        task = guard('common/tasks/pre_flight_checks.yml', 'Assert the SSH key basenames are plain file names')
        for name, accepted in [('id_ed25519_sk', True), ('id_rh_git', True), ('id.key-2', True),
                               ('../authorized_keys', False), ('..', False), ('.hidden', False), ('sub/key', False),
                               ('/etc/ssh/key', False)]:
            for variable in ('ssh_signing_key_basename', 'dotfiles_ssh_hardware_key_basename',
                             'dotfiles_ssh_redhat_key_basename'):
                with self.subTest(variable=variable, name=name):
                    self.check(task, {variable: name}, accepted)

    def test_secret_directories_are_normalized_paths_in_home(self):
        home = '/home/fixture'
        codex = guard('roles/codex/tasks/main.yml', 'Validate Codex installation method and paths')
        base = {'ansible_facts': {'user_dir': home}, 'codex_install_method': 'npm',
                'codex_npm_prefix': home + '/.npm-global', 'codex_legacy_install_dir': home + '/.local/share/codex-cli',
                'codex_work_config_dir': home + '/.codex', 'codex_personal_config_dir': home + '/.codex-personal'}
        for value, accepted in [(home + '/.codex', True), (home + '/../other/.codex', False),
                                (home + '/x/../.codex', False), (home + '/./.codex', False), (home + '//.codex', False)]:
            with self.subTest(codex_work_config_dir=value):
                self.check(codex, dict(base, codex_work_config_dir=value), accepted)
        drive = guard('roles/drive_sync/tasks/main.yml', 'Validate drive sync settings')
        base = {'ansible_facts': {'user_dir': home}, 'is_linux': True, 'drive_sync_accounts': [],
                'drive_sync_host': 'fixture-host'}
        for value, accepted in [(home + '/.config/rclone/drive-sync.conf', True),
                                (home + '/../other/rclone.conf', False), ('/tmp/rclone.conf', False),
                                (home + '/.config/./rclone.conf', False)]:
            with self.subTest(drive_sync_config=value):
                self.check(drive, dict(base, drive_sync_config=value), accepted)

    def test_locale_and_keymap_are_plain_names(self):
        """/etc/profile.d/lang.sh evaluates /etc/locale.conf in every login shell, root's included."""
        task = guard('roles/system/tasks/timezone_locale.yml', 'SECURITY assert system_locale and system_keymap')
        base = {'system_is_container': False, 'system_locale': 'en_US.UTF-8', 'system_keymap': 'us'}
        cases = [({}, True), ({'system_locale': 'C.UTF-8'}, True), ({'system_locale': 'de_DE.UTF-8@euro'}, True),
                 ({'system_locale': 'en_US.UTF-8; id -un > /tmp/x'}, False), ({'system_locale': 'en_US.UTF-8$(id)'}, False),
                 ({'system_locale': 'en_US.UTF-8\n'}, False), ({'system_keymap': 'us; id'}, False)]
        for override, accepted in cases:
            with self.subTest(override=override):
                self.check(task, dict(base, **override), accepted)

    def test_injected_locale_never_runs_in_a_login_shell(self):
        tasks = yaml.safe_load((ROOT / 'roles/system/tasks/timezone_locale.yml').read_text())
        guard_tasks = [task for task in tasks if task.get('name', '').startswith('SECURITY assert system_locale')]
        write = next(task for task in tasks if task.get('name') == 'Set system locale')
        with tempfile.TemporaryDirectory() as directory:
            conf, marker = Path(directory) / 'locale.conf', Path(directory) / 'ran'
            write = dict(write, **{'ansible.builtin.lineinfile': dict(write['ansible.builtin.lineinfile'], path=str(conf))})
            source = {'name': 'A login shell sources it', 'ansible.builtin.command': {'argv': ['sh', '-c', '. "$1"', 'sh', str(conf)]}}
            self.check(guard_tasks + [write, source], {'system_is_container': False, 'system_keymap': 'us',
                                                       'system_locale': 'en_US.UTF-8; touch ' + str(marker)}, False)
            self.assertFalse(marker.exists())

    def test_tlp_profiles_are_known_names(self):
        task = guard('roles/system/tasks/power_management.yml', 'SECURITY assert the TLP profile values')
        base = {'system_tlp_platform_profile_ac': 'performance', 'system_tlp_platform_profile_bat': 'balanced',
                'system_tlp_cpu_energy_policy_ac': 'balance_performance', 'system_tlp_cpu_energy_policy_bat': 'balance_power'}
        for override, accepted in [({}, True), ({'system_tlp_platform_profile_ac': 'performance; id'}, False),
                                   ({'system_tlp_cpu_energy_policy_bat': 'power$(id)'}, False)]:
            with self.subTest(override=override):
                self.check(task, dict(base, **override), accepted)

    def test_config_ssh_port_is_a_plain_port_number(self):
        """A bool is an int to Python: true rendered Port 0 in sshd but True/tcp in firewalld."""
        task = guard('common/tasks/pre_flight_checks.yml', 'Assert ssh_port in config.yml is a plain decimal port')
        cases = [(None, True), (722, True), ('722', True), (2222, True), (65535, True),
                 (True, False), (False, False), (722.9, False), ('722.0', False), (' 722', False), ('+722', False),
                 ('7_22', False), ('722\n', False), ('0x2d2', False), (22, False), (0, False), (65536, False),
                 ('', False), ([722], False)]
        for value, accepted in cases:
            with self.subTest(value=value):
                self.check(task, {'_pf_config_raw': {} if value is None else {'ssh_port': value}}, accepted)

    def test_usbguard_allow_rules_are_narrow(self):
        task = guard('common/tasks/pre_flight_checks.yml', 'Assert USBGuard whitelist/extra_rules in play namespace')
        defaults = yaml.safe_load((ROOT / 'roles/system/defaults/main.yml').read_text())['system_usbguard_whitelist']
        cases = [('allow id 046d:c52b', True), ('allow id 1d6b:* via-port "1-1"', True),
                 ('allow id 3297:1969 name "Moonlander Mark I" with-interface one-of { 03:*:* }', True),
                 ('allow id 2109:0817 name "USB3.0 Hub" with-interface 09:00:00', True),
                 ('allow serial "abc123"', True), ('block id *:*', True), ('reject with-interface { 08:*:* }', True),
                 ('allow id *:*', False), ('allow id  *:c52b', False), ('allow id one-of { 046d:c52b *:* }', False),
                 ('allow id *:* hash "jEP/6WzviqdJ5VSeTUY8PatCNBKeaREvo2OqdplND/o="', True),
                 ('allow id *:* via-port "1-1"', False), ('allow name "Keyboard"', False), ('allow', False),
                 ('allow name "foo id bar"', False), ('allow name "x" serial "y via-port z"', True)]
        for rule, accepted in cases:
            with self.subTest(rule=rule):
                self.check(task, {'system_usbguard_whitelist': defaults, 'system_usbguard_extra_rules': [rule]},
                           accepted)

    def test_distrobox_container_name_is_not_an_inventory_host(self):
        """add_host with an inventory host's name would put that host (localhost) in the containers group."""
        task = guard('roles/distrobox/tasks/main.yml', 'SECURITY assert distrobox variables are injection-safe')
        image = 'quay.io/toolbx/fedora-toolbox:44@sha256:' + '0' * 64
        for name, accepted in [('fedora-dev', True), ('', True), ('localhost', False)]:
            with self.subTest(name=name):
                self.check(task, {'distrobox_container_image': image, 'distrobox_container_name': name}, accepted)
        # A second pass of the role in one run (molecule distrobox-role) finds its own container registered.
        for name, accepted in [('fedora-dev', True), ('localhost', False)]:
            with self.subTest(registered=name):
                register = {'name': 'Register the container', 'ansible.builtin.add_host': {'name': name, 'groups': 'containers'}}
                self.check([register, task], {'distrobox_container_image': image, 'distrobox_container_name': name}, accepted)

    def test_distrobox_provisioning_values(self):
        """The validators Play 2 and Play 3 both run on the values add_host hands to the container play."""
        tasks = yaml.safe_load((ROOT / 'roles/distrobox/tasks/validate-provision.yml').read_text())
        good = {'distrobox_packages': ['git', 'python3-pip'], 'distrobox_oc_version': '4.18.1',
                'distrobox_oc_mirror_url': 'https://mirror.openshift.com/pub/openshift-v4/clients/ocp',
                'distrobox_host_exec_commands': ['podman', 'kind'], 'distrobox_golangci_lint_version': 'v2.12.2',
                'distrobox_oc_fetch_timeout': 30, 'distrobox_oc_fetch_retries': 3, 'distrobox_oc_fetch_delay': 10}
        cases = [({}, True), ({'distrobox_host_exec_commands': ['podman', 'bash']}, False),
                 ({'distrobox_oc_mirror_url': 'http://mirror.example/x'}, False),
                 ({'distrobox_oc_mirror_url': 'https://mirror.example/../x'}, False),
                 ({'distrobox_oc_version': '4.18;id'}, False), ({'distrobox_packages': ['--privileged']}, False),
                 ({'distrobox_packages': ['git\ninit_hooks=x']}, False),
                 ({'distrobox_golangci_lint_version': '2.12.2'}, True), ({'distrobox_oc_fetch_timeout': '600'}, True),
                 ({'distrobox_golangci_lint_version': 'latest'}, False), ({'distrobox_golangci_lint_version': 'v2.1'}, False),
                 ({'distrobox_golangci_lint_version': 'v2.12.2@main'}, False),
                 ({'distrobox_oc_fetch_timeout': 0}, False), ({'distrobox_oc_fetch_timeout': 601}, False),
                 ({'distrobox_oc_fetch_timeout': '30s'}, False), ({'distrobox_oc_fetch_timeout': True}, False),
                 ({'distrobox_oc_fetch_retries': 0}, False), ({'distrobox_oc_fetch_retries': 21}, False),
                 ({'distrobox_oc_fetch_delay': -1}, False), ({'distrobox_oc_fetch_delay': 301}, False)]
        for override, accepted in cases:
            with self.subTest(override=override):
                self.check(tasks, dict(good, **override), accepted)

    def notes_tasks(self, *prefixes):
        found = []

        def walk(items):
            for task in items or []:
                if task.get('name', '').startswith(prefixes):
                    found.append(task)
                for key in ('block', 'rescue', 'always'):
                    walk(task.get(key))
        walk(yaml.safe_load((ROOT / 'roles/notes/tasks/main.yml').read_text()))
        return found

    def test_transcrypt_is_the_installed_root_owned_binary(self):
        """transcrypt receives the notes vault password: only the packages role's binary may run."""
        task = guard('roles/notes/tasks/main.yml', 'SECURITY assert transcrypt is the installed binary')
        root_file = {'exists': True, 'uid': 0, 'islnk': False, 'wgrp': False, 'woth': False}
        root_dirs = {'results': [{'stat': {'uid': 0, 'wgrp': False, 'woth': False}} for _ in range(4)]}
        linux = {'ansible_facts': {'os_family': 'RedHat'}, 'notes_transcrypt_bin_path': '/usr/local/bin/transcrypt',
                 'notes_transcrypt_bin': {'stat': root_file}, '_notes_transcrypt_dirs': root_dirs}
        cases = [({}, True), ({'notes_transcrypt_bin': {'stat': {'exists': False}}}, True),
                 ({'notes_transcrypt_bin_path': '/home/user/bin/transcrypt'}, False),
                 ({'notes_transcrypt_bin': {'stat': dict(root_file, uid=1000)}}, False),
                 ({'notes_transcrypt_bin': {'stat': dict(root_file, woth=True)}}, False),
                 ({'notes_transcrypt_bin': {'stat': dict(root_file, wgrp=True)}}, False),
                 ({'notes_transcrypt_bin': {'stat': dict(root_file, islnk=True)}}, False),
                 ({'_notes_transcrypt_dirs': {'results': [{'stat': {'uid': 1000, 'wgrp': False, 'woth': False}}]}}, False),
                 ({'_notes_transcrypt_dirs': {'results': [{'stat': {'uid': 0, 'wgrp': False, 'woth': True}}]}}, False),
                 ({'ansible_facts': {'os_family': 'Darwin'}, 'dotfiles_homebrew_prefix': '/opt/homebrew',
                   'notes_transcrypt_bin_path': '/opt/homebrew/bin/transcrypt'}, True),
                 ({'ansible_facts': {'os_family': 'Darwin'}, 'dotfiles_homebrew_prefix': '/opt/homebrew',
                   'notes_transcrypt_bin_path': '/tmp/transcrypt'}, False)]
        for override, accepted in cases:
            with self.subTest(override=override):
                self.check(task, dict(linux, **override), accepted)

    def test_fake_transcrypt_never_receives_the_password(self):
        tasks = self.notes_tasks('Gather transcrypt binary stat', 'Gather the transcrypt directory chain',
                                 'SECURITY assert transcrypt is the installed binary')
        with tempfile.TemporaryDirectory() as directory:
            fake, marker = Path(directory) / 'transcrypt', Path(directory) / 'ran'
            fake.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
            fake.chmod(0o755)
            run = {'name': 'Initialize transcrypt as the role does', 'ansible.builtin.command': {
                'argv': ['{{ notes_transcrypt_bin_path }}', '-p', 'vault-password']}}
            self.check(tasks + [run], {'notes_transcrypt_bin_path': str(fake), 'dotfiles_homebrew_prefix': '/opt/homebrew',
                                       'ansible_facts': {'os_family': 'RedHat'}}, False)
            self.assertFalse(marker.exists())

    def test_container_integer_limits(self):
        for variable in ['containers_pids_limit', 'containers_nofile_limit']:
            task = guard('roles/containers/tasks/main.yml', f'Assert {variable} is a non-negative integer')
            for value in [0, 4096, -1, 'abc', '1.5']:
                with self.subTest(variable=variable, value=value):
                    self.check(task, {'is_linux': True, variable: value}, value in [0, 4096])

    def test_ssh_lockout_scope(self):
        task = guard('roles/ssh/tasks/main.yml', 'Assert — vault empty and no authorized_keys')
        for system, key, over_ssh, accepted in [('Darwin', '', True, True), ('Linux', '', True, False),
                                                 ('Linux', '', False, True),
                                                 ('Linux', 'ssh-ed25519 fixture', True, True)]:
            with self.subTest(system=system, key=key, over_ssh=over_ssh):
                self.check(task, {'ansible_facts': {'system': system}, 'ssh_auth_key_pub': key,
                                  '_ssh_authkeys_stat': {'stat': {'exists': False}}}, accepted,
                           SSH_ENV if over_ssh else None)

    def test_ssh_auth_key_types_every_line(self):
        """vault_ssh_auth_key_pub may hold backup keys, one per line: every line must pass the sshd type check."""
        task = guard('roles/ssh/tasks/main.yml', 'Assert SSH auth public key type is accepted by sshd hardening')
        primary, backup, rsa = 'ssh-ed25519 AAAA a', 'sk-ssh-ed25519@openssh.com AAAA b', 'ssh-rsa AAAA c'
        cases = [(primary, True), (primary + '\n' + backup + '\n', True), (primary + '\n' + rsa, False)]
        for (key, accepted), system in [(c, s) for c in cases for s in ('Linux', 'Darwin')]:  # the Mac's sshd too
            with self.subTest(key=key, system=system):
                self.check(task, {'ansible_facts': {'system': system}, 'ssh_auth_key_pub': key,
                                  'ssh_pubkey_accepted_algorithms': ['ssh-ed25519', 'sk-ssh-ed25519@openssh.com']},
                           accepted)

    def test_vendor_polkit_rules_fail_closed(self):
        source = yaml.safe_load((ROOT / 'roles/system/tasks/hardening_core.yml').read_text())
        block = next(task['block'] for task in source if task['name'] == 'Polkit rules directory scan')
        tasks = block[-4:]  # Execute the production classification, rejection and CSB warning tasks.
        vendor = '/etc/polkit-1/rules.d/49-polkit-pkla-compat.rules'
        unknown = '/etc/polkit-1/rules.d/unknown.rules'
        cases = [([vendor], 'polkit-pkla-compat', 0, '', True),
                 ([vendor], 'other-package', 0, '', False),
                 ([vendor], 'polkit-pkla-compat', 1, '', False),
                 ([vendor], 'polkit-pkla-compat', 0, 'modified file', False),
                 ([vendor, unknown], 'polkit-pkla-compat', 0, '', False),
                 ([], '', 1, '', True)]
        for csb in (False, True):  # on CSB an unexpected rule only warns
            for files, owner, rc, output, accepted in cases:
                with self.subTest(files=files, owner=owner, rc=rc, output=output, csb=csb):
                    self.check(tasks, {'system_polkitd_whitelist': [], 'csb_detected': csb,
                                      '_polkit_rules_found': {'files': [{'path': path, 'isdir': False} for path in files]},
                                      '_polkit_compat_owner': {'rc': 0, 'stdout': owner},
                                      '_polkit_compat_integrity': {'rc': rc, 'stdout': output}}, accepted or csb)

    def test_native_homebrew_update_configuration(self):
        task = guard('roles/packages/tasks/install_brew.yml', 'Validate native Homebrew update configuration')
        cases = [('/opt/homebrew', 86400, True), ('/usr/local', 0, True),
                 ('/tmp/unsafe', 86400, False), ('/opt/homebrew', '86400 --upgrade', False),
                 ('/opt/homebrew', -1, False), ('/opt/homebrew', 2147483648, False)]
        for prefix, interval, accepted in cases:
            with self.subTest(prefix=prefix, interval=interval):
                self.check(task, {'_packages_brew_prefix': {'stdout': prefix},
                                  'packages_brew_autoupdate_interval': interval}, accepted)

    def test_tailscale_restart_only_for_a_running_service(self):
        handlers = yaml.safe_load((ROOT / 'roles/system/handlers/main.yml').read_text())
        handler = next(task for task in handlers if task['name'] == 'Restart tailscaled')
        for container, services, expected in [(False, {}, False),
                                              (False, {'tailscaled.service': {'state': 'stopped'}}, False),
                                              (False, {'tailscaled.service': {'state': 'running'}}, True),
                                              (True, {'tailscaled.service': {'state': 'running'}}, False)]:
            with self.subTest(container=container, services=services):
                tasks = [{'ansible.builtin.debug': {'msg': 'restart selected'},
                          'when': handler['when'], 'register': '_handler_probe'},
                         {'ansible.builtin.assert': {'that': f'(_handler_probe is not skipped) == {str(expected).lower()}'}}]
                self.check(tasks, {'system_is_container': container, 'ansible_facts': {'services': services}}, True)


class WrapperBashTests(unittest.TestCase):
    """verify-ansible-args.sh sets IFS=$'\\n\\t'; under bash 3.2 "${arr[@]:N}" then joins into one word."""

    source = (ROOT / 'scripts/verify-ansible-args.sh').read_text()

    def test_no_array_slices(self):
        self.assertNotIn('[@]:', ''.join(l.split('#')[0] for l in self.source.splitlines()))

    def test_run_log_keeps_every_argument_separate(self):
        begin = self.source.index('args=("$@")')
        end = self.source.index('    if [[ ! "$_run_log_name"', begin)
        body = self.source[begin:end] + 'fi\nprintf "[%s]" "${args[@]}"\n'
        out = subprocess.run(['bash', '-c', "set -euo pipefail; IFS=$'\\n\\t'\n" + body, 'w',
                              '--run-log=x', 'ansible-playbook', '--syntax-check', '-i', 'y'],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout, '[ansible-playbook][--syntax-check][-i][y]', out.stderr)

    def test_old_or_non_bash_shells_are_refused_before_any_scan(self):
        gate = self.source.index('BASH_VERSINFO')
        self.assertLess(gate, self.source.index('IFS='))
        self.assertLess(gate, self.source.index('args=("$@")'))
        old_bash = Path('/bin/bash')
        version = subprocess.run([str(old_bash), '-c', 'echo ${BASH_VERSINFO[0]}'], capture_output=True,
                                 text=True).stdout.strip() if old_bash.exists() else ''
        if version.isdigit() and int(version) < 4:  # macOS: the real wrapper under the system bash
            out = subprocess.run([str(old_bash), str(ROOT / 'scripts/verify-ansible-args.sh'), 'ansible-playbook',
                                  '--syntax-check', '-i', 'x', 'site.yml'], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 1)
            self.assertIn('needs bash 4 or later', out.stderr)


class IsolatedPythonTests(unittest.TestCase):
    """A sourceless scripts/json.pyc would shadow the stdlib json for any Python run without -I from scripts/."""

    def test_planted_json_bytecode_does_not_run_in_the_config_guard(self):
        import py_compile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/parse-config.py', root / 'scripts/parse-config.py')
            # the --policy step reads the wrapper's protected-key list
            shutil.copy2(ROOT / 'scripts/verify-ansible-args.sh', root / 'scripts/verify-ansible-args.sh')
            # and the vault_* mappings in the tracked group_vars (fails closed without any)
            (root / 'group_vars/all').mkdir(parents=True)
            for name in ('vars.yml', 'rollout.yml'):
                if (ROOT / 'group_vars/all' / name).exists():
                    shutil.copy2(ROOT / 'group_vars/all' / name, root / 'group_vars/all' / name)
            marker = root / 'PLANTED'
            evil = root / 'evil_json.py'
            evil.write_text(f"open({str(marker)!r}, 'w').write('planted')\n"
                            "def dumps(*args, **kwargs):\n    return '{}'\n")
            py_compile.compile(str(evil), cfile=str(root / 'scripts/json.pyc'), doraise=True)
            (root / 'config.yml').write_text('profile: work\n')
            (root / 'play.yml').write_text(yaml.safe_dump([{
                'hosts': 'localhost', 'gather_facts': False,
                'tasks': [{'ansible.builtin.include_tasks': str(ROOT / 'common/tasks/config_ssti_guard.yml')},
                          {'ansible.builtin.assert': {'that': "_pf_config_raw.profile == 'work'"}}]}]))
            env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'))
            env.pop('MOLECULE_PROJECT_DIRECTORY', None)
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(root / 'play.yml')], cwd=root,
                                    env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists(), 'parse-config.py imported the planted scripts/json.pyc')

    def test_wrapper_sets_safe_path(self):
        source = (ROOT / 'scripts/verify-ansible-args.sh').read_text()
        self.assertRegex(source, r'(?m)^export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1$')
        self.assertIn('"$_py" -I "${_repo_root}/scripts/rollout.py"', source)


if __name__ == '__main__':
    unittest.main()
