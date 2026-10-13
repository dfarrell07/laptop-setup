#!/usr/bin/env python3
"""Exercise workstation privacy, power transitions and portable preference settings."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('workstation_audit', ROOT / 'scripts/workstation-audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TEST_TIMEOUT = int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0'))


class WorkstationAuditTests(unittest.TestCase):
    def test_malformed_pipx_metadata_preserves_other_report_rows(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            base = home / '.local/share/pipx/venvs'
            for index, data in enumerate([None, ['fixture-secret'], {'main_package': None}, {'main_package': []}]):
                environment = base / f'invalid-{index}'
                environment.mkdir(parents=True)
                (environment / 'pipx_metadata.json').write_text(json.dumps(data))
            valid = base / 'valid'
            valid.mkdir()
            (valid / 'pipx_metadata.json').write_text(json.dumps({'main_package': {'package_version': '1.2.3'}}))
            (base / 'without-metadata').mkdir()
            with patch.object(audit, 'probe', return_value=None), patch.object(audit.shutil, 'which', return_value=None):
                report = audit.collect(home=home, path_value='')
            self.assertEqual([row['name'] for row in report['pipx']], ['valid'])
            self.assertEqual(len(report['errors']), 4)
            self.assertNotIn('fixture-secret', json.dumps(report))
            output = home / 'report.json'
            with patch.object(audit, 'collect', return_value=report), patch('sys.argv', ['audit', '--output', str(output)]):
                self.assertEqual(audit.main(), 1)
            self.assertEqual(json.loads(output.read_text())['pipx'][0]['version'], '1.2.3')

    @unittest.skipIf(os.geteuid() == 0, 'unreadable-directory fixture needs an unprivileged user')
    def test_unreadable_pipx_paths_are_reported_probe_errors(self):
        for relative in ['.local/share/pipx', '.local/share/pipx/venvs', '.local/share/pipx/venvs/tool',
                         '.local/share/pipx/venvs/tool/pipx_metadata.json']:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as name:
                home = Path(name)
                metadata = home / '.local/share/pipx/venvs/tool/pipx_metadata.json'
                metadata.parent.mkdir(parents=True)
                metadata.write_text('{"main_package": {}}')
                blocked = home / relative
                blocked.chmod(0)
                errors = []
                try:
                    self.assertEqual(audit.pipx_state(home, errors), [])
                    base_error = relative.endswith(('/pipx', '/venvs'))
                    error_path = '~/' + ('.local/share/pipx/venvs' if base_error else str(metadata.relative_to(home)))
                    reason = 'directory unreadable' if base_error else 'metadata unreadable'
                    self.assertEqual(errors, [{'probe': 'pipx', 'path': error_path, 'reason': reason}])
                finally:
                    blocked.chmod(0o700)

    def test_pep508_provenance_redacts_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            venv = home / '.local/share/pipx/venvs/tool'
            venv.mkdir(parents=True)
            (venv / 'pipx_metadata.json').write_text(json.dumps({'main_package': {
                'package_version': '1.2.3', 'package_or_url':
                'tool[one, two] @ https://fixture-user:fixture-secret@[::1]:8443/tool.whl?token=fixture-token#private'}}))
            with patch.object(audit, 'probe', return_value=None):
                report = audit.collect(home=home, path_value='')
            serialized = json.dumps(report)
            for sensitive in ['fixture-user', 'fixture-secret', 'fixture-token', '#private']:
                self.assertNotIn(sensitive, serialized)
            self.assertEqual(report['pipx'][0]['source'], 'tool[one, two] @ https://[::1]:8443/tool.whl')

    def test_private_report_and_no_credential_content(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            config = home / '.ssh/config'
            config.parent.mkdir()
            config.write_text('fixture-secret-must-not-be-serialized\n')
            go_config = home / '.config/go/env'
            go_config.parent.mkdir(parents=True)
            go_config.write_text('GONOSUMDB=*\nGOTOOLCHAIN=auto\nGOPROXY=https://user:fixture-proxy-secret@example.invalid\n')
            venv = home / '.local/share/pipx/venvs/tool'
            (venv / 'bin').mkdir(parents=True)
            (venv / 'bin/python').symlink_to('/nonexistent/python3.13')
            (venv / 'pipx_metadata.json').write_text(json.dumps({'main_package': {
                'package_version': '1.2.3', 'package_or_url': 'https://user:fixture-password@example.invalid/repo?token=fixture-token'}}))
            with patch.object(audit, 'probe', return_value=None), patch.object(audit.shutil, 'which', return_value=None):
                result = audit.collect(home=home, path_value='')
            encoded = json.dumps(result)
            for secret in ['fixture-secret', 'fixture-password', 'fixture-token', 'fixture-proxy-secret']:
                self.assertNotIn(secret, encoded)
            self.assertEqual(result['findings']['broken_pipx_environments'], ['tool'])
            self.assertTrue(result['go_config_policy']['checksum_bypass_all'])
            self.assertTrue(result['go_config_policy']['credentialed_proxy'])
            self.assertEqual(result['pipx'][0]['source'], 'https://example.invalid/repo')
            output = home / 'report.json'
            with patch.object(audit, 'collect', return_value=result), patch('sys.argv', ['audit', '--output', str(output)]):
                self.assertEqual(audit.main(), 0)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    audit.main()

    def test_native_system_file_ownership_uses_the_available_manager(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            config_dir = home / 'system-config'
            config_dir.mkdir()
            (config_dir / 'setting.conf').write_text('private config contents\n')
            for manager in ['rpm', 'dpkg-query']:
                def probe(command, errors, label):
                    if label == 'file-owner':
                        return 'fixture-package' if manager == 'rpm' else 'fixture-package: /fixture/setting.conf\n'
                    return None
                with patch.object(audit, 'SYSTEM_DIRS', [str(config_dir)]), patch.object(audit, 'probe', side_effect=probe), \
                     patch.object(audit.shutil, 'which', side_effect=lambda tool: '/fixture/tool' if tool == manager else None):
                    report = audit.collect(home=home, path_value='')
                self.assertEqual(report['system_config_files'][0]['package_owner'], 'fixture-package')
                self.assertFalse(report['findings']['unpackaged_system_files'])
                self.assertNotIn('private config contents', json.dumps(report))

    def test_path_candidates_are_inspected_without_running_them(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            first, second = home / 'first', home / 'second'
            first.mkdir()
            second.mkdir()
            for parent in [first, second]:
                tool = parent / 'go'
                tool.write_text('#!/bin/sh\nexit 99\n')
                tool.chmod(0o700)
            report = audit.command_paths(['go'], ':'.join([str(first), str(first), str(second)]), home)
            self.assertEqual(report['go']['selected'], '~/first/go')
            self.assertTrue(report['go']['shadowed'])
            self.assertEqual(len(report['go']['candidates']), 2)

    def test_probe_errors_never_publish_stderr(self):
        class Failed:
            returncode = 1
            stdout = 'private stdout'
            stderr = 'fixture credential in error output'
        errors = []
        with patch.object(audit.shutil, 'which', return_value='/fixture/tool'), patch.object(audit.subprocess, 'run', return_value=Failed()):
            self.assertIsNone(audit.probe(['tool'], errors, 'fixture'))
        self.assertNotIn('credential', json.dumps(errors))
        self.assertEqual(errors[0]['exit_code'], 1)


MOCK_MODULE = '''from ansible.module_utils.basic import AnsibleModule
import base64, json
module = AnsibleModule(argument_spec=dict(operation=dict(type='str'), arguments=dict(type='dict'),
                                         fixture=dict(type='dict'), logfile=dict(type='str')))
p = module.params
with open(p['logfile'], 'a') as stream:
    stream.write(json.dumps({'operation': p['operation'], 'arguments': p['arguments']}) + '\\n')
failure = p['fixture'].get('failure')
a = p['arguments']
if p['operation'] == 'dnf' and a.get('state') == 'present':
    if failure == 'tlp_install' and a.get('name') == ['tlp', 'tlp-rdw']:
        module.fail_json(msg='fixture TLP install failure')
    if failure == 'native_install' and a.get('name') == ['tuned', 'tuned-ppd']:
        module.fail_json(msg='fixture native install failure')
if p['operation'] == 'systemd' and a.get('state') in ['started', 'restarted']:
    if a.get('masked'):
        module.fail_json(msg='fixture cannot start a masked service')
    if failure == 'tlp_start' and a.get('name') == 'tlp.service' and sum(
        json.loads(line)['operation'] == 'systemd' and
        json.loads(line)['arguments'].get('name') == 'tlp.service' and
        json.loads(line)['arguments'].get('state') in ['started', 'restarted']
        for line in open(p['logfile'])) == 1:
        module.fail_json(msg='fixture TLP startup failure')
    if failure == 'native_start' and a.get('name') == 'tuned-ppd.service':
        module.fail_json(msg='fixture native startup failure')
result = dict(changed=False)
if p['operation'] == 'command':
    exists = p['fixture']['tlp_active'] if a['argv'][1] == 'is-active' else p['fixture']['tlp_exists']
    result['rc'] = 0 if exists else 1
if p['operation'] == 'stat':
    kind = p['fixture'].get('config_kind', 'file')
    result['stat'] = dict(exists=p['fixture']['config_exists'], mode=p['fixture']['config_mode'],
                          isreg=kind == 'file', islnk=kind == 'symlink')
if p['operation'] == 'slurp': result['content'] = base64.b64encode(b'TLP_ENABLE=1\\n# previous settings\\n').decode()
if p['operation'] == 'template': result['changed'] = p['fixture'].get('config_changed', False)
if p['operation'] == 'package_facts':
    result['ansible_facts'] = dict(packages=({'tlp': [], 'tlp-rdw': []} if p['fixture']['tlp_exists']
                                            else {'tuned': [], 'tuned-ppd': []}))
if p['operation'] == 'service_facts':
    result['ansible_facts'] = dict(services=({'tlp.service': p['fixture']['tlp_service']}
        if p['fixture']['tlp_exists'] else {
            'tuned.service': {'state': 'running', 'status': 'enabled'},
            'tuned-ppd.service': {'state': 'running', 'status': 'enabled'}}))
    for name, status in p['fixture'].get('native_status', {}).items():
        result['ansible_facts']['services'][name] = {
            'state': 'running' if name in p['fixture'].get('native_running', []) else 'stopped', 'status': status}
module.exit_json(**result)
'''


class PowerPolicyTests(unittest.TestCase):
    def test_native_power_dropins_survive_default_cleanup(self):
        source = yaml.safe_load((ROOT / 'roles/system/tasks/service_masking.yml').read_text())
        cleanup = next(t for t in source if t.get('name') == 'Remove pre-existing drop-in directories for managed masked units')
        default_enabled = yaml.safe_load((ROOT / 'roles/system/defaults/main.yml').read_text())['system_tlp_enabled']
        native = ['tuned.service', 'tuned-ppd.service', 'power-profiles-daemon.service']
        for enabled in [default_enabled, True]:
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                for unit in native + ['bluetooth.service']:
                    dropin = directory / (unit + '.d')
                    dropin.mkdir()
                    (dropin / 'existing.conf').write_text('[Service]\n')
                task = copy.deepcopy(cleanup)
                task['loop'] = native + ['bluetooth.service']
                task['ansible.builtin.file']['path'] = name + '/{{ item }}.d'
                playbook = directory / 'play.yml'
                playbook.write_text(yaml.safe_dump([{'name': 'Isolated production drop-in cleanup',
                    'hosts': 'localhost', 'gather_facts': False, 'vars': {
                        'system_tlp_enabled': enabled, 'system_is_container': False,
                        'csb_rhel': False, 'csb_detected': False}, 'tasks': [task]}]))
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                    cwd=ROOT, env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                    capture_output=True, text=True, timeout=max(30, TEST_TIMEOUT))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for unit in native:
                    self.assertTrue((directory / (unit + '.d') / 'existing.conf').exists())
                self.assertFalse((directory / 'bluetooth.service.d').exists())

    def exercise(self, enabled, previous=False, container=False, csb=False, distribution='Fedora', version='44', failure=None, managed=None,
                 native_status=None, tlp_state='running', tlp_status='enabled', config_changed=False, config_kind='file',
                 tlp_active=None, config_mode='0644', native_running=None):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            library = directory / 'library'
            library.mkdir()
            (library / 'mock_power.py').write_text(MOCK_MODULE)
            logfile = directory / 'operations.jsonl'
            tasks = copy.deepcopy(yaml.safe_load((ROOT / 'roles/system/tasks/power_management.yml').read_text()))
            def mock(items):
                for task in items:
                    for nested in ['block', 'rescue', 'always']:
                        if nested in task:
                            mock(task[nested])
                    for operation in ['dnf', 'systemd', 'file', 'template', 'command', 'stat', 'slurp', 'copy', 'package_facts', 'service_facts']:
                        key = 'ansible.builtin.' + operation
                        if key in task:
                            arguments = task.pop(key)
                            task['mock_power'] = {'operation': operation, 'arguments': arguments,
                                                  'fixture': '{{ fixture }}', 'logfile': str(logfile)}
            mock(tasks)
            gate = next(t['when'] for t in yaml.safe_load((ROOT / 'roles/system/tasks/service_masking.yml').read_text())
                        if t.get('name') == 'Configure laptop power management')
            play = [{'name': 'Production power policy with isolated package/service probes', 'hosts': 'localhost',
                     'gather_facts': False, 'vars': {
                         'is_dnf': True, 'system_is_container': container, 'csb_rhel': csb,
                         'system_tlp_enabled': enabled, 'ansible_distribution': distribution,
                         'ansible_distribution_major_version': version,
                         'fixture': {'tlp_exists': previous, 'config_exists': previous if managed is None else managed,
                                     'failure': failure, 'native_status': native_status or {}, 'native_running': native_running or [],
                                     'tlp_service': {'state': tlp_state, 'status': tlp_status},
                                     'tlp_active': previous and (tlp_state == 'running' if tlp_active is None else tlp_active),
                                     'config_changed': config_changed, 'config_kind': config_kind, 'config_mode': config_mode}},
                     'tasks': [{'name': 'Production platform gate', 'block': tasks, 'when': gate}]}]
            playbook = directory / 'play.yml'
            playbook.write_text(yaml.safe_dump(play))
            env = dict(os.environ, ANSIBLE_LIBRARY=str(library), ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'))
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=max(45, TEST_TIMEOUT))
            self.assertEqual(result.returncode, 2 if failure else 0, result.stdout + result.stderr)
            return [json.loads(line) for line in logfile.read_text().splitlines()] if logfile.exists() else []

    def test_tlp_package_transition_precedes_startup(self):
        operations = self.exercise(True)
        packages = [r['arguments'] for r in operations if r['operation'] == 'dnf']
        self.assertEqual(packages[0]['state'], 'absent')
        self.assertIn('tuned', packages[0]['name'])
        self.assertEqual(packages[1]['name'], ['tlp', 'tlp-rdw'])
        self.assertFalse(packages[1]['install_weak_deps'])
        units = [r['arguments'] for r in operations if r['operation'] == 'systemd']
        self.assertEqual([u['name'] for u in units if u.get('state') in ['started', 'restarted']], ['tlp.service'])
        self.assertFalse(units[-1]['masked'])
        self.assertTrue(all(u.get('masked') for u in units[:-1]))

    def test_opt_out_restores_exactly_one_native_stack(self):
        for distribution, version, expected in [('Fedora', '44', ['tuned.service', 'tuned-ppd.service']),
                                                 ('Fedora', '40', ['power-profiles-daemon.service']),
                                                 ('Rocky', '10', ['tuned.service'])]:
            with self.subTest(distribution=distribution, version=version):
                operations = self.exercise(False, previous=True, distribution=distribution, version=version)
                units = [r['arguments']['name'] for r in operations
                         if r['operation'] == 'systemd' and r['arguments'].get('state') == 'started']
                self.assertEqual(units, expected)
                package_calls = [r['arguments'] for r in operations if r['operation'] == 'dnf']
                self.assertIn('tlp', package_calls[0]['name'])
                self.assertEqual(package_calls[0]['state'], 'absent')

    def test_unconfigured_csb_and_containers_have_no_power_mutations(self):
        for enabled, previous, container, csb in [(True, False, True, False), (True, False, False, True),
                                                   (False, False, False, False)]:
            operations = self.exercise(enabled, previous, container, csb)
            self.assertFalse(any(r['operation'] in ['dnf', 'systemd', 'file', 'template'] for r in operations))

    def test_failed_install_restores_the_previous_running_stack(self):
        operations = self.exercise(True, failure='tlp_install')
        self.assertFalse(any(r['operation'] == 'template' for r in operations))
        packages = [r['arguments'] for r in operations if r['operation'] == 'dnf']
        self.assertEqual(set(packages[-1]['name']), {'tuned', 'tuned-ppd'})
        units = [r['arguments']['name'] for r in operations if r['operation'] == 'systemd']
        self.assertEqual(units, ['tuned.service', 'tuned-ppd.service', 'power-profiles-daemon.service',
                                 'tuned.service', 'tuned-ppd.service'])

    def test_late_tlp_start_failure_restores_native_stack(self):
        operations = self.exercise(True, failure='tlp_start')
        calls = [r['arguments'] for r in operations if r['operation'] == 'dnf']
        self.assertEqual(calls[-2]['state'], 'absent')
        self.assertIn('tlp', calls[-2]['name'])
        self.assertEqual(set(calls[-1]['name']), {'tuned', 'tuned-ppd'})
        units = [r['arguments'] for r in operations if r['operation'] == 'systemd']
        self.assertEqual([u['name'] for u in units[-2:]], ['tuned.service', 'tuned-ppd.service'])
        self.assertTrue(all(u['state'] == 'started' and not u['masked'] for u in units[-2:]))
        # PPD was absent from initial service facts: the failed transition's new mask still must be cleared.
        self.assertEqual([u['masked'] for u in units if u['name'] == 'power-profiles-daemon.service'], [True, False])
        self.assertTrue(any(r['operation'] == 'file' and r['arguments']['state'] == 'absent'
                            for r in operations))

    def test_failed_tlp_transition_starts_native_units_before_restoring_their_masks(self):
        native = ['tuned.service', 'tuned-ppd.service']
        for failure in ['tlp_install', 'tlp_start']:
            with self.subTest(failure=failure):
                operations = self.exercise(True, failure=failure,
                    native_status={name: 'masked' for name in native}, native_running=native)
                units = [r['arguments'] for r in operations if r['operation'] == 'systemd']
                for name in native:
                    calls = [u for u in units if u['name'] == name]
                    self.assertEqual(calls[-2]['state'], 'started')
                    self.assertFalse(calls[-2]['masked'])
                    self.assertNotIn('enabled', calls[-2])
                    self.assertEqual(calls[-1], {'name': name, 'masked': True})
                # A failed rescue start would abort before configuration recovery.
                self.assertTrue(any(r['operation'] == 'file' and r['arguments']['state'] == 'absent'
                                    for r in operations))

    def test_failed_native_transition_restores_tlp_and_retains_config(self):
        for failure in ['native_install', 'native_start']:
            with self.subTest(failure=failure):
                operations = self.exercise(False, previous=True, failure=failure,
                    native_status={'tuned.service': 'disabled', 'tuned-ppd.service': 'masked'})
                calls = [r['arguments'] for r in operations if r['operation'] == 'dnf']
                self.assertEqual(calls[-2]['name'], ['tuned', 'tuned-ppd'])
                self.assertEqual(calls[-2]['state'], 'absent')
                self.assertEqual(set(calls[-1]['name']), {'tlp', 'tlp-rdw'})
                units = [r['arguments'] for r in operations if r['operation'] == 'systemd']
                self.assertEqual(units[-1]['name'], 'tlp.service')
                self.assertEqual(units[-1]['state'], 'started')
                self.assertTrue(units[-1]['enabled'])
                self.assertEqual([u['name'] for u in units[-3:-1]], ['tuned.service', 'tuned-ppd.service'])
                self.assertEqual([u['masked'] for u in units[-3:-1]], [False, True])
                self.assertFalse(any(r['operation'] == 'file' for r in operations))

    def test_unmanaged_tlp_survives_default_opt_out(self):
        operations = self.exercise(False, previous=True, managed=False)
        self.assertFalse(any(r['operation'] in ['dnf', 'systemd', 'file', 'template'] for r in operations))

    def test_failed_opt_out_preserves_stopped_enabled_tlp(self):
        operations = self.exercise(False, previous=True, failure='native_install', tlp_state='stopped')
        units = [r['arguments'] for r in operations if r['operation'] == 'systemd'
                 and r['arguments']['name'] == 'tlp.service']
        self.assertEqual(units[-1]['state'], 'stopped')
        self.assertTrue(units[-1]['enabled'])

    def test_failed_power_transitions_restore_active_exited_tlp(self):
        # TLP is a RemainAfterExit oneshot; service_facts calls it stopped even while active.
        for enabled, failure in [(True, 'tlp_start'), (False, 'native_install')]:
            with self.subTest(enabled=enabled):
                operations = self.exercise(enabled, previous=True, failure=failure,
                    tlp_state='stopped', tlp_active=True, config_changed=True)
                units = [r['arguments'] for r in operations if r['operation'] == 'systemd'
                         and r['arguments']['name'] == 'tlp.service']
                self.assertEqual(units[-1]['state'], 'started')
                self.assertTrue(units[-1]['enabled'])

    def test_failed_power_transitions_start_active_tlp_before_restoring_its_mask(self):
        for enabled, failure in [(True, 'tlp_start'), (False, 'native_install')]:
            with self.subTest(enabled=enabled):
                operations = self.exercise(enabled, previous=True, failure=failure,
                    tlp_state='stopped', tlp_status='masked', tlp_active=True, config_changed=True)
                units = [r['arguments'] for r in operations if r['operation'] == 'systemd'
                         and r['arguments']['name'] == 'tlp.service']
                self.assertEqual(units[-2]['state'], 'started')
                self.assertFalse(units[-2]['masked'])
                self.assertNotIn('enabled', units[-2])
                self.assertEqual(units[-1], {'name': 'tlp.service', 'masked': True})

    def test_power_transition_rejects_non_regular_config_before_mutations(self):
        for enabled in [True, False]:
            for kind in ['symlink', 'directory']:
                with self.subTest(enabled=enabled, kind=kind):
                    operations = self.exercise(enabled, previous=True, failure='config_kind', config_kind=kind)
                    self.assertFalse(any(r['operation'] in ['dnf', 'systemd', 'file', 'template', 'copy', 'slurp']
                                         for r in operations))

    def test_failed_tlp_reconfiguration_restores_content_mode_and_service(self):
        for state, status in [('running', 'enabled'), ('stopped', 'enabled'), ('stopped', 'disabled'), ('stopped', 'masked')]:
            with self.subTest(state=state, status=status):
                operations = self.exercise(True, previous=True, failure='tlp_start', config_changed=True,
                    tlp_state=state, tlp_status=status, config_mode='0600',
                    native_status={'tuned.service': 'masked', 'tuned-ppd.service': 'masked'})
                copies = [r['arguments'] for r in operations if r['operation'] == 'copy']
                self.assertEqual(copies, [{'content': 'TLP_ENABLE=1\n# previous settings\n',
                    'dest': '/etc/tlp.d/50-thinkpad.conf', 'mode': '0600'}])
                units = [r['arguments'] for r in operations if r['operation'] == 'systemd']
                tlp_units = [u for u in units if u['name'] == 'tlp.service']
                self.assertEqual(tlp_units[0]['state'], 'restarted')
                self.assertEqual(tlp_units[-1]['state'], 'started' if state == 'running' else 'stopped')
                self.assertEqual(tlp_units[-1]['masked'], status == 'masked')
                if status in ['enabled', 'disabled']:
                    self.assertEqual(tlp_units[-1]['enabled'], status == 'enabled')
                else:
                    self.assertNotIn('enabled', tlp_units[-1])
                native_masks = {u['name']: u['masked'] for u in units if u['name'] != 'tlp.service'}
                self.assertEqual(native_masks, {'tuned.service': True, 'tuned-ppd.service': True,
                    'power-profiles-daemon.service': False})
                self.assertFalse(any(u.get('state') in ['started', 'restarted'] for u in units if u['name'] != 'tlp.service'))
                self.assertFalse(any(r['operation'] == 'file' and r['arguments'].get('state') == 'absent'
                                     for r in operations))

    def test_tlp_rerun_with_unchanged_config_does_not_restart(self):
        operations = self.exercise(True, previous=True)
        units = [r['arguments'] for r in operations if r['operation'] == 'systemd']
        self.assertEqual([u['state'] for u in units if u['name'] == 'tlp.service'], ['started'])
        self.assertFalse(any(r['operation'] == 'copy' for r in operations))

    def test_failed_tlp_rerun_restores_unmasked_masked_and_absent_native_units(self):
        operations = self.exercise(True, previous=True, failure='tlp_start', config_changed=True,
            native_status={'tuned.service': 'disabled', 'tuned-ppd.service': 'masked'})
        units = [r['arguments'] for r in operations if r['operation'] == 'systemd']
        for name, expected in [('tuned.service', False), ('tuned-ppd.service', True),
                               ('power-profiles-daemon.service', False)]:
            calls = [u for u in units if u['name'] == name]
            self.assertTrue(calls[0]['masked'])
            self.assertEqual(calls[-1]['masked'], expected)
            self.assertNotIn('state', calls[-1])

    def test_failed_tlp_configuration_without_previous_file_removes_new_file(self):
        operations = self.exercise(True, previous=True, managed=False, failure='tlp_start', config_changed=True)
        self.assertFalse(any(r['operation'] == 'slurp' for r in operations))
        self.assertFalse(any(r['operation'] == 'copy' for r in operations))
        self.assertTrue(any(r['operation'] == 'file' and r['arguments'].get('state') == 'absent'
                            for r in operations))


class PortablePreferencesTests(unittest.TestCase):
    def test_additive_package_guard_rejects_cli_arguments(self):
        task = next(task for task in yaml.safe_load((ROOT / 'roles/packages/tasks/main.yml').read_text())
                    if task.get('name') == 'Validate additive native package lists')
        for extras, expected in [(['ccache', 'openssl-devel'], 0), (['--nogpgcheck'], 2), (['package with spaces'], 2), ({'ccache': 'ignored'}, 2)]:
            with tempfile.TemporaryDirectory() as name:
                playbook = Path(name) / 'play.yml'
                playbook.write_text(yaml.safe_dump([{'name': 'Production additive package guard', 'hosts': 'localhost',
                    'gather_facts': False, 'vars': {'packages_extra_dnf': extras, 'packages_extra_apt': [],
                                                  'packages_extra_brew': []}, 'tasks': [task]}]))
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                    cwd=ROOT, env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                    capture_output=True, text=True, timeout=max(30, TEST_TIMEOUT))
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('dbus-run-session') and shutil.which('dconf'), 'isolated DBus/dconf required')
    def test_gnome_preferences_round_trip_and_repeat(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            tasks = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
            block = next(task['block'] for task in tasks if task.get('name') == 'GNOME hardening dconf settings')
            apply = next(task for task in block if task.get('name') == 'Apply GNOME dconf hardening and UI settings')
            guard = next(task for task in block if task.get('name') == 'Validate GNOME preference types')
            defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
            defaults['desktop_gnome_hardening_dconf'] = []
            defaults['ansible_facts'] = {'system': 'Linux'}
            probe = [{'name': 'Read typed preference values', 'community.general.dconf': {'state': 'read',
                      'key': '{{ item.key }}'}, 'loop': '{{ desktop_gnome_ui_dconf }}', 'register': '_read'},
                     {'name': 'Assert values persisted', 'ansible.builtin.assert': {'that': [
                      "_read.results[0].value == \"'24h'\"", "_read.results[1].value == 'true'",
                      "_read.results[2].value == 'true'", "_read.results[4].value == 'false'",
                      "_read.results[5].value == '4'", "_read.results[6].value == \"['lv3:ralt_switch']\""]}}]
            play = [{'name': 'Isolated GNOME user preference test', 'hosts': 'localhost', 'gather_facts': False,
                     'vars': defaults, 'tasks': [guard, apply, *probe]}]
            playbook = directory / 'play.yml'
            playbook.write_text(yaml.safe_dump(play))
            env = dict(os.environ, XDG_CONFIG_HOME=str(directory / 'config'),
                       ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'),
                       ANSIBLE_COLLECTIONS_PATH=str(ROOT / 'collections'))
            for repeat in [False, True]:
                result = subprocess.run(['dbus-run-session', '--', str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=max(45, TEST_TIMEOUT))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if repeat:
                    self.assertIn('changed=0', result.stdout)


class GoPolicyTests(unittest.TestCase):
    def test_effective_private_pattern_guard(self):
        task = next(task for task in yaml.safe_load((ROOT / 'roles/packages/tasks/install_go_tools.yml').read_text())
                    if '(1/3)' in task.get('name', ''))
        for private, expected in [('corp.invalid', 0), ('*.redhat.com', 0), ('github.com/private-owner', 0),
                                  ('github.com', 2), ('github.com/*', 2), ('github.*', 2), ('*.*', 2), ('*', 2)]:
            with tempfile.TemporaryDirectory() as name:
                playbook = Path(name) / 'play.yml'
                playbook.write_text(yaml.safe_dump([{'name': 'Effective checksum exception guard', 'hosts': 'localhost',
                    'gather_facts': False, 'vars': {'dotfiles_gonosumdb': '', 'dotfiles_goprivate': private}, 'tasks': [task,
                        next(t for t in yaml.safe_load((ROOT / 'roles/packages/tasks/install_subctl_versions.yml').read_text())
                             if t.get('name') == 'Validate versioned subctl checksum exceptions')]}]))
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                    cwd=ROOT, env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'),
                                       ANSIBLE_FILTER_PLUGINS=str(ROOT / 'roles/packages/filter_plugins')),
                    capture_output=True, text=True, timeout=max(30, TEST_TIMEOUT))
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('go'), 'real Go environment regression requires Go')
    def test_production_install_environments_override_persistent_patterns(self):
        def environments(items):
            result = []
            for task in items:
                if 'GONOSUMDB' in task.get('environment', {}):
                    result.append(task['environment'])
                for key in ['block', 'rescue', 'always']:
                    result.extend(environments(task.get(key, [])))
            return result
        for filename in ['install_go_tools.yml', 'install_subctl_versions.yml']:
            source = yaml.safe_load((ROOT / 'roles/packages/tasks' / filename).read_text())
            production = environments(source)[0]
            for private in ['', 'corp.invalid']:
                with self.subTest(filename=filename, private=private), tempfile.TemporaryDirectory() as name:
                    directory = Path(name)
                    go_env = directory / 'goenv'
                    go_env.write_text('GONOSUMDB=*\nGOPRIVATE=stale.invalid\n')
                    environment = {key: production[key] for key in ['GONOSUMDB', 'GOPRIVATE']}
                    environment.update({'GOENV': str(go_env), 'GOTOOLCHAIN': 'local'})
                    tasks = [{'name': 'Probe the production Go install environment', 'ansible.builtin.command': {
                        'argv': [shutil.which('go'), 'env', 'GONOSUMDB', 'GOPRIVATE'], 'chdir': name},
                        'environment': environment, 'register': '_go', 'changed_when': False},
                        {'name': 'Assert explicit pattern overrides', 'ansible.builtin.assert': {
                            'that': ['_go.stdout_lines == expected']}}]
                    playbook = directory / 'play.yml'
                    playbook.write_text(yaml.safe_dump([{'name': 'GoENV policy regression', 'hosts': 'localhost',
                        'gather_facts': False, 'vars': {'dotfiles_gonosumdb': '', 'dotfiles_goprivate': private,
                                                       'expected': [private or 'none', private or 'none']}, 'tasks': tasks}]))
                    result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                        cwd=ROOT, env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                        capture_output=True, text=True, timeout=max(30, TEST_TIMEOUT))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
