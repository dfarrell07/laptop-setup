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
        task = next(t for t in block if t.get('name', '').startswith('Warn on unmanaged systemd user unit files'))
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
        recognize = next(t for t in tasks if t.get('name') == 'Recognize the safe empty SSH audit watch directory')
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
                   .replace("{{ ansible_facts['user_id'] }}", 'fixture')
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
                    'ansible_facts': {'user_id': 'fixture', 'user_uid': 1000},
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
        checks = block[1:]
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
                        '_existing_authkeys': {'content': base64.b64encode(content.encode()).decode()},
                        'ssh_pubkey_accepted_algorithms': ['ssh-ed25519', 'sk-ssh-ed25519@openssh.com'],
                    }, expected, SSH_ENV if over_ssh else None)

    def test_go_checksum_server(self):
        task = guard('roles/packages/tasks/install_go_tools.yml', '(2/3)')
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
                settings = {'sandbox': {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False},
                            'enableAllProjectMcpServers': False,
                            'permissions': {'deny': ['Read(~/.claude-work/.credentials.json)',
                                                     'Edit(**/.git/hooks/**)', 'Edit(**/.githooks/**)']}}
                if failure == 'self-lockout':
                    settings['permissions']['deny'].append('Read(~/.claude-work/**)')
                if failure == 'sandbox':
                    settings['sandbox']['enabled'] = False
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
                 ({'bad;key': 1}, False), ({'safe': '1\n'}, False), ({'safe': '1;bad'}, False)]
        for extra, accepted in cases:
            with self.subTest(extra=extra):
                self.check(task, {'system_is_container': False, 'csb_rhel': False,
                                  'system_sysctl_extra': extra, 'system_sysctl_extra_additional': {}}, accepted)

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

    def test_vendor_polkit_rules_fail_closed(self):
        source = yaml.safe_load((ROOT / 'roles/system/tasks/hardening_core.yml').read_text())
        block = next(task['block'] for task in source if task['name'] == 'Polkit rules directory scan')
        tasks = block[-2:]  # Execute the production classification and rejection tasks.
        vendor = '/etc/polkit-1/rules.d/49-polkit-pkla-compat.rules'
        unknown = '/etc/polkit-1/rules.d/unknown.rules'
        cases = [([vendor], 'polkit-pkla-compat', 0, '', True),
                 ([vendor], 'other-package', 0, '', False),
                 ([vendor], 'polkit-pkla-compat', 1, '', False),
                 ([vendor], 'polkit-pkla-compat', 0, 'modified file', False),
                 ([vendor, unknown], 'polkit-pkla-compat', 0, '', False),
                 ([], '', 1, '', True)]
        for files, owner, rc, output, accepted in cases:
            with self.subTest(files=files, owner=owner, rc=rc, output=output):
                self.check(tasks, {'system_polkitd_whitelist': [],
                                  '_polkit_rules_found': {'files': [{'path': path, 'isdir': False} for path in files]},
                                  '_polkit_compat_owner': {'rc': 0, 'stdout': owner},
                                  '_polkit_compat_integrity': {'rc': rc, 'stdout': output}}, accepted)

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


if __name__ == '__main__':
    unittest.main()
