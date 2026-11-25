#!/usr/bin/env python3
"""Exercise the production tmpfiles template without changing the real home."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent


class ClaudePrivacyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('systemd-tmpfiles'), 'Requires Linux systemd-tmpfiles')
    def test_recursive_privacy_preserves_file_modes_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            default = home / '.claude'
            personal = home / '.claude-per2'
            nested = default / 'nested'
            nested.mkdir(parents=True)
            personal.mkdir()
            for path in [default, nested, personal]:
                path.chmod(0o755)
            modes = {default / 'CLAUDE.md': 0o600, default / 'settings.json': 0o644,
                     nested / 'session.json': 0o644, nested / 'run.sh': 0o755,
                     personal / 'settings.json': 0o644}
            for path, mode in modes.items():
                path.write_text('public test fixture\n')
                path.chmod(mode)
            outside = home / 'outside.txt'
            outside.write_text('public outside fixture\n')
            outside.chmod(0o644)
            (nested / 'outside-link').symlink_to(outside)
            template = Environment(undefined=StrictUndefined).from_string(
                (ROOT / 'roles/claude/templates/claude-privacy.conf.j2').read_text())
            config = home / 'privacy.conf'
            config.write_text(template.render(_claude_instances=[str(default), str(personal)],
                                              claude_default_config_dir=str(default)).replace('%h', str(home)))
            for _ in range(2):
                result = subprocess.run(['systemd-tmpfiles', '--user', '--create', str(config)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                for path in [default, nested]:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                for path in modes:
                    expected = 0o700 if path.name == 'run.sh' else 0o600
                    self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))
                self.assertEqual(outside.stat().st_mode & 0o777, 0o644)

    def test_policy_targets_user_settings_not_local_settings(self):
        tasks = (ROOT / 'roles/claude/tasks/main.yml').read_text()
        defaults = (ROOT / 'roles/claude/defaults/main.yml').read_text()
        template = (ROOT / 'roles/claude/templates/settings-policy.json.j2').read_text()
        self.assertIn('merge-claude-settings.py', tasks)
        self.assertIn("item ~ '/settings.json'", tasks)
        self.assertNotRegex(tasks, r'dest:\s*"?\{\{ item \}\}/settings\.local\.json')
        for key in ['"sandbox"', '"enableAllProjectMcpServers"', '"crossSessionInbound"', '"deny"']:
            self.assertIn(key, template)
        self.assertIn('"**/.githooks/**"', defaults)
        self.assertIn('"**/.git/hooks/**"', defaults)
        for rule in ['"**/.claude/settings.json"', '"**/.claude/settings.local.json"', '"**/.mcp.json"']:
            self.assertIn(rule, defaults)
        self.assertNotIn('Write(', template)

    def test_weaker_nested_sandbox_only_in_containers(self):
        defaults = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
        ansible = ROOT / '.venv/bin/ansible'
        if not ansible.exists():
            self.skipTest('no .venv ansible')
        for in_container, tier, expected, mode in [(False, 'host-only', False, 'bypassPermissions'),
                                                   (False, 'hybrid', False, 'default'),
                                                   (True, 'host-only', True, 'bypassPermissions'),
                                                   (False, 'container', True, 'bypassPermissions')]:
            defaults['claude_default_mode'] = mode
            with tempfile.TemporaryDirectory() as tmp:
                vars_file = Path(tmp) / 'v.json'
                vars_file.write_text(json.dumps({
                    **defaults, 'system_is_container': in_container, 'needs_container_tier': tier,
                    'ansible_facts': {'user_dir': tmp}, '_claude_instances': [], 'item': tmp}))
                out = Path(tmp) / 'o.json'
                result = subprocess.run(
                    [str(ansible), 'localhost', '-m', 'template', '-a',
                     f'src={ROOT}/roles/claude/templates/settings-policy.json.j2 dest={out}',
                     '-e', f'@{vars_file}', '-i', 'localhost,', '-c', 'local'],
                    capture_output=True, text=True, timeout=120, cwd=tmp,
                    env={**os.environ, 'ANSIBLE_LOCAL_TEMP': tmp, 'ANSIBLE_REMOTE_TEMP': tmp, 'HOME': tmp})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rendered = json.loads(out.read_text())
                sandbox = rendered['sandbox']
                bypass = defaults['claude_default_mode'] == 'bypassPermissions'
                self.assertEqual(rendered.get('skipDangerousModePermissionPrompt'), True if bypass else None)
                self.assertEqual(rendered['permissions']['defaultMode'], defaults['claude_default_mode'])
                self.assertEqual(rendered['crossSessionInbound'], 'accept')
                self.assertNotIn('remoteControlAtStartup', rendered)  # seeded through --defaults, never enforced
                self.assertEqual(rendered['hooks']['PreToolUse'], [{'matcher': 'Bash', 'hooks': [{
                    'type': 'command', 'command': f'/usr/bin/python3 -I {tmp}/.config/claude/hooks/file-heredoc-guard.py'}]}])
                self.assertEqual(rendered['permissions']['deny'][:len(defaults['claude_deny_bash'])],
                                 defaults['claude_deny_bash'])
                self.assertTrue(set(defaults['claude_deny_bash']).isdisjoint(defaults['claude_retired_deny']))
                self.assertIs(sandbox['enableWeakerNestedSandbox'], expected, (in_container, tier))
                self.assertEqual({k: v for k, v in sandbox.items() if k != 'enableWeakerNestedSandbox'},
                                 {'enabled': False, 'allowUnsandboxedCommands': False, 'failIfUnavailable': True})

    # Files a Chrome profile holds (Linux and macOS): the secret stores, and the ones make chrome-bookmarks-*
    # reads. A deny glob names a file or dir at any depth, and a denied dir covers everything in it.
    CHROME_SECRETS = ['Default/Cookies', 'Default/Cookies-journal', 'Default/Network/Cookies', 'Default/Extension Cookies',
                      'Default/Safe Browsing Cookies', 'Default/Login Data', 'Default/Login Data For Account',
                      'Default/Login Data-journal', 'Default/Web Data', 'Default/Account Web Data', 'Default/History',
                      'Default/Media History', 'Default/Sessions/Session_1', 'Default/Current Session', 'Default/Last Tabs',
                      'Default/Local Storage/leveldb/000003.log', 'Default/Session Storage/MANIFEST-000001',
                      'Default/IndexedDB/https_x_0.indexeddb.leveldb/000003.ldb', 'Default/Extension State/LOG',
                      'Default/Local Extension Settings/abc/000003.log', 'Default/Sync Extension Settings/abc/x',
                      'Default/Extension Rules/x', 'Default/Sync Data/LevelDB/000005.ldb',
                      'Default/Trust Tokens', 'Default/Service Worker/CacheStorage/x', 'Default/File System/000/t',
                      'Default/databases/Databases.db', 'Default/GCM Store/Encryption/LOG', 'Default/Shared Dictionary/db',
                      'Default/Affiliation Database', 'Default/Top Sites', 'Default/Visited Links', 'Default/Shortcuts',
                      'Default/Favicons', 'Default/Network Action Predictor', 'Profile 1/Cookies', 'Safe Browsing/x.pb',
                      'Default/shared_proto_db/000003.log', 'Default/blob_storage/x', 'Default/WebStorage/QuotaManager',
                      'Default/trusted_vault.pb', 'Default/DIPS', 'Default/Conversions', 'Default/InterestGroups',
                      'Default/BrowsingTopicsSiteData', 'Default/PrivateAggregation',
                      'Default/heavy_ad_intervention_opt_out.db', 'Default/Network Persistent State',
                      'Default/TransportSecurity', 'Default/Reporting and NEL', 'Default/QuotaManager',
                      'Crashpad/completed/x.dmp', 'Crash Reports/x.dmp',
                      'Default/Policy/User Policy', 'Policy/Enrollment/CloudManagementEnrollmentToken']
    CHROME_READABLE = ['Local State', 'Default/Bookmarks', 'Default/AccountBookmarks', 'Profile 1/Bookmarks',
                       'Default/Bookmarks.bak', 'Default/Preferences', 'Default/Secure Preferences',
                       'Default/Extensions/abc/1.0_0/manifest.json']

    def test_chrome_profiles_deny_secret_stores_and_leave_bookmarks_readable(self):
        import fnmatch
        defaults = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
        ansible = ROOT / '.venv/bin/ansible'
        if not ansible.exists():
            self.skipTest('no .venv ansible')
        for system, data_dir, other in [
                ('Darwin', '~/Library/Application Support/Google/Chrome', '~/.config/google-chrome'),
                ('Darwin', '~/Library/Application Support/Chromium', '~/.config/chromium'),
                ('Linux', '~/.config/google-chrome', '~/Library/Application Support/Google/Chrome'),
                ('Linux', '~/.config/chromium', '~/Library/Application Support/Chromium')]:
            with tempfile.TemporaryDirectory() as tmp:
                vars_file = Path(tmp) / 'v.json'
                vars_file.write_text(json.dumps({
                    **defaults, 'system_is_container': False, 'needs_container_tier': 'host-only',
                    'ansible_facts': {'user_dir': tmp, 'system': system}, '_claude_instances': [], 'item': tmp}))
                out = Path(tmp) / 'o.json'
                result = subprocess.run(
                    [str(ansible), 'localhost', '-m', 'template', '-a',
                     f'src={ROOT}/roles/claude/templates/settings-policy.json.j2 dest={out}',
                     '-e', f'@{vars_file}', '-i', 'localhost,', '-c', 'local'],
                    capture_output=True, text=True, timeout=120, cwd=tmp,
                    env={**os.environ, 'ANSIBLE_LOCAL_TEMP': tmp, 'ANSIBLE_REMOTE_TEMP': tmp, 'HOME': tmp})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                deny = json.loads(out.read_text())['permissions']['deny']
            prefix = data_dir + '/**/'
            globs = [d[len('Read(' + prefix):-1] for d in deny if d.startswith('Read(' + prefix)]
            self.assertEqual(sorted(globs), sorted(defaults['claude_chromium_deny_globs']), system)
            self.assertFalse([d for d in deny if d.startswith('Edit(' + prefix)], system)  # reads only
            self.assertFalse([d for d in deny if d.startswith('Read(' + other)], system)  # only this OS's dirs
            self.assertNotIn(f'Read({data_dir}/**)', deny)  # no wholesale read rule
            self.assertIn(f'Edit({data_dir}/**)', deny)  # writes stay denied wholesale (NativeMessagingHosts)
            if data_dir != '~/Library/Application Support/Chromium':  # the one dir earlier releases did not deny
                self.assertIn(f'Read({data_dir}/**)', defaults['claude_retired_deny'])
            self.assertNotIn(f'Edit({data_dir}/**)', defaults['claude_retired_deny'])

            def denied(rel):
                return any(fnmatch.fnmatchcase(part, glob) for part in rel.split('/') for glob in globs)
            self.assertEqual([f for f in self.CHROME_SECRETS if not denied(f)], [], system)
            self.assertEqual([f for f in self.CHROME_READABLE if denied(f)], [], system)

    def test_retired_wholesale_chrome_rules_leave_existing_settings(self):
        defaults = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
        old = ['Read(~/Library/Application Support/Google/Chrome/**)', 'Edit(~/Library/Application Support/Google/Chrome/**)',
               'Read(~/.config/google-chrome/**)', 'Read(~/.ssh/**)']
        new = ['Read(~/Library/Application Support/Google/Chrome/**/*Cookies*)', 'Read(~/.ssh/**)',
               'Edit(~/Library/Application Support/Google/Chrome/**)']
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'settings.json'
            settings.write_text(json.dumps({'permissions': {'deny': old}}))
            result = subprocess.run(
                [sys.executable, '-I', str(ROOT / 'roles/claude/files/merge-claude-settings.py'), str(settings),
                 '--retire', json.dumps(defaults['claude_retired_deny'])],
                input=json.dumps({'permissions': {'deny': new}}), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(settings.read_text())['permissions']['deny'], new)

    def test_remote_control_is_seeded_only_in_the_owners_personal_homes(self):
        defaults = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
        tasks = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        merge = next(t for t in tasks if t.get('name') == 'Merge security policy into instance settings.json')
        seed = merge['vars']['_claude_settings_seed']
        ansible = ROOT / '.venv/bin/ansible'
        if not ansible.exists():
            self.skipTest('no .venv ansible')
        for home, expected in [('.claude', True), ('.claude-per2', True), ('.claude-work', None),
                               ('.claude-friend', None)]:
            with tempfile.TemporaryDirectory() as tmp:
                vars_file = Path(tmp) / 'v.json'
                vars_file.write_text(json.dumps({
                    **defaults, 'ansible_facts': {'user_dir': tmp}, 'item': f'{tmp}/{home}', 'seed': seed}))
                (Path(tmp) / 'seed.j2').write_text('{{ seed | to_json }}')
                out = Path(tmp) / 'o.json'
                result = subprocess.run(
                    [str(ansible), 'localhost', '-m', 'template', '-a', f'src={tmp}/seed.j2 dest={out}',
                     '-e', f'@{vars_file}', '-i', 'localhost,', '-c', 'local'],
                    capture_output=True, text=True, timeout=120, cwd=tmp,
                    env={**os.environ, 'ANSIBLE_LOCAL_TEMP': tmp, 'ANSIBLE_REMOTE_TEMP': tmp, 'HOME': tmp})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(out.read_text()).get('remoteControlAtStartup'), expected, home)

    def test_a_seeded_key_never_overrides_the_users_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'settings.json'
            settings.write_text('{"remoteControlAtStartup": false}')
            result = subprocess.run(
                ['python3', '-I', str(ROOT / 'roles/claude/files/merge-claude-settings.py'), str(settings),
                 '--defaults', '{"remoteControlAtStartup": true}'],
                input='{"crossSessionInbound": "accept"}', capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIs(json.loads(settings.read_text())['remoteControlAtStartup'], False)

if __name__ == '__main__':
    unittest.main()
