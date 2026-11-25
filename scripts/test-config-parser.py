#!/usr/bin/env python3
"""Regression coverage for raw YAML parsing before template-injection checks."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TEST_TIMEOUT = max(60, int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0')))

spec = importlib.util.spec_from_file_location("config_parser", Path(__file__).with_name("parse-config.py"))
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


class ConfigParserTests(unittest.TestCase):
    def parse(self, content):
        return json.loads(parser.parse_config(content))

    def test_preserves_yaml_types_and_unsafe_literals(self):
        self.assertEqual(
            self.parse("name: !unsafe 'Valid User'\nenabled: true\ncount: 3\npaths: [one, two]\n"),
            {"name": "Valid User", "enabled": True, "count": 3, "paths": ["one", "two"]},
        )

    def test_keeps_template_markers_without_executing_lookups(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "executed"
            payload = "{{ lookup('pipe', 'touch " + str(marker) + "') }}"
            for content in (
                'value: "' + payload + '"\n',
                'value: !unsafe "' + payload + '"\n',
                'value: ["' + payload + '"]\n',
                'nested:\n  value: "' + payload + '"\n',
            ):
                with self.subTest(content=content):
                    self.assertIn(payload, json.dumps(self.parse(content)))
                    self.assertFalse(marker.exists())

    def test_rejects_python_object_tags(self):
        with self.assertRaises(Exception):
            self.parse("value: !!python/object/apply:os.getpid []\n")

    def test_preserves_literal_unsafe_word_inside_a_string(self):
        self.assertEqual(self.parse("value: 'literal !unsafe text'"), {"value": "literal !unsafe text"})

    def test_empty_document_is_an_empty_mapping(self):
        self.assertEqual(self.parse(""), {})

    def test_rejects_non_mapping_documents(self):
        for content in ("[one, two]", "123", "some text"):
            with self.subTest(content=content), self.assertRaises(TypeError):
                self.parse(content)


class ConfigPolicyTests(unittest.TestCase):
    """config.yml keys and characters that would change what runs, or how, are rejected."""

    def violations(self, content):
        return parser.policy_violations(parser.load(content))

    def assertRejected(self, content, needle):
        found = self.violations(content)
        self.assertTrue(any(needle in line for line in found), found)

    def test_default_config_and_ordinary_values_pass(self):
        self.assertEqual(self.violations((ROOT / 'default.config.yml').read_text()), [])
        self.assertEqual(self.violations(
            "dotfiles_user_name: Zoë Ünïcode\nrollout_enable: [ssh_client]\nssh_port: 722\n"
            "system_sysctl_extra: {vm.swappiness: 10}\nsystem_usbguard_extra_rules: ['allow id 046d:c52b']\n"), [])

    def test_rejects_ansible_connection_and_behaviour_keys(self):
        for key in ['ansible_python_interpreter', 'ansible_become_exe', 'ansible_become_user', 'ansible_become_method',
                    'ansible_shell_executable', 'ansible_connection', 'ANSIBLE_python_interpreter']:
            with self.subTest(key=key):
                self.assertRejected(key + ': /tmp/x\n', repr(key))
        # ansible_facts from config.yml would beat gathered and package facts (repo cleanup, architecture guards)
        self.assertRejected('ansible_facts:\n  packages: {}\n  architecture: x86_64\n', repr('ansible_facts'))

    def test_rejects_internal_and_protected_keys(self):
        for key in ['_csb_molecule_force', '_pf_vault_asserted', '_rollout_disabled', '_system_sysctl_work_defaults',
                    '_anything', 'csb_rhel', 'system_sysctl_hardening', 'common_project_root', 'rollout_catalog']:
            with self.subTest(key=key):
                self.assertRejected(key + ': true\n', repr(key))
        self.assertEqual(self.violations('rollout_enable: all\n'), [])

    def test_protected_keys_come_from_the_wrapper(self):
        wrapper = (ROOT / 'scripts/verify-ansible-args.sh').read_text()
        self.assertIn("_protected_keys='" + parser.protected_keys_pattern().pattern + "'", wrapper)
        with tempfile.TemporaryDirectory() as directory:  # fail closed when the list cannot be read
            (Path(directory) / 'scripts').mkdir()
            (Path(directory) / 'scripts/verify-ansible-args.sh').write_text('#!/bin/bash\n')
            with self.assertRaises(parser.PolicySourceError):
                parser.protected_keys_pattern(Path(directory))

    def vars_root(self, directory, files):
        root = Path(directory)
        for name, text in dict({'vars.yml': 'mapped_secret: "{{ vault_mapped_secret | default(\'\') }}"\n'}, **files).items():
            (root / 'group_vars/all').mkdir(parents=True, exist_ok=True)
            (root / 'group_vars/all' / name).write_text(text)
        return root

    def test_vars_files_with_ansible_tags_or_lists_still_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.vars_root(directory, {
                'local_repos.yml': "git_repos_local: !unsafe [{url: 'git@internal.example:a/b.git'}]\n"
                                   "tagged: !unsafe '{{ vault_tagged }}'\n"
                                   "secret: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  3132\n",
                'list.yml': '- one\n- two\n', 'empty.yml': ''})
            self.assertEqual(parser.vault_mappings(root), {'mapped_secret': {'vault_mapped_secret'}, 'tagged': {'vault_tagged'}})

    def test_unreadable_vars_file_error_names_the_file_not_its_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.vars_root(directory, {'local_repos.yml': 'repos: [git@internal-secret-host.example:x\n  bad: {\n'})
            with self.assertRaises(parser.PolicySourceError) as raised:
                parser.vault_mappings(root)
            self.assertIn('local_repos.yml', str(raised.exception))
            self.assertNotIn('internal-secret-host', str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_rejects_magic_variables(self):
        for key in ['omit', 'inventory_hostname', 'group_names', 'inventory_dir', 'role_path', 'hostvars',
                    'playbook_dir', 'environment']:
            with self.subTest(key=key):
                self.assertRejected(key + ': "0777"\n', repr(key))

    def test_rejects_template_globals(self):
        """A config key named like a template global shadows it: `lookup: 0` breaks every later lookup()."""
        from ansible._internal._templating._jinja_bits import AnsibleEnvironment
        names = set(AnsibleEnvironment().globals) | {'zip'}
        self.assertTrue({'lookup', 'query', 'q', 'now', 'undef', 'range', 'dict', 'lipsum', 'namespace'} <= names)
        for key in sorted(names):
            with self.subTest(key=key):
                self.assertRejected(key + ': 0\n', repr(key))

    def test_rejects_every_name_mapped_from_the_vault(self):
        for key in ['acli_api_token', 'drive_sync_client_id', 'drive_sync_client_secret', 'codex_api_key',
                    'ssh_auth_key', 'ssh_auth_key_pub', 'ssh_signing_key', 'rh_git_key', 'registry_tokens',
                    'notes_transcrypt_password', 'system_grub_password_hash']:
            with self.subTest(key=key):
                self.assertRejected(key + ': x\n', repr(key))

    def test_every_vault_variable_is_mapped(self):
        """A new vault_* variable needs a mapping that the derived shadow list can see."""
        stub = (ROOT / 'group_vars/all/vault.yml').read_text()
        documented = set(re.findall(r'\bvault_[A-Za-z0-9_]+', stub)) - {'vault_placeholder'}
        self.assertIn('vault_grub_password_hash', documented)  # named in the stub's comments only
        mapped = set().union(*parser.vault_mappings().values())
        self.assertEqual(documented - mapped, set())

    def test_rejects_control_characters_in_keys_and_values(self):
        for content, needle in [
                ('system_sysctl_extra:\n  vm.swappiness: "1\\0kernel.core_pattern = |/tmp/x %p"\n', 'U+0000'),
                ('value: "a\\rb"\n', 'U+000D'), ('value: "a\\tb"\n', 'U+0009'), ('value: "722\\n"\n', 'U+000A'),
                ('value: ["ok", "a\\x1bb"]\n', 'U+001B'), ('value: "a\\x7f"\n', 'U+007F'),
                ('nested:\n  "k\\0ey": 1\n', 'U+0000'), ('"top\\x01": 1\n', 'U+0001'),
                ('value: "a\\x85b"\n', 'U+0085'), ('value: "a\\x9fb"\n', 'U+009F'),
                ('value: "a\\u2028b"\n', 'U+2028'), ('value: "a\\u2029b"\n', 'U+2029')]:
            with self.subTest(content=content):
                self.assertRejected(content, needle)

    def test_messages_name_keys_never_values(self):
        found = self.violations('ansible_become_exe: /tmp/SECRETVALUE\nvalue: "SECRET\\0VALUE"\n')
        self.assertEqual(len(found), 2, found)
        self.assertNotIn('SECRET', '\n'.join(found))

    def run_guard(self, config):
        """Run the production guard and pre-flight's config load, then a module."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'interpreter-ran'
            interpreter = Path(directory) / 'python'
            interpreter.write_text('#!/bin/sh\ntouch ' + str(marker) + '\nexec ' + str(Path(ANSIBLE).parent / 'python3') + ' "$@"\n')
            interpreter.chmod(0o755)
            (Path(directory) / 'config.yml').write_text(config.replace('INTERPRETER', str(interpreter)))
            (Path(directory) / 'guard.yml').write_text(yaml.safe_dump([{
                'hosts': 'localhost', 'gather_facts': False, 'tasks': [
                    {'name': 'Guard', 'ansible.builtin.include_tasks': str(ROOT / 'common/tasks/config_ssti_guard.yml')},
                    pre_flight_tasks('Load user overrides')[0],
                    {'name': 'Run a module', 'ansible.builtin.command': {'argv': ['true']}, 'changed_when': False}]}]))
            env = dict(os.environ, MOLECULE_PROJECT_DIRECTORY=str(ROOT), ANSIBLE_NOCOLOR='1',
                       ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'))
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', 'guard.yml'], cwd=directory,
                                    env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT)
            return result, marker.exists()

    def test_production_guard_rejects_before_loading(self):
        result, ran = self.run_guard('dotfiles_user_name: Valid User\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for config in ['ansible_python_interpreter: INTERPRETER\n',
                       'system_sysctl_extra:\n  vm.swappiness: "1\\0kernel.core_pattern = |/tmp/x %p"\n',
                       '_csb_molecule_force: true\n', 'acli_api_token: shadow\n']:
            with self.subTest(config=config):
                result, ran = self.run_guard(config)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('Fail when config.yml sets keys or characters that change execution', result.stdout)
                self.assertFalse(ran)


def pre_flight_tasks(*prefixes):
    """Top-level tasks of the pre-flight block whose names start with the given prefixes, in file order."""
    block = yaml.safe_load((ROOT / 'common/tasks/pre_flight_checks.yml').read_text())[0]['block']
    return [task for task in block if task.get('name', '').startswith(prefixes)]


SHADOW_PATHS = ['vars/config.yml', 'common/tasks/config.yml', 'common/tasks/vars/config.yml',
                'vars/default.config.yml', 'common/tasks/default.config.yml', 'common/tasks/vars/default.config.yml']
LOAD_PREFIXES = ('Load configuration', 'Load user overrides')
SHADOW_PREFIX = ('Assert no copy of config.yml or default.config.yml shadows',)


class Project:
    """A scratch project with the production guard and pre-flight's load tasks, and a marker interpreter."""

    def __init__(self, directory, config='probe_source: checked\n', prefixes=LOAD_PREFIXES):
        self.root = Path(directory)
        self.marker, self.interpreter = self.root / 'interpreter-ran', self.root / 'python'
        self.interpreter.write_text('#!/bin/sh\ntouch ' + str(self.marker) + '\nexec '
                                    + str(Path(ANSIBLE).parent / 'python3') + ' "$@"\n')
        self.interpreter.chmod(0o755)
        self.evil = 'probe_source: planted\nansible_python_interpreter: ' + str(self.interpreter) + '\n'
        shutil.copy(ROOT / 'default.config.yml', self.root / 'default.config.yml')
        (self.root / 'config.yml').write_text(config)
        (self.root / 'common/tasks').mkdir(parents=True)
        (self.root / 'common/tasks/load.yml').write_text(yaml.safe_dump(pre_flight_tasks(*prefixes)))

    load = [{'name': 'Guard', 'ansible.builtin.include_tasks': str(ROOT / 'common/tasks/config_ssti_guard.yml')},
            {'name': 'Load as pre-flight does', 'ansible.builtin.include_tasks': 'common/tasks/load.yml'}]

    def plant(self, paths):
        for path in paths:
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(self.evil)

    def run(self, plays, inventory='localhost,', env=None):
        env = dict(os.environ, MOLECULE_PROJECT_DIRECTORY=str(ROOT), ANSIBLE_NOCOLOR='1',
                   ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'), **(env or {}))
        (self.root / 'play.yml').write_text(yaml.safe_dump(plays))
        return subprocess.run([str(ANSIBLE), '-i', inventory, '-c', 'local', 'play.yml'], cwd=self.root,
                              env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT)

    def run_probe(self, env=None):
        return self.run([{'hosts': 'localhost', 'gather_facts': False, 'vars': {'common_project_root': str(self.root)},
                          'tasks': self.load + [{'name': 'Report', 'ansible.builtin.debug': {'msg': 'PROBE={{ probe_source }}'}},
                                                {'name': 'Run a module', 'ansible.builtin.command': {'argv': ['true']},
                                                 'changed_when': False}]}], env=env)


class ConfigShadowTests(unittest.TestCase):
    """A copy of config.yml on Ansible's relative include search path must not load unscanned."""

    def test_loads_only_the_checked_project_root_file(self):
        before = set(Path(tempfile.gettempdir()).glob('*config.yml'))
        with tempfile.TemporaryDirectory() as directory:
            project = Project(directory)
            project.plant(SHADOW_PATHS)
            result = project.run_probe()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PROBE=checked', result.stdout)
            self.assertFalse(project.marker.exists())
        self.assertEqual(set(Path(tempfile.gettempdir()).glob('*config.yml')) - before, set())  # no copies left

    def test_pre_flight_refuses_while_a_shadowing_copy_exists(self):
        for path in [None] + SHADOW_PATHS:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                project = Project(directory, prefixes=LOAD_PREFIXES + SHADOW_PREFIX)
                project.plant([path] if path else [])
                result = project.run_probe()
                self.assertEqual(result.returncode, 2 if path else 0, result.stdout + result.stderr)
                if path:
                    self.assertIn('SECURITY: remove ', result.stdout)
                    self.assertIn('/' + path, result.stdout)
                self.assertFalse(project.marker.exists())


class ConfigLoadTests(unittest.TestCase):
    """config.yml values are facts set from the checked dict: they reach later plays and the container host."""

    def test_later_plays_and_the_container_host_see_config_values(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Project(directory, config='probe_source: checked\nprofile: personal\nssh_port: 2222\n'
                                                'probe_list: [a, 1]\n')
            inventory = project.root / 'inventory'
            inventory.write_text('localhost ansible_connection=local\ncontainer1 ansible_connection=local '
                                 'ansible_python_interpreter=' + str(Path(ANSIBLE).parent / 'python3') + '\n')
            report = {'name': 'Report', 'ansible.builtin.debug': {
                'msg': "{{ play }}|{{ probe_source }}|{{ profile }}|{{ ssh_port | type_debug }}|{{ probe_list | join(',') }}|"
                       "{{ probe_list[1] | type_debug }}"}}
            roots = {'common_project_root': str(project.root)}
            result = project.run([
                {'name': 'Play 0', 'hosts': 'localhost', 'gather_facts': False, 'vars': roots, 'tasks': project.load},
                {'name': 'Play 2', 'hosts': 'localhost', 'gather_facts': False, 'vars': {'play': 'PLAY2'}, 'tasks': [report]},
                {'name': 'Play 3', 'hosts': 'container1', 'gather_facts': False, 'vars': dict(roots, play='PLAY3'),
                 'pre_tasks': [
                     {'name': 'Not loaded yet on this host', 'ansible.builtin.assert': {'that': 'probe_source is not defined'}},
                     {'name': 'Validate config like site.yml Play 3', 'delegate_to': 'localhost', 'block': project.load}],
                 'tasks': [report]}], inventory=str(inventory))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for play in ('PLAY2', 'PLAY3'):  # config.yml beats default.config.yml (profile: work) and keeps types
                self.assertIn(play + '|checked|personal|int|a,1|int', result.stdout)

    def test_facts_set_before_the_config_load_are_refused(self):
        """set_fact ties go to the last writer: a fact set before the load must not be settable from config.yml."""
        before = []
        play0 = yaml.safe_load((ROOT / 'pre-flight.yml').read_text())[0]['tasks']
        before += play0[:[task.get('name') for task in play0].index('Run early pre-flight checks (before any role execution)')]
        play3 = next(play for play in yaml.safe_load((ROOT / 'site.yml').read_text()) if play.get('hosts') == 'containers')
        pre = play3['pre_tasks']
        before += pre[:[task.get('name', '') for task in pre].index(
            'SECURITY CRITICAL — Validate config integrity before container provisioning')]
        names = set()
        for task in before:
            names.update([task['register']] if 'register' in task else [])
            names.update(task.get('ansible.builtin.set_fact') or {})
        self.assertIn('real_user_home', names)
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertTrue(parser.policy_violations({name: 'x'}), name)

    def test_racing_config_writes_never_reach_the_interpreter(self):
        """A same-user racer flips config.yml and any *config.yml in TMPDIR while the guard runs; nothing loads it."""
        import threading
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as tmpdir:
            project = Project(directory)
            source, checked = project.root / 'config.yml', 'probe_source: checked\n'
            stop, swaps, seen = threading.Event(), [], {}

            def race():
                flip = 0
                while not stop.is_set():
                    flip ^= 1
                    staged = project.root / '.staged'
                    staged.write_text(checked + (project.evil if flip else ''))
                    os.replace(staged, source)
                    for copy in Path(tmpdir).glob('*config.yml'):  # the private copy an older load used
                        try:
                            good = seen.setdefault(copy, copy.read_text())
                            for content in (good + project.evil, good):  # flip so a later hash can match
                                staged_copy = copy.with_name(copy.name + '.rv')
                                staged_copy.write_text(content)
                                os.replace(staged_copy, copy)
                            swaps.append(copy)
                        except OSError:
                            pass
                    stop.wait(0.001)
            racer = threading.Thread(target=race)
            racer.start()
            outcomes = []
            try:
                for _ in range(20):
                    result = project.run_probe(env={'TMPDIR': tmpdir})
                    self.assertFalse(project.marker.exists(), result.stdout + result.stderr)
                    self.assertIn(result.returncode, (0, 2), result.stdout + result.stderr)
                    outcomes.append(result.returncode)
                    if result.returncode == 0:
                        self.assertIn('PROBE=checked', result.stdout)
            finally:
                stop.set()
                racer.join()
            self.assertEqual(len(set(swaps)), 0, 'config.yml copies were written to TMPDIR')  # nothing to swap


GUARD_FILES = ['common/tasks/config_ssti_guard.yml', 'common/tasks/pre_flight_checks.yml']


def guard_variables():
    """Every variable the guard files register, set_fact or define as task/block vars or loop vars."""
    names = set()

    def walk(tasks):
        for task in tasks or []:
            names.update([task['register']] if 'register' in task else [])
            facts = task.get('ansible.builtin.set_fact') or task.get('set_fact') or {}
            names.update(key for key in facts if key != 'cacheable')
            names.update(task.get('vars') or {})
            names.update([task['loop_control']['loop_var']] if 'loop_var' in task.get('loop_control', {}) else [])
            for key in ('block', 'rescue', 'always'):
                walk(task.get(key))
    for path in GUARD_FILES:
        walk(yaml.safe_load((ROOT / path).read_text()))
    return names


class GuardVariableTests(unittest.TestCase):
    def test_every_guard_variable_is_protected_from_extra_vars(self):
        """-e outranks register and set_fact: an unprotected guard variable lets -e skip the guard."""
        protected = parser.protected_keys_pattern()
        names = guard_variables()
        self.assertIn('_pf_config_raw', names)
        for name in sorted(names):
            with self.subTest(name=name):
                # the wrapper searches every -e value, in both key=value and JSON form
                self.assertRegex(name + '=1', protected)
                self.assertRegex(json.dumps({name: {}}), protected)

    def test_undecodable_input_reports_the_error_type_only(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/parse-config.py')],
                                input=b'value: "\xff\xfe internal-secret-host"\n', capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.decode(), 'Cannot parse config.yml: UnicodeDecodeError\n')


if __name__ == "__main__":
    unittest.main()
